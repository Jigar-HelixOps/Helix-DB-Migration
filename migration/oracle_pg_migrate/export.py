import logging

import oracledb

from .export_common import (
    default_export_path,
    format_cell,
    split_qualified_name,
    write_csv_rows,
)

logger = logging.getLogger(__name__)

ORACLE_BINARY_TYPES = set([
    "BLOB",
    "RAW",
    "LONG RAW",
    "BFILE",
])


def resolve_oracle_table(table_name, default_schema):
    schema, table = split_qualified_name(table_name)
    if schema is None:
        if not default_schema:
            raise ValueError(
                "Table '{}' has no schema. Set oracle.schema in config or use SCHEMA.TABLE.".format(
                    table_name
                )
            )
        schema = default_schema
    return schema.upper(), table.upper()


def connect_oracle(config):
    return oracledb.connect(user=config.user, password=config.password, dsn=config.dsn)


def _get_source_column_types(connection, schema, table):
    sql = """
        SELECT column_name, data_type
        FROM all_tab_columns
        WHERE owner = :owner
          AND table_name = :table_name
        ORDER BY column_id
    """
    with connection.cursor() as cursor:
        cursor.execute(sql, owner=schema.upper(), table_name=table.upper())
        return {row[0].lower(): (row[1] or "").upper() for row in cursor.fetchall()}


def get_binary_column_indexes(connection, schema, table, source_column_names=None):
    column_types = _get_source_column_types(connection, schema, table)
    if source_column_names:
        indexes = set()
        for idx, column_name in enumerate(source_column_names):
            data_type = column_types.get(column_name.lower(), "")
            if data_type in ORACLE_BINARY_TYPES:
                indexes.add(idx)
        return indexes

    indexes = set()
    for idx, data_type in enumerate(column_types.values()):
        if data_type in ORACLE_BINARY_TYPES:
            indexes.add(idx)
    return indexes


def _read_lob_value(lob):
    data = lob.read()
    if data is None:
        return None
    if isinstance(data, (bytes, bytearray, memoryview)):
        return "\\x" + bytes(data).hex()
    return str(data)


def _format_oracle_cell(value, null_marker, is_binary=False, pg_type=None):
    if is_binary and isinstance(value, oracledb.LOB):
        data = value.read()
        if data is None:
            return null_marker
        if isinstance(data, (bytes, bytearray, memoryview)):
            return "\\x" + bytes(data).hex()
        return str(data)

    if isinstance(value, oracledb.LOB):
        return _read_lob_value(value)

    return format_cell(value, null_marker, is_binary=is_binary, pg_type=pg_type)


def build_select_sql(schema, table, columns=None, where=None):
    column_sql = ", ".join(columns) if columns else "*"
    sql = 'SELECT {} FROM "{}"."{}"'.format(column_sql, schema, table)
    if where:
        sql += " WHERE {}".format(where)
    return sql


def _iter_oracle_rows(cursor, fetch_size):
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
        sql = 'SELECT {} FROM "{}"."{}"'.format(select_columns, schema, table)
        if where:
            sql += " WHERE {}".format(where)
    else:
        sql = build_select_sql(schema, table, columns=columns, where=where)
    logger.info("Exporting %s.%s -> %s", schema, table, output_file)

    column_types = column_types or {}

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
        cursor.arraysize = settings.fetch_size
        cursor.execute(sql)
        if column_names is None:
            column_names = [desc[0] for desc in cursor.description]
            if lowercase_columns:
                column_names = [name.lower() for name in column_names]

        def formatted_rows():
            for row in _iter_oracle_rows(cursor, settings.fetch_size):
                yield [
                    _format_oracle_cell(
                        value,
                        settings.null_marker,
                        is_binary=(idx in binary_columns),
                        pg_type=column_types.get(column_names[idx]),
                    )
                    for idx, value in enumerate(row)
                ]

        row_count = write_csv_rows(
            output_file,
            settings,
            column_names,
            formatted_rows(),
            binary_column_indexes=binary_columns,
            column_types=column_types,
            preformatted=True,
        )

    logger.info("Exported %s rows from %s.%s", row_count, schema, table)
    return row_count
