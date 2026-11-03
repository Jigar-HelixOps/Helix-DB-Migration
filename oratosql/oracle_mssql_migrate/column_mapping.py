import logging
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)


class ColumnMappingSpec(object):
    def __init__(self, source, target, expression=None):
        self.source = source
        self.target = target
        self.expression = expression


def load_column_mappings(path):
    mapping_path = Path(path) if path else None
    if not mapping_path or not mapping_path.exists():
        return {}, {}

    with mapping_path.open("r", encoding="utf-8") as handle:
        raw = yaml.safe_load(handle) or {}

    global_columns = _parse_table_mapping(raw.get("global_columns", {}) or {})
    tables = raw.get("tables")
    if tables is None:
        tables = {
            key: value
            for key, value in raw.items()
            if key != "global_columns"
        }

    mappings = {}
    for table_name, table_mapping in (tables or {}).items():
        if not table_mapping:
            continue
        mappings[table_name.lower()] = _parse_table_mapping(table_mapping)
    return mappings, global_columns


def _parse_table_mapping(table_mapping):
    parsed = {}
    for source_key, value in table_mapping.items():
        if isinstance(value, dict):
            target = value.get("target", source_key)
            parsed[source_key.lower()] = ColumnMappingSpec(
                source=source_key,
                target=target,
                expression=value.get("expression"),
            )
        else:
            parsed[source_key.lower()] = ColumnMappingSpec(
                source=source_key,
                target=value,
            )
    return parsed


def get_table_column_mapping(mappings, table_name):
    return mappings.get(table_name.lower(), {})


def _quote_oracle_identifier(name):
    # Quoted Oracle identifiers are case-sensitive; catalog names are uppercase.
    return '"{}"'.format(str(name).upper().replace('"', '""'))


def get_oracle_columns(connection, schema, table):
    sql = """
        SELECT column_name
        FROM all_tab_columns
        WHERE owner = :owner
          AND table_name = :table_name
        ORDER BY column_id
    """
    with connection.cursor() as cursor:
        cursor.execute(sql, owner=schema.upper(), table_name=table.upper())
        return {row[0].lower(): row[0] for row in cursor.fetchall()}


def _find_mapping_for_target(table_mapping, target_column):
    target_key = target_column.lower()
    for spec in table_mapping.values():
        if spec.target.lower() == target_key:
            return spec
    return None


def resolve_export_plan(
    config,
    connection,
    schema,
    table,
    target_columns_ordered,
    table_mapping=None,
    global_mapping=None,
):
    table_mapping = table_mapping or {}
    global_mapping = global_mapping or {}
    source_columns = get_oracle_columns(connection, schema, table)

    select_parts = []
    csv_headers = []
    source_column_names = []

    for target_column in target_columns_ordered:
        spec = _find_mapping_for_target(table_mapping, target_column)
        if spec is None:
            spec = _find_mapping_for_target(global_mapping, target_column)

        if spec is None and target_column.lower() in source_columns:
            spec = ColumnMappingSpec(
                source=source_columns[target_column.lower()],
                target=target_column,
            )

        # Do NOT look up table_mapping by target name — keys are Oracle source names.

        if spec is None:
            # SQL Server-only columns (defaults / extras) — omit from export/INSERT.
            logger.warning(
                "Skipping SQL Server column %s on %s.%s "
                "(not found in Oracle; omitted from INSERT — column default used if any)",
                target_column,
                schema,
                table,
            )
            continue

        target_name = target_column
        if spec.expression:
            expression_sql = spec.expression
        else:
            source_key = (spec.source or "").lower()
            if source_key not in source_columns:
                raise ValueError(
                    "Mapped Oracle column '{}' not found on {}.{}. "
                    "SQL Server column is '{}'.".format(
                        spec.source, schema, table, target_column
                    )
                )
            source_name = source_columns[source_key]
            expression_sql = _quote_oracle_identifier(source_name)

        select_parts.append(
            '{} AS "{}"'.format(expression_sql, target_name.replace('"', '""'))
        )
        csv_headers.append(target_name)
        source_column_names.append(spec.source or target_name)

    if table_mapping or global_mapping:
        logger.info(
            "Using column mapping for %s.%s (table=%s global=%s)",
            schema,
            table,
            len(table_mapping),
            len(global_mapping),
        )

    return ", ".join(select_parts), csv_headers, source_column_names
