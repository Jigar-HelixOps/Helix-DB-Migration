import logging
from pathlib import Path

import yaml

from .source_types import SOURCE_ORACLE, SOURCE_SQLSERVER, normalize_source_type

logger = logging.getLogger(__name__)


class ColumnMappingSpec(object):
    def __init__(self, source, target, expression=None):
        self.source = source
        self.target = target
        self.expression = expression


# Oracle adds lib_partition_* columns via lib_partition.sql. SQL Server partitions
# on existing date/datetime columns without adding lib_partition_* names.
SOURCE_DATE_TYPES = frozenset(
    (
        "date",
        "datetime",
        "datetime2",
        "smalldatetime",
        "datetimeoffset",
        "timestamp",
        "timestamp with time zone",
        "timestamp with local time zone",
    )
)

# Backward-compatible alias
SQLSERVER_DATE_TYPES = SOURCE_DATE_TYPES

SQLSERVER_PARTITION_RESULT_DATE_SOURCES = (
    "lib_partition_result_date",
    "partition_result_date",
    "date_created",
    "created_date",
    "create_date",
    "result_date",
    "completion_date",
    "start_date",
    "record_date",
    "partition_date",
)

SQLSERVER_PARTITION_EVENT_DATE_SOURCES = (
    "lib_partition_event_date",
    "partition_event_date",
    "date_created",
    "event_date",
    "created_date",
    "start_time",
    "event_time",
    "start_date",
)

PG_PARTITION_COLUMNS = frozenset(
    ("lib_partition_result_date", "lib_partition_event_date")
)

PARTITION_COLUMN_AUTODETECT = {
    "lib_partition_result_date": {
        "candidates": SQLSERVER_PARTITION_RESULT_DATE_SOURCES,
    },
    "lib_partition_event_date": {
        "candidates": SQLSERVER_PARTITION_EVENT_DATE_SOURCES,
    },
}

# Backward-compatible alias
SQLSERVER_PARTITION_AUTODETECT = PARTITION_COLUMN_AUTODETECT

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


def resolve_pg_column_from_source(mappings, pg_table, source_column):
    table_mapping = get_table_column_mapping(mappings, pg_table)
    source_key = source_column.lower()
    for spec in table_mapping.values():
        if spec.source.lower() == source_key:
            return spec.target
    return source_column


def _quote_sqlserver_identifier(name):
    return "[{}]".format(name.replace("]", "]]"))


def _quote_oracle_identifier(name):
    return '"{}"'.format(name.replace('"', '""'))


def get_source_columns(connection, config, schema, table):
    columns, _column_types = get_source_columns_with_types(
        connection, config, schema, table
    )
    return columns


def get_source_columns_with_types(connection, config, schema, table):
    source_type = normalize_source_type(config.source.type)
    if source_type == SOURCE_SQLSERVER:
        sql = """
            SELECT COLUMN_NAME, DATA_TYPE
            FROM INFORMATION_SCHEMA.COLUMNS
            WHERE TABLE_SCHEMA = ?
              AND TABLE_NAME = ?
            ORDER BY ORDINAL_POSITION
        """
        with connection.cursor() as cursor:
            cursor.execute(sql, schema, table)
            columns = {}
            column_types = {}
            for name, data_type in cursor.fetchall():
                key = name.lower()
                columns[key] = name
                column_types[key] = (data_type or "").lower()
            return columns, column_types

    sql = """
        SELECT column_name, data_type
        FROM all_tab_columns
        WHERE owner = :owner
          AND table_name = :table_name
        ORDER BY column_id
    """
    with connection.cursor() as cursor:
        cursor.execute(sql, owner=schema.upper(), table_name=table.upper())
        columns = {}
        column_types = {}
        for name, data_type in cursor.fetchall():
            key = name.lower()
            columns[key] = name
            column_types[key] = (data_type or "").lower()
        return columns, column_types


def partition_column_handling_enabled(config):
    return bool(getattr(config.migrate, "partition_column_handling", True))


def _find_mapping_for_target(table_mapping, pg_column):
    pg_key = pg_column.lower()
    for spec in table_mapping.values():
        if spec.target.lower() == pg_key:
            return spec
    return None


def _build_cast_date_expression(source_columns, column_types, quote_ident, names):
    parts = []
    for name in names:
        key = name.lower()
        if key not in source_columns:
            continue
        if column_types and column_types.get(key) not in SOURCE_DATE_TYPES:
            if column_types.get(key) not in ("", None):
                continue
        parts.append(
            "CAST({} AS date)".format(quote_ident(source_columns[key]))
        )
    if not parts:
        return None
    if len(parts) == 1:
        return parts[0]
    return "COALESCE({})".format(", ".join(parts))


def _list_source_datetime_columns(source_columns, column_types):
    names = []
    for key in sorted(source_columns.keys()):
        if column_types.get(key) in SOURCE_DATE_TYPES:
            names.append(source_columns[key])
    return names


def _build_job_run_event_partition_subquery(
    connection,
    config,
    schema,
    deploy_table,
    deploy_source_columns,
    quote_ident,
    candidate_names,
):
    if "job_event_id" not in deploy_source_columns:
        return None

    parent_columns, parent_types = get_source_columns_with_types(
        connection, config, schema, "job_run_event"
    )
    coalesce_parts = []
    for name in candidate_names:
        key = name.lower()
        if key not in parent_columns:
            continue
        if parent_types.get(key) not in SOURCE_DATE_TYPES:
            if parent_types.get(key) not in ("", None):
                continue
        coalesce_parts.append(
            "e.{}".format(quote_ident(parent_columns[key]))
        )

    if not coalesce_parts:
        return None

    value_expr = (
        coalesce_parts[0]
        if len(coalesce_parts) == 1
        else "COALESCE({})".format(", ".join(coalesce_parts))
    )

    parent_job_event_col = quote_ident(
        parent_columns.get("job_event_id", "job_event_id")
    )
    deploy_job_event_col = quote_ident(deploy_source_columns["job_event_id"])

    return (
        "(SELECT CAST({value_expr} AS date) "
        "FROM {schema_sql}.{parent_sql} e "
        "WHERE e.{parent_job_event_col} = {schema_sql}.{deploy_sql}.{deploy_job_event_col})"
    ).format(
        value_expr=value_expr,
        schema_sql=quote_ident(schema),
        parent_sql=quote_ident("job_run_event"),
        deploy_sql=quote_ident(deploy_table),
        parent_job_event_col=parent_job_event_col,
        deploy_job_event_col=deploy_job_event_col,
    )


def _auto_resolve_partition_column(
    connection,
    config,
    pg_column,
    source_columns,
    column_types,
    quote_ident,
    schema,
    table,
    source_type,
):
    pg_key = pg_column.lower()
    table_key = table.lower()
    autodetect = PARTITION_COLUMN_AUTODETECT.get(pg_key)
    if not autodetect:
        return None

    for candidate in autodetect["candidates"]:
        if candidate not in source_columns:
            continue
        source_name = source_columns[candidate]
        data_type = column_types.get(candidate)
        if data_type in SOURCE_DATE_TYPES and data_type != "date":
            expression = "CAST({} AS date)".format(quote_ident(source_name))
            logger.info(
                "Auto-mapping %s.%s -> %s from %s",
                table,
                pg_column,
                pg_column,
                source_name,
            )
            return ColumnMappingSpec(
                source=candidate,
                target=pg_column,
                expression=expression,
            )
        return ColumnMappingSpec(
            source=source_name,
            target=pg_column,
        )

    if (
        source_type == SOURCE_SQLSERVER
        and table_key == "deploy_job_run_event"
        and pg_key == "lib_partition_event_date"
    ):
        expression = _build_job_run_event_partition_subquery(
            connection,
            config,
            schema,
            table,
            source_columns,
            quote_ident,
            autodetect["candidates"],
        )
        if expression:
            logger.info(
                "Auto-mapping %s.%s -> %s using job_run_event lookup: %s",
                table,
                pg_column,
                pg_column,
                expression,
            )
            return ColumnMappingSpec(
                source="(expression)",
                target=pg_column,
                expression=expression,
            )

    expression = _build_cast_date_expression(
        source_columns,
        column_types,
        quote_ident,
        autodetect["candidates"],
    )
    if expression:
        logger.info(
            "Auto-mapping %s.%s -> %s using %s",
            table,
            pg_column,
            pg_column,
            expression,
        )
        return ColumnMappingSpec(
            source="(expression)",
            target=pg_column,
            expression=expression,
        )

    for key in sorted(source_columns.keys()):
        if column_types.get(key) not in SOURCE_DATE_TYPES:
            continue
        if "date" in key or "time" in key:
            expression = "CAST({} AS date)".format(
                quote_ident(source_columns[key])
            )
            logger.info(
                "Auto-mapping %s.%s -> %s from datetime column %s",
                table,
                pg_column,
                pg_column,
                source_columns[key],
            )
            return ColumnMappingSpec(
                source=source_columns[key],
                target=pg_column,
                expression=expression,
            )
    return None


def _resolve_column_spec(
    connection,
    config,
    pg_column,
    source_columns,
    column_types,
    table_mapping,
    global_mapping,
    source_type,
    quote_ident,
    schema,
    table,
):
    spec = _find_mapping_for_target(table_mapping, pg_column)
    if spec is None:
        spec = _find_mapping_for_target(global_mapping, pg_column)

    if spec is None and pg_column.lower() in source_columns:
        return ColumnMappingSpec(
            source=source_columns[pg_column.lower()],
            target=pg_column,
        )

    if spec is None and pg_column.lower() in table_mapping:
        return table_mapping[pg_column.lower()]

    if (
        partition_column_handling_enabled(config)
        and spec is None
        and pg_column.lower().startswith("lib_")
    ):
        stripped = pg_column.lower()[4:]
        if stripped in source_columns:
            return ColumnMappingSpec(
                source=source_columns[stripped],
                target=pg_column,
            )

    if (
        partition_column_handling_enabled(config)
        and spec is None
        and pg_column.lower() in PARTITION_COLUMN_AUTODETECT
    ):
        spec = _auto_resolve_partition_column(
            connection,
            config,
            pg_column,
            source_columns,
            column_types,
            quote_ident,
            schema,
            table,
            source_type,
        )

    return spec


def resolve_export_plan(
    config,
    connection,
    schema,
    table,
    pg_columns_ordered,
    table_mapping=None,
    global_mapping=None,
):
    table_mapping = table_mapping or {}
    global_mapping = global_mapping or {}
    if partition_column_handling_enabled(config):
        source_columns, column_types = get_source_columns_with_types(
            connection, config, schema, table
        )
    else:
        source_columns = get_source_columns(connection, config, schema, table)
        column_types = {}
    source_type = normalize_source_type(config.source.type)
    quote_ident = (
        _quote_sqlserver_identifier
        if source_type == SOURCE_SQLSERVER
        else _quote_oracle_identifier
    )

    select_parts = []
    csv_headers = []
    source_column_names = []
    auto_mapped = 0
    skipped_partition = 0

    for pg_column in pg_columns_ordered:
        spec = _resolve_column_spec(
            connection,
            config,
            pg_column,
            source_columns,
            column_types,
            table_mapping,
            global_mapping,
            source_type,
            quote_ident,
            schema,
            table,
        )

        if spec is None:
            if pg_column.lower() in PG_PARTITION_COLUMNS:
                skipped_partition += 1
                logger.info(
                    "Skipping PostgreSQL partition column %s on %s.%s "
                    "(not on source; omitted from export — PostgreSQL will "
                    "use column default if defined)",
                    pg_column,
                    schema,
                    table,
                )
                continue

            if partition_column_handling_enabled(config):
                date_columns = _list_source_datetime_columns(
                    source_columns, column_types
                )
                extra = " (source date/datetime columns: {})".format(
                    ", ".join(date_columns) if date_columns else "none"
                )
            else:
                extra = ""
            raise ValueError(
                "No source column mapping found for PostgreSQL column '{}' "
                "on table '{}'{}".format(pg_column, table, extra)
            )

        if (
            spec.expression
            or spec.target.lower() != spec.source.lower()
            or pg_column.lower() != spec.source.lower()
        ):
            if pg_column.lower() in PARTITION_COLUMN_AUTODETECT:
                auto_mapped += 1

        # Always use the exact PostgreSQL catalog name for AS / CSV / COPY.
        # lowercase_columns must not rewrite mixed-case or spaced names like
        # "USER" or "Select Option" — those are case-sensitive identifiers.
        target_name = pg_column
        if spec.expression:
            expression_sql = spec.expression
        else:
            source_name = source_columns.get(spec.source.lower(), spec.source)
            expression_sql = quote_ident(source_name)

        select_parts.append(
            "{} AS {}".format(expression_sql, quote_ident(target_name))
        )
        csv_headers.append(target_name)
        source_column_names.append(spec.source or target_name)

    select_sql = ", ".join(select_parts)
    if table_mapping or global_mapping or auto_mapped or skipped_partition:
        logger.info(
            "Using column mapping for %s.%s (table=%s global=%s auto=%s "
            "skipped_partition=%s)",
            schema,
            table,
            len(table_mapping),
            len(global_mapping),
            auto_mapped,
            skipped_partition,
        )
    return select_sql, csv_headers, source_column_names
