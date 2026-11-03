import logging

from .import_pg import connect_postgres
from .source_db import connect_source, get_source_schema, get_source_tables, source_display_name

logger = logging.getLogger(__name__)


class TablePair(object):
    def __init__(self, source_schema, source_table, postgres_schema, postgres_table):
        self.source_schema = source_schema
        self.source_table = source_table
        self.postgres_schema = postgres_schema
        self.postgres_table = postgres_table

    @property
    def oracle_schema(self):
        return self.source_schema

    @property
    def oracle_table(self):
        return self.source_table

    @property
    def name(self):
        return self.source_table


def get_postgres_tables(connection, schema):
    sql = """
        SELECT tablename
        FROM pg_catalog.pg_tables
        WHERE schemaname = %s
        ORDER BY tablename
    """
    with connection.cursor() as cursor:
        cursor.execute(sql, (schema,))
        return {row[0].lower(): row[0] for row in cursor.fetchall()}


def resolve_table_pairs(config):
    source_schema = get_source_schema(config)
    if not source_schema:
        raise ValueError(
            "{}.schema is required for automatic table discovery.".format(
                source_display_name(config)
            )
        )
    if not config.postgres.schema:
        raise ValueError("postgres.schema is required for automatic table discovery.")

    with connect_source(config) as source_conn:
        with connect_postgres(config.postgres) as pg_conn:
            source_tables = get_source_tables(source_conn, config)
            postgres_tables = get_postgres_tables(pg_conn, config.postgres.schema)

    include = {name.lower() for name in config.migrate.tables}
    exclude = {name.lower() for name in config.migrate.exclude_tables}

    if include:
        candidate_names = sorted(include)
    else:
        candidate_names = sorted(source_tables.keys())

    pairs = []
    source_name = source_display_name(config)
    for name in candidate_names:
        if name in exclude:
            logger.info("Skipping excluded table: %s", name)
            continue

        source_table = source_tables.get(name)
        if not source_table:
            logger.warning(
                "Table '%s' not found in %s schema %s",
                name,
                source_name,
                source_schema,
            )
            continue

        postgres_table = postgres_tables.get(name)
        if not postgres_table:
            logger.warning(
                "Table '%s' not found in PostgreSQL schema %s; skipping",
                name,
                config.postgres.schema,
            )
            continue

        pairs.append(
            TablePair(
                source_schema=source_schema,
                source_table=source_table,
                postgres_schema=config.postgres.schema,
                postgres_table=postgres_table,
            )
        )

    if not pairs:
        raise ValueError(
            "No matching tables found to migrate. "
            "Check source schema, postgres.schema, and that tables exist in both databases."
        )

    return pairs
