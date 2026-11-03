import logging

from .export import connect_oracle, export_table_to_csv as export_oracle_table_to_csv
from .export_common import default_export_path
from .export_sqlserver import connect_sqlserver, export_table_to_csv as export_sqlserver_table_to_csv
from .source_types import SOURCE_ORACLE, SOURCE_SQLSERVER, normalize_source_type

logger = logging.getLogger(__name__)


def get_source_schema(config):
    source_type = normalize_source_type(config.source.type)
    if source_type == SOURCE_SQLSERVER:
        return config.sqlserver.schema
    if config.oracle.schema:
        return config.oracle.schema.upper()
    return config.oracle.schema


def connect_source(config):
    source_type = normalize_source_type(config.source.type)
    if source_type == SOURCE_SQLSERVER:
        return connect_sqlserver(config.sqlserver)
    return connect_oracle(config.oracle)


def reset_source_connection(connection, config):
    source_type = normalize_source_type(config.source.type)
    if source_type != SOURCE_SQLSERVER:
        return

    try:
        connection.rollback()
    except Exception as exc:
        logger.debug("Could not rollback SQL Server connection: %s", exc)


def export_table_to_csv(
    connection,
    config,
    schema,
    table,
    output_file,
    lowercase_columns=False,
    column_types=None,
    select_columns=None,
    csv_headers=None,
    source_column_names=None,
):
    source_type = normalize_source_type(config.source.type)
    export_kwargs = {
        "connection": connection,
        "schema": schema,
        "table": table,
        "output_file": output_file,
        "settings": config.export,
        "lowercase_columns": lowercase_columns,
        "column_types": column_types,
        "select_columns": select_columns,
        "csv_headers": csv_headers,
        "source_column_names": source_column_names,
    }
    if source_type == SOURCE_SQLSERVER:
        return export_sqlserver_table_to_csv(**export_kwargs)
    return export_oracle_table_to_csv(**export_kwargs)


def get_source_tables(connection, config):
    source_type = normalize_source_type(config.source.type)
    schema = get_source_schema(config)

    if source_type == SOURCE_SQLSERVER:
        return _get_sqlserver_tables(connection, schema)
    return _get_oracle_tables(connection, schema)


def _get_oracle_tables(connection, schema):
    sql = """
        SELECT table_name
        FROM all_tables
        WHERE owner = :owner
        ORDER BY table_name
    """
    with connection.cursor() as cursor:
        cursor.execute(sql, owner=schema.upper())
        return {row[0].lower(): row[0] for row in cursor.fetchall()}


def _get_sqlserver_tables(connection, schema):
    sql = """
        SELECT TABLE_NAME
        FROM INFORMATION_SCHEMA.TABLES
        WHERE TABLE_SCHEMA = ?
          AND TABLE_TYPE = 'BASE TABLE'
        ORDER BY TABLE_NAME
    """
    with connection.cursor() as cursor:
        cursor.execute(sql, schema)
        return {row[0].lower(): row[0] for row in cursor.fetchall()}


def get_source_sequences(connection, config):
    source_type = normalize_source_type(config.source.type)
    schema = get_source_schema(config)

    if source_type == SOURCE_SQLSERVER:
        return _get_sqlserver_sequences(connection, schema)
    return _get_oracle_sequences(connection, schema)


def _get_oracle_sequences(connection, schema):
    query = """
        SELECT sequence_name, last_number
        FROM all_sequences
        WHERE sequence_owner = :owner
        ORDER BY sequence_name
    """
    with connection.cursor() as cursor:
        cursor.execute(query, owner=schema.upper())
        return {row[0].lower(): (row[0], int(row[1])) for row in cursor.fetchall()}


def _get_sqlserver_sequences(connection, schema):
    query = """
        SELECT
            s.name,
            CAST(s.current_value AS bigint) + CAST(s.increment AS bigint)
        FROM sys.sequences s
        INNER JOIN sys.schemas sch ON s.schema_id = sch.schema_id
        WHERE sch.name = ?
        ORDER BY s.name
    """
    with connection.cursor() as cursor:
        cursor.execute(query, schema)
        return {row[0].lower(): (row[0], int(row[1])) for row in cursor.fetchall()}


def _sqlserver_identity_next_value(last_value, seed_value, increment_value):
    seed = int(seed_value) if seed_value is not None else 1
    increment = int(increment_value) if increment_value is not None else 1

    if last_value is None:
        return seed

    last = int(last_value)
    if last == seed - increment:
        return seed
    return last + increment


def get_sqlserver_identity_columns(connection, schema):
    # last_value/seed_value are sql_variant; pyodbc cannot read type -150 directly.
    query = """
        SELECT
            t.name,
            c.name,
            TRY_CAST(ic.last_value AS bigint) AS last_value,
            TRY_CAST(ic.seed_value AS bigint) AS seed_value,
            TRY_CAST(ic.increment_value AS bigint) AS increment_value
        FROM sys.tables t
        INNER JOIN sys.schemas s ON t.schema_id = s.schema_id
        INNER JOIN sys.columns c ON t.object_id = c.object_id
        INNER JOIN sys.identity_columns ic
            ON c.object_id = ic.object_id
           AND c.column_id = ic.column_id
        WHERE s.name = ?
        ORDER BY t.name, c.name
    """
    identities = {}
    with connection.cursor() as cursor:
        cursor.execute(query, (schema,))
        for (
            table_name,
            column_name,
            last_value,
            seed_value,
            increment_value,
        ) in cursor.fetchall():
            key = (table_name.lower(), column_name.lower())
            next_value = _sqlserver_identity_next_value(
                last_value, seed_value, increment_value
            )
            identities[key] = (table_name, column_name, next_value)
    return identities


def source_label(config):
    return normalize_source_type(config.source.type)


def source_display_name(config):
    source_type = normalize_source_type(config.source.type)
    if source_type == SOURCE_SQLSERVER:
        return "SQL Server"
    return "Oracle"
