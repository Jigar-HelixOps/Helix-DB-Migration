import csv
import logging
from pathlib import Path

import psycopg2
from psycopg2 import sql

from .config import AppConfig, ImportSettings, PostgresConfig

logger = logging.getLogger(__name__)


def split_qualified_name(name):
    parts = [part.strip() for part in name.split(".") if part.strip()]
    if len(parts) == 1:
        return None, parts[0]
    if len(parts) == 2:
        return parts[0], parts[1]
    raise ValueError("Invalid table name '{}'. Use schema.table or table.".format(name))


def resolve_postgres_table(table_name, default_schema):
    schema, table = split_qualified_name(table_name)
    if schema is None:
        if not default_schema:
            raise ValueError(
                "Table '{}' has no schema. Set postgres.schema in config or use schema.table.".format(
                    table_name
                )
            )
        schema = default_schema
    return schema, table


def connect_postgres(config):
    return psycopg2.connect(
        host=config.host,
        port=config.port,
        dbname=config.database,
        user=config.user,
        password=config.password,
    )


def get_postgres_column_types(connection, schema, table):
    return {
        name.lower(): data_type
        for name, data_type in get_postgres_columns_ordered(connection, schema, table)
    }


def get_postgres_columns_ordered(connection, schema, table):
    sql = """
        SELECT column_name, data_type, udt_name
        FROM information_schema.columns
        WHERE table_schema = %s
          AND table_name = %s
        ORDER BY ordinal_position
    """
    with connection.cursor() as cursor:
        cursor.execute(sql, (schema, table))
        return [
            (row[0], (row[1] or "").lower())
            for row in cursor.fetchall()
        ]


def read_csv_header(path, delimiter, encoding):
    with path.open("r", encoding=encoding, newline="") as handle:
        reader = csv.reader(handle, delimiter=delimiter)
        header = next(reader, None)
        if not header:
            raise ValueError("CSV file is empty: {}".format(path))
        return header




def _count_csv_rows(path, delimiter, encoding):
    with path.open("r", encoding=encoding, newline="") as handle:
        reader = csv.reader(handle, delimiter=delimiter)
        next(reader, None)
        return sum(1 for _ in reader)


def import_csv_copy(connection, schema, table, input_file, settings, export_settings):
    columns = read_csv_header(input_file, export_settings.delimiter, export_settings.encoding)
    delimiter = export_settings.delimiter.replace("'", "''")
    null_marker = export_settings.null_marker.replace("'", "''")
    copy_options = "FORMAT csv, HEADER true, DELIMITER '{}', NULL '{}'".format(
        delimiter, null_marker
    )

    copy_sql = sql.SQL("COPY {}.{} ({}) FROM STDIN WITH ({})").format(
        sql.Identifier(schema),
        sql.Identifier(table),
        sql.SQL(", ").join(sql.Identifier(column) for column in columns),
        sql.SQL(copy_options),
    )

    try:
        with connection.cursor() as cursor:
            with input_file.open("r", encoding=export_settings.encoding, newline="") as handle:
                cursor.copy_expert(copy_sql.as_string(connection), handle)
        connection.commit()
    except Exception:
        connection.rollback()
        raise

    row_count = _count_csv_rows(
        input_file, export_settings.delimiter, export_settings.encoding
    )
    logger.info("Imported %s rows into %s.%s using COPY", row_count, schema, table)
    return row_count


def import_csv_insert(connection, schema, table, input_file, settings, export_settings):
    columns = read_csv_header(input_file, export_settings.delimiter, export_settings.encoding)
    placeholders = sql.SQL(", ").join(sql.Placeholder() * len(columns))
    insert_sql = sql.SQL("INSERT INTO {}.{} ({}) VALUES ({})").format(
        sql.Identifier(schema),
        sql.Identifier(table),
        sql.SQL(", ").join(sql.Identifier(column) for column in columns),
        placeholders,
    )

    row_count = 0
    batch = []

    try:
        with input_file.open("r", encoding=export_settings.encoding, newline="") as handle:
            reader = csv.DictReader(handle, delimiter=export_settings.delimiter)
            for row in reader:
                values = [
                    None if value == export_settings.null_marker else value
                    for value in (row.get(column) for column in columns)
                ]
                batch.append(values)

                if len(batch) >= settings.batch_size:
                    with connection.cursor() as cursor:
                        cursor.executemany(insert_sql, batch)
                    row_count += len(batch)
                    batch = []

            if batch:
                with connection.cursor() as cursor:
                    cursor.executemany(insert_sql, batch)
                row_count += len(batch)

        connection.commit()
    except Exception:
        connection.rollback()
        raise

    logger.info("Imported %s rows into %s.%s using INSERT", row_count, schema, table)
    return row_count


def get_table_row_count(connection, schema, table):
    query = sql.SQL("SELECT COUNT(*) FROM {}.{}").format(
        sql.Identifier(schema),
        sql.Identifier(table),
    )
    with connection.cursor() as cursor:
        cursor.execute(query)
        return int(cursor.fetchone()[0])


def import_table_from_csv(
    connection,
    schema,
    table,
    input_file,
    config,
    columns=None,
    skip_truncate=False,
    truncate_cascade=None,
):
    if not input_file.exists():
        raise FileNotFoundError("Input file not found: {}".format(input_file))

    try:
        if config.import_.truncate_before_import and not skip_truncate:
            from .discover import TablePair
            from .pg_prepare import clear_tables_for_migration

            migrate = config.migrate
            if truncate_cascade is not None:
                original_cascade = migrate.truncate_cascade
                migrate.truncate_cascade = truncate_cascade
            else:
                original_cascade = None

            try:
                clear_tables_for_migration(
                    connection,
                    [TablePair("", "", schema, table)],
                    migrate,
                    table_count=1,
                )
            finally:
                if original_cascade is not None:
                    migrate.truncate_cascade = original_cascade

        if config.import_.use_copy:
            return import_csv_copy(
                connection, schema, table, input_file, config.import_, config.export
            )

        return import_csv_insert(
            connection, schema, table, input_file, config.import_, config.export
        )
    except Exception:
        connection.rollback()
        raise
