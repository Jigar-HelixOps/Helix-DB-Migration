import logging

from psycopg2 import sql

from .column_mapping import get_table_column_mapping, resolve_pg_column_from_source
from .source_db import (
    get_source_schema,
    get_source_sequences,
    get_source_tables,
    get_sqlserver_identity_columns,
    source_display_name,
)
from .source_types import SOURCE_SQLSERVER, normalize_source_type

logger = logging.getLogger(__name__)


class SequenceSyncResult(object):
    def __init__(
        self,
        source_sequence,
        postgres_sequence,
        source_next_value,
        postgres_old_value,
        postgres_new_value,
        linked_table,
        linked_column,
        max_column_value,
        status,
        error_message=None,
        source_kind="sequence",
    ):
        self.source_sequence = source_sequence
        self.postgres_sequence = postgres_sequence
        self.source_next_value = source_next_value
        self.postgres_old_value = postgres_old_value
        self.postgres_new_value = postgres_new_value
        self.linked_table = linked_table
        self.linked_column = linked_column
        self.max_column_value = max_column_value
        self.status = status
        self.error_message = error_message
        self.source_kind = source_kind

    @property
    def oracle_sequence(self):
        return self.source_sequence

    @property
    def oracle_next_value(self):
        return self.source_next_value


def get_postgres_sequences(connection, schema):
    query = """
        SELECT sequencename, COALESCE(last_value, 0)
        FROM pg_catalog.pg_sequences
        WHERE schemaname = %s
        ORDER BY sequencename
    """
    with connection.cursor() as cursor:
        cursor.execute(query, (schema,))
        return {row[0].lower(): (row[0], int(row[1])) for row in cursor.fetchall()}


def get_postgres_sequence_links(connection, schema):
    query = """
        SELECT
            s.relname AS sequence_name,
            t.relname AS table_name,
            a.attname AS column_name
        FROM pg_class s
        JOIN pg_namespace n ON n.oid = s.relnamespace
        JOIN pg_depend d ON d.objid = s.oid AND d.deptype = 'a'
        JOIN pg_class t ON t.oid = d.refobjid
        JOIN pg_attribute a
          ON a.attrelid = t.oid
         AND a.attnum = d.refobjsubid
          AND NOT a.attisdropped
        WHERE s.relkind = 'S'
          AND n.nspname = %s
    """
    links = {}
    with connection.cursor() as cursor:
        cursor.execute(query, (schema,))
        for sequence_name, table_name, column_name in cursor.fetchall():
            links[sequence_name.lower()] = (table_name, column_name)
    return links


def get_postgres_serial_sequences(connection, schema):
    query = """
        SELECT
            c.relname AS table_name,
            a.attname AS column_name,
            pg_get_serial_sequence(
                quote_ident(n.nspname) || '.' || quote_ident(c.relname),
                a.attname
            ) AS sequence_name
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        JOIN pg_attribute a ON a.attrelid = c.oid
        WHERE n.nspname = %s
          AND c.relkind = 'r'
          AND a.attnum > 0
          AND NOT a.attisdropped
          AND pg_get_serial_sequence(
                quote_ident(n.nspname) || '.' || quote_ident(c.relname),
                a.attname
              ) IS NOT NULL
    """
    column_sequences = {}
    with connection.cursor() as cursor:
        cursor.execute(query, (schema,))
        for table_name, column_name, qualified_sequence in cursor.fetchall():
            sequence_name = qualified_sequence.split(".")[-1]
            column_sequences[(table_name.lower(), column_name.lower())] = (
                sequence_name.lower()
            )
    return column_sequences


def _build_table_sequence_index(postgres_sequences):
    index = {}
    for seq_key in postgres_sequences:
        if seq_key.startswith("seq_"):
            table_key = seq_key[4:]
            index.setdefault(table_key, seq_key)
    return index


def _build_column_sequence_index(sequence_links, serial_sequences):
    column_sequences = dict(serial_sequences)
    for seq_key, (table_name, column_name) in sequence_links.items():
        column_sequences.setdefault(
            (table_name.lower(), column_name.lower()),
            seq_key,
        )
    return column_sequences


def _find_postgres_sequence_key(
    pg_table,
    pg_column,
    postgres_sequences,
    column_sequence_index,
    table_sequence_index,
):
    table_key = pg_table.lower()
    column_key = (table_key, pg_column.lower())

    if column_key in column_sequence_index:
        return column_sequence_index[column_key]

    column_seq_key = "{}_{}_seq".format(table_key, pg_column.lower())
    if column_seq_key in postgres_sequences:
        return column_seq_key

    if table_key in table_sequence_index:
        return table_sequence_index[table_key]

    return None


def _get_max_column_value(connection, schema, table_name, column_name):
    query = sql.SQL("SELECT COALESCE(MAX({}), 0) FROM {}.{}").format(
        sql.Identifier(column_name),
        sql.Identifier(schema),
        sql.Identifier(table_name),
    )
    with connection.cursor() as cursor:
        cursor.execute(query)
        return int(cursor.fetchone()[0])


def _set_postgres_sequence(connection, schema, sequence_name, new_value):
    qualified_name = "{}.{}".format(schema, sequence_name)
    query = "SELECT setval(%s::regclass, %s, false)"
    with connection.cursor() as cursor:
        cursor.execute(query, (qualified_name, new_value))
    connection.commit()


def _resolve_sqlserver_identity_lookup(
    config, source_connection, pg_table, pg_column
):
    source_tables = get_source_tables(source_connection, config)
    source_table = source_tables.get(pg_table.lower(), pg_table)
    source_column = pg_column

    table_mapping = get_table_column_mapping(config.column_mappings, pg_table)
    for spec in table_mapping.values():
        if spec.target.lower() == pg_column.lower():
            source_column = spec.source
            break

    return (source_table.lower(), source_column.lower())


def _excluded_tables(config):
    return {name.lower() for name in (config.migrate.exclude_tables or [])}


def _table_from_sequence_name(sequence_key):
    """Infer table name from common TSSA sequence naming (seq_<table>)."""
    key = (sequence_key or "").lower()
    if key.startswith("seq_"):
        return key[4:]
    return None


def _is_sequence_excluded(exclude, table_name, sequence_key):
    if not exclude:
        return False
    if table_name and table_name.lower() in exclude:
        return True
    inferred = _table_from_sequence_name(sequence_key)
    if inferred and inferred in exclude:
        return True
    return False


def _sync_postgres_sequence(
    postgres_connection,
    schema,
    source_label,
    source_next_value,
    postgres_name,
    postgres_old_value,
    table_name,
    column_name,
    source_kind,
):
    max_column_value = None
    target_next_value = source_next_value

    if table_name and column_name:
        max_column_value = _get_max_column_value(
            postgres_connection, schema, table_name, column_name
        )
        target_next_value = max(source_next_value, max_column_value + 1)

    _set_postgres_sequence(
        postgres_connection, schema, postgres_name, target_next_value
    )

    logger.info(
        "Synced %s %s -> %s.%s: source_next=%s max_col=%s new_next=%s (was %s)",
        source_kind,
        source_label,
        schema,
        postgres_name,
        source_next_value,
        max_column_value,
        target_next_value,
        postgres_old_value,
    )

    return SequenceSyncResult(
        source_sequence=source_label,
        postgres_sequence=postgres_name,
        source_next_value=source_next_value,
        postgres_old_value=postgres_old_value,
        postgres_new_value=target_next_value,
        linked_table=table_name,
        linked_column=column_name,
        max_column_value=max_column_value,
        status="SUCCESS",
        source_kind=source_kind,
    )


def _sync_sqlserver_identity_columns(
    source_connection,
    postgres_connection,
    config,
    schema,
    postgres_sequences,
    sequence_links,
    synced_postgres_sequences,
    results,
    exclude=None,
):
    identity_columns = get_sqlserver_identity_columns(
        source_connection, get_source_schema(config)
    )
    if not identity_columns:
        logger.info("No SQL Server IDENTITY columns found")
        return

    exclude = exclude or set()
    serial_sequences = get_postgres_serial_sequences(postgres_connection, schema)
    column_sequence_index = _build_column_sequence_index(
        sequence_links, serial_sequences
    )
    table_sequence_index = _build_table_sequence_index(postgres_sequences)

    logger.info(
        "Found %s SQL Server IDENTITY column(s); %s PostgreSQL sequence(s), "
        "%s column-linked, %s table-named (seq_<table>)",
        len(identity_columns),
        len(postgres_sequences),
        len(column_sequence_index),
        len(table_sequence_index),
    )

    identities_per_table = {}
    for table_key, column_key in identity_columns:
        identities_per_table.setdefault(table_key, []).append(column_key)

    matched = 0
    missing_sequence = 0
    already_synced = 0
    excluded_count = 0

    for (source_table_key, source_column_key), identity_entry in sorted(
        identity_columns.items()
    ):
        source_table, source_column, source_next_value = identity_entry
        pg_table = source_table_key

        if pg_table in exclude or source_table_key in exclude:
            excluded_count += 1
            logger.debug(
                "Skipping IDENTITY sync for excluded table %s",
                source_table,
            )
            results.append(
                SequenceSyncResult(
                    source_sequence="{}.{} (IDENTITY)".format(
                        source_table, source_column
                    ),
                    postgres_sequence="",
                    source_next_value=source_next_value,
                    postgres_old_value=None,
                    postgres_new_value=None,
                    linked_table=pg_table,
                    linked_column=source_column,
                    max_column_value=None,
                    status="SKIPPED",
                    error_message="Table excluded via migrate.exclude_tables",
                    source_kind="identity",
                )
            )
            continue

        pg_column = resolve_pg_column_from_source(
            config.column_mappings, pg_table, source_column
        )

        if len(identities_per_table.get(source_table_key, [])) > 1:
            sequence_key = _find_postgres_sequence_key(
                pg_table,
                pg_column,
                postgres_sequences,
                column_sequence_index,
                {},
            )
        else:
            sequence_key = _find_postgres_sequence_key(
                pg_table,
                pg_column,
                postgres_sequences,
                column_sequence_index,
                table_sequence_index,
            )

        if not sequence_key:
            missing_sequence += 1
            logger.debug(
                "No PostgreSQL sequence for SQL Server IDENTITY %s.%s",
                source_table,
                source_column,
            )
            continue

        if sequence_key in synced_postgres_sequences:
            already_synced += 1
            continue

        postgres_entry = postgres_sequences.get(sequence_key)
        if not postgres_entry:
            missing_sequence += 1
            continue

        postgres_name, postgres_old_value = postgres_entry
        source_label = "{}.{} (IDENTITY)".format(source_table, source_column)

        try:
            results.append(
                _sync_postgres_sequence(
                    postgres_connection,
                    schema,
                    source_label,
                    source_next_value,
                    postgres_name,
                    postgres_old_value,
                    pg_table,
                    pg_column,
                    "identity",
                )
            )
            synced_postgres_sequences.add(sequence_key)
            matched += 1
        except Exception as exc:
            postgres_connection.rollback()
            logger.exception(
                "Failed to sync IDENTITY %s.%s", source_table, source_column
            )
            results.append(
                SequenceSyncResult(
                    source_sequence=source_label,
                    postgres_sequence=postgres_name,
                    source_next_value=source_next_value,
                    postgres_old_value=postgres_old_value,
                    postgres_new_value=None,
                    linked_table=pg_table,
                    linked_column=pg_column,
                    max_column_value=None,
                    status="FAILED",
                    error_message=str(exc),
                    source_kind="identity",
                )
            )

    logger.info(
        "SQL Server IDENTITY matching: %s synced, %s already synced, "
        "%s without PostgreSQL sequence, %s excluded",
        matched,
        already_synced,
        missing_sequence,
        excluded_count,
    )


def sync_sequences(source_connection, postgres_connection, config):
    schema = config.postgres.schema
    source_schema = get_source_schema(config)
    source_name = source_display_name(config)
    source_type = normalize_source_type(config.source.type)
    results = []
    synced_postgres_sequences = set()
    exclude = _excluded_tables(config)

    logger.info(
        "Starting sequence sync: %s schema %s -> PostgreSQL schema %s",
        source_name,
        source_schema,
        schema,
    )
    if exclude:
        logger.info(
            "Excluding %s table(s) from sequence sync: %s",
            len(exclude),
            ", ".join(sorted(exclude)),
        )

    source_sequences = get_source_sequences(source_connection, config)
    postgres_sequences = get_postgres_sequences(postgres_connection, schema)
    sequence_links = get_postgres_sequence_links(postgres_connection, schema)

    if source_sequences:
        logger.info(
            "Found %s named %s sequence(s)",
            len(source_sequences),
            source_name,
        )
    elif source_type != SOURCE_SQLSERVER:
        logger.warning("No %s sequences found in schema %s", source_name, source_schema)

    for sequence_key in sorted(source_sequences.keys()):
        source_name_value, source_next_value = source_sequences[sequence_key]
        postgres_entry = postgres_sequences.get(sequence_key)
        link = sequence_links.get(sequence_key, ("", ""))
        table_name, column_name = link

        if _is_sequence_excluded(exclude, table_name, sequence_key):
            logger.debug(
                "Skipping sequence %s (excluded table %s)",
                source_name_value,
                table_name or _table_from_sequence_name(sequence_key) or "?",
            )
            results.append(
                SequenceSyncResult(
                    source_sequence=source_name_value,
                    postgres_sequence=(postgres_entry[0] if postgres_entry else ""),
                    source_next_value=source_next_value,
                    postgres_old_value=(postgres_entry[1] if postgres_entry else None),
                    postgres_new_value=None,
                    linked_table=table_name,
                    linked_column=column_name,
                    max_column_value=None,
                    status="SKIPPED",
                    error_message="Table excluded via migrate.exclude_tables",
                    source_kind="sequence",
                )
            )
            continue

        if not postgres_entry:
            logger.warning(
                "No matching PostgreSQL sequence for %s sequence %s",
                source_name,
                source_name_value,
            )
            results.append(
                SequenceSyncResult(
                    source_sequence=source_name_value,
                    postgres_sequence="",
                    source_next_value=source_next_value,
                    postgres_old_value=None,
                    postgres_new_value=None,
                    linked_table="",
                    linked_column="",
                    max_column_value=None,
                    status="SKIPPED",
                    error_message="No matching PostgreSQL sequence",
                    source_kind="sequence",
                )
            )
            continue

        postgres_name, postgres_old_value = postgres_entry

        try:
            results.append(
                _sync_postgres_sequence(
                    postgres_connection,
                    schema,
                    source_name_value,
                    source_next_value,
                    postgres_name,
                    postgres_old_value,
                    table_name,
                    column_name,
                    "sequence",
                )
            )
            synced_postgres_sequences.add(sequence_key)
        except Exception as exc:
            postgres_connection.rollback()
            logger.exception("Failed to sync sequence %s", source_name_value)
            results.append(
                SequenceSyncResult(
                    source_sequence=source_name_value,
                    postgres_sequence=postgres_name,
                    source_next_value=source_next_value,
                    postgres_old_value=postgres_old_value,
                    postgres_new_value=None,
                    linked_table=table_name,
                    linked_column=column_name,
                    max_column_value=None,
                    status="FAILED",
                    error_message=str(exc),
                    source_kind="sequence",
                )
            )

    if source_type == SOURCE_SQLSERVER:
        _sync_sqlserver_identity_columns(
            source_connection,
            postgres_connection,
            config,
            schema,
            postgres_sequences,
            sequence_links,
            synced_postgres_sequences,
            results,
            exclude=exclude,
        )

    success_count = len([r for r in results if r.status == "SUCCESS"])
    identity_success = len([
        r for r in results if r.status == "SUCCESS" and r.source_kind == "identity"
    ])
    logger.info(
        "Sequence sync finished: %s updated (%s identity), %s skipped, %s failed",
        success_count,
        identity_success,
        len([r for r in results if r.status == "SKIPPED"]),
        len([r for r in results if r.status == "FAILED"]),
    )
    return results
