import csv
import logging
from pathlib import Path

import pyodbc

logger = logging.getLogger(__name__)

STRING_TYPES = set([
    "char",
    "nchar",
    "varchar",
    "nvarchar",
    "text",
    "ntext",
    "sysname",
])


def connect_sqlserver(config):
    return pyodbc.connect(config.build_connection_string(), autocommit=False)


def get_sqlserver_tables(connection, schema):
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


def get_sqlserver_columns_ordered(connection, schema, table):
    sql = """
        SELECT COLUMN_NAME, DATA_TYPE
        FROM INFORMATION_SCHEMA.COLUMNS
        WHERE TABLE_SCHEMA = ?
          AND TABLE_NAME = ?
        ORDER BY ORDINAL_POSITION
    """
    with connection.cursor() as cursor:
        cursor.execute(sql, schema, table)
        return [(row[0], (row[1] or "").lower()) for row in cursor.fetchall()]


def get_sqlserver_column_types(connection, schema, table):
    return {
        name: data_type
        for name, data_type in get_sqlserver_columns_ordered(connection, schema, table)
    }


def get_sqlserver_column_meta(connection, schema, table):
    """Return {column_name: (data_type, max_length_or_None)} using catalog names."""
    sql = """
        SELECT COLUMN_NAME, DATA_TYPE, CHARACTER_MAXIMUM_LENGTH
        FROM INFORMATION_SCHEMA.COLUMNS
        WHERE TABLE_SCHEMA = ?
          AND TABLE_NAME = ?
        ORDER BY ORDINAL_POSITION
    """
    meta = {}
    with connection.cursor() as cursor:
        cursor.execute(sql, schema, table)
        for name, data_type, max_len in cursor.fetchall():
            length = None
            # varchar(max)/nvarchar(max) report -1; ignore those for truncation.
            if max_len is not None and int(max_len) > 0:
                length = int(max_len)
            entry = ((data_type or "").lower(), length)
            meta[name] = entry
            meta[name.lower()] = entry
    return meta


def get_identity_column(connection, schema, table):
    sql = """
        SELECT c.name
        FROM sys.tables t
        INNER JOIN sys.schemas s ON t.schema_id = s.schema_id
        INNER JOIN sys.columns c ON c.object_id = t.object_id
        INNER JOIN sys.identity_columns ic
            ON ic.object_id = c.object_id
           AND ic.column_id = c.column_id
        WHERE s.name = ?
          AND t.name = ?
    """
    with connection.cursor() as cursor:
        cursor.execute(sql, schema, table)
        row = cursor.fetchone()
        return row[0] if row else None


def get_table_row_count(connection, schema, table):
    sql = "SELECT COUNT(*) FROM [{}].[{}]".format(
        schema.replace("]", "]]"),
        table.replace("]", "]]"),
    )
    with connection.cursor() as cursor:
        cursor.execute(sql)
        return int(cursor.fetchone()[0])


def _quote_ident(name):
    return "[{}]".format(name.replace("]", "]]"))


def _lookup_meta(column_meta, column_name):
    entry = column_meta.get(column_name)
    if entry is None:
        entry = column_meta.get(column_name.lower())
    if entry is None:
        return "", None
    return entry


def _parse_cell(
    raw,
    null_marker,
    data_type,
    max_length=None,
    truncate_warnings=None,
    column_name=None,
):
    if raw is None:
        return None
    text = raw
    if text == "" or text == null_marker:
        return None

    data_type = (data_type or "").lower()
    if data_type in ("int", "bigint", "smallint", "tinyint"):
        try:
            return int(float(text))
        except ValueError:
            return text
    if data_type in ("float", "real", "decimal", "numeric", "money", "smallmoney"):
        try:
            return float(text)
        except ValueError:
            return text
    if data_type == "bit":
        lowered = text.strip().lower()
        if lowered in ("1", "true", "t", "yes", "y"):
            return 1
        if lowered in ("0", "false", "f", "no", "n"):
            return 0
        return text
    if data_type in ("varbinary", "binary", "image") and text.startswith("0x"):
        try:
            return pyodbc.Binary(bytes.fromhex(text[2:]))
        except ValueError:
            return text

    if (
        data_type in STRING_TYPES
        and max_length
        and isinstance(text, str)
        and len(text) > max_length
    ):
        if truncate_warnings is not None and column_name not in truncate_warnings:
            truncate_warnings.add(column_name)
            logger.warning(
                "Truncating column %s from %s to %s characters to fit SQL Server type",
                column_name,
                len(text),
                max_length,
            )
        return text[:max_length]
    return text


def _is_bind_or_truncation_error(exc):
    text = str(exc).lower()
    return (
        "hy104" in text
        or "invalid precision" in text
        or "right truncation" in text
        or "string data" in text
    )


def _executemany_with_fallback(cursor, insert_sql, batch, connection):
    """Try fast_executemany; on bind/truncation errors retry without it."""
    try:
        cursor.executemany(insert_sql, batch)
        connection.commit()
        return
    except Exception as exc:
        if not _is_bind_or_truncation_error(exc):
            raise
        connection.rollback()
        logger.warning(
            "fast_executemany insert failed (%s); retrying batch without it",
            exc,
        )
        try:
            cursor.fast_executemany = False
        except Exception:
            pass
        # Re-bind without setinputsizes; row-by-row if needed.
        try:
            cursor.executemany(insert_sql, batch)
            connection.commit()
            return
        except Exception:
            connection.rollback()
            for row in batch:
                cursor.execute(insert_sql, row)
            connection.commit()


def import_csv_to_table(
    connection,
    schema,
    table,
    input_file,
    config,
    skip_truncate=False,
):
    settings = config.export
    batch_size = config.import_.batch_size
    input_path = Path(input_file)

    with input_path.open("r", encoding=settings.encoding, newline="") as handle:
        reader = csv.reader(handle, delimiter=settings.delimiter)
        headers = next(reader, None)
        if not headers:
            raise ValueError("CSV file is empty: {}".format(input_path))

        column_meta = get_sqlserver_column_meta(connection, schema, table)
        column_types = {}
        column_lengths = {}
        for name in headers:
            data_type, max_len = _lookup_meta(column_meta, name)
            column_types[name] = data_type
            column_lengths[name] = max_len

        identity_column = get_identity_column(connection, schema, table)

        if not skip_truncate and config.migrate.truncate_before_import:
            from .mssql_prepare import clear_table

            clear_table(
                connection,
                schema,
                table,
                restart_identity=config.migrate.restart_identity,
            )

        placeholders = ", ".join("?" for _ in headers)
        column_list = ", ".join(_quote_ident(col) for col in headers)
        insert_sql = "INSERT INTO {}.{} ({}) VALUES ({})".format(
            _quote_ident(schema),
            _quote_ident(table),
            column_list,
            placeholders,
        )

        identity_on = False
        if identity_column and any(
            col.lower() == identity_column.lower() for col in headers
        ):
            with connection.cursor() as cursor:
                cursor.execute(
                    "SET IDENTITY_INSERT {}.{} ON".format(
                        _quote_ident(schema), _quote_ident(table)
                    )
                )
            identity_on = True
            connection.commit()

        cursor = connection.cursor()
        # Avoid setinputsizes — (SQL_WVARCHAR, n, 0) triggers HY104 on ODBC 17.
        # Truncate strings in Python instead; fall back if fast path still fails.
        try:
            cursor.fast_executemany = True
        except Exception:
            pass

        batch = []
        imported = 0
        truncate_warnings = set()
        try:
            for row in reader:
                values = [
                    _parse_cell(
                        row[idx] if idx < len(row) else "",
                        settings.null_marker,
                        column_types.get(headers[idx]),
                        max_length=column_lengths.get(headers[idx]),
                        truncate_warnings=truncate_warnings,
                        column_name=headers[idx],
                    )
                    for idx in range(len(headers))
                ]
                batch.append(values)
                if len(batch) >= batch_size:
                    _executemany_with_fallback(
                        cursor, insert_sql, batch, connection
                    )
                    imported += len(batch)
                    batch = []

            if batch:
                _executemany_with_fallback(cursor, insert_sql, batch, connection)
                imported += len(batch)
        finally:
            if identity_on:
                with connection.cursor() as id_cursor:
                    id_cursor.execute(
                        "SET IDENTITY_INSERT {}.{} OFF".format(
                            _quote_ident(schema), _quote_ident(table)
                        )
                    )
                connection.commit()
            cursor.close()

    logger.info("Imported %s rows into %s.%s", imported, schema, table)
    return imported
