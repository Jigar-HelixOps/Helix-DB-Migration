import logging

from .export_common import default_export_path, write_csv_rows

logger = logging.getLogger(__name__)

SQLSERVER_BINARY_TYPES = set([
    "binary",
    "varbinary",
    "image",
    "timestamp",
])


def connect_sqlserver(config):
    try:
        import pyodbc
    except ImportError:
        raise ImportError(
            "pyodbc is required for SQL Server source. Install with: pip install pyodbc"
        )

    connection_string = config.build_connection_string()
    logger.debug("SQL Server connection string: %s", connection_string)
    return pyodbc.connect(connection_string)


def _get_source_column_types(connection, schema, table):
    sql = """
        SELECT COLUMN_NAME, DATA_TYPE
        FROM INFORMATION_SCHEMA.COLUMNS
        WHERE TABLE_SCHEMA = ?
          AND TABLE_NAME = ?
        ORDER BY ORDINAL_POSITION
    """
    with connection.cursor() as cursor:
        cursor.execute(sql, schema, table)
        return {row[0].lower(): (row[1] or "").lower() for row in cursor.fetchall()}


def get_binary_column_indexes(connection, schema, table, source_column_names=None):
    column_types = _get_source_column_types(connection, schema, table)
    if source_column_names:
        indexes = set()
        for idx, column_name in enumerate(source_column_names):
            data_type = column_types.get(column_name.lower(), "")
            if data_type in SQLSERVER_BINARY_TYPES:
                indexes.add(idx)
        return indexes

    indexes = set()
    for idx, data_type in enumerate(column_types.values()):
        if data_type in SQLSERVER_BINARY_TYPES:
            indexes.add(idx)
    return indexes


def build_select_sql(schema, table, columns=None, where=None):
    column_sql = ", ".join(columns) if columns else "*"
    sql = "SELECT {} FROM [{}].[{}]".format(column_sql, schema, table)
    if where:
        sql += " WHERE {}".format(where)
    return sql


def _iter_rows(cursor, fetch_size):
    while True:
        rows = cursor.fetchmany(fetch_size)
        if not rows:
            break
        for row in rows:
            yield row


def export_table_to_csv(
    connection,
    schema,
    table,
    output_file,
    settings,
    columns=None,
    where=None,
    lowercase_columns=False,
    column_types=None,
    select_columns=None,
    csv_headers=None,
    source_column_names=None,
):
    if select_columns:
        sql = "SELECT {} FROM [{}].[{}]".format(select_columns, schema, table)
        if where:
            sql += " WHERE {}".format(where)
    else:
        sql = build_select_sql(schema, table, columns=columns, where=where)
    logger.info("Exporting %s.%s -> %s", schema, table, output_file)

    if csv_headers:
        column_names = list(csv_headers)
    else:
        column_names = None

    binary_columns = get_binary_column_indexes(
        connection, schema, table, source_column_names=source_column_names
    )
    if binary_columns:
        logger.info(
            "Binary columns detected in %s.%s at indexes: %s",
            schema,
            table,
            sorted(binary_columns),
        )

    with connection.cursor() as cursor:
        cursor.execute(sql)
        if column_names is None:
            column_names = [desc[0] for desc in cursor.description]
            if lowercase_columns:
                column_names = [name.lower() for name in column_names]

        row_count = write_csv_rows(
            output_file,
            settings,
            column_names,
            _iter_rows(cursor, settings.fetch_size),
            binary_column_indexes=binary_columns,
            column_types=column_types or {},
        )

    logger.info("Exported %s rows from %s.%s", row_count, schema, table)
    return row_count
