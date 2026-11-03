import logging

from .export_oracle import connect_oracle, get_oracle_tables
from .import_mssql import connect_sqlserver, get_sqlserver_tables

logger = logging.getLogger(__name__)


class TablePair(object):
    def __init__(
        self, oracle_schema, oracle_table, sqlserver_schema, sqlserver_table
    ):
        self.oracle_schema = oracle_schema
        self.oracle_table = oracle_table
        self.sqlserver_schema = sqlserver_schema
        self.sqlserver_table = sqlserver_table

    @property
    def source_schema(self):
        return self.oracle_schema

    @property
    def source_table(self):
        return self.oracle_table

    @property
    def name(self):
        return self.oracle_table


def resolve_table_pairs(config):
    # Oracle unquoted identifiers are stored uppercase; quoted SQL must match.
    oracle_schema = (config.oracle.schema or "").upper()
    sqlserver_schema = config.sqlserver.schema
    if not oracle_schema:
        raise ValueError("oracle.schema is required for automatic table discovery.")
    if not sqlserver_schema:
        raise ValueError("sqlserver.schema is required for automatic table discovery.")

    with connect_oracle(config.oracle) as oracle_conn:
        with connect_sqlserver(config.sqlserver) as mssql_conn:
            oracle_tables = get_oracle_tables(oracle_conn, oracle_schema)
            sqlserver_tables = get_sqlserver_tables(mssql_conn, sqlserver_schema)

    include = {name.lower() for name in config.migrate.tables}
    exclude = {name.lower() for name in config.migrate.exclude_tables}

    if include:
        candidate_names = sorted(include)
    else:
        candidate_names = sorted(oracle_tables.keys())

    pairs = []
    for name in candidate_names:
        if name in exclude:
            logger.info("Skipping excluded table: %s", name)
            continue

        oracle_table = oracle_tables.get(name)
        if not oracle_table:
            logger.warning(
                "Table '%s' not found in Oracle schema %s",
                name,
                oracle_schema,
            )
            continue

        sqlserver_table = sqlserver_tables.get(name)
        if not sqlserver_table:
            logger.warning(
                "Table '%s' not found in SQL Server schema %s; skipping",
                name,
                sqlserver_schema,
            )
            continue

        pairs.append(
            TablePair(
                oracle_schema=oracle_schema,
                oracle_table=oracle_table,
                sqlserver_schema=sqlserver_schema,
                sqlserver_table=sqlserver_table,
            )
        )

    if not pairs:
        raise ValueError(
            "No matching tables found to migrate. "
            "Check oracle.schema, sqlserver.schema, and that tables exist in both databases."
        )

    return pairs
