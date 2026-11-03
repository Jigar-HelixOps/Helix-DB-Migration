import logging

from .export_oracle import get_oracle_sequences
from .import_mssql import get_identity_column, get_table_row_count

logger = logging.getLogger(__name__)


class IdentitySyncResult(object):
    def __init__(
        self,
        source_sequence,
        sqlserver_table,
        identity_column,
        source_next_value,
        max_column_value,
        reseed_value,
        status,
        error_message=None,
    ):
        self.source_sequence = source_sequence
        self.sqlserver_table = sqlserver_table
        self.identity_column = identity_column
        self.source_next_value = source_next_value
        self.max_column_value = max_column_value
        self.reseed_value = reseed_value
        self.status = status
        self.error_message = error_message
        # Aliases for shared report helpers
        self.postgres_sequence = sqlserver_table
        self.postgres_old_value = None
        self.postgres_new_value = reseed_value
        self.linked_table = sqlserver_table
        self.linked_column = identity_column
        self.source_kind = "sequence"


def _excluded_tables(config):
    return {name.lower() for name in (config.migrate.exclude_tables or [])}


def _table_from_sequence_name(sequence_key):
    key = (sequence_key or "").lower()
    if key.startswith("seq_"):
        return key[4:]
    return None


def _get_max_identity_value(connection, schema, table, column):
    sql = "SELECT COALESCE(MAX([{}]), 0) FROM [{}].[{}]".format(
        column.replace("]", "]]"),
        schema.replace("]", "]]"),
        table.replace("]", "]]"),
    )
    with connection.cursor() as cursor:
        cursor.execute(sql)
        return int(cursor.fetchone()[0])


def _reseed_identity(connection, schema, table, reseed_value):
    # DBCC CHECKIDENT RESEED sets the last used value; next insert is reseed+increment.
    sql = "DBCC CHECKIDENT ('{}.{}', RESEED, {})".format(
        schema.replace("'", "''"),
        table.replace("'", "''"),
        int(reseed_value),
    )
    with connection.cursor() as cursor:
        cursor.execute(sql)
    connection.commit()


def get_sqlserver_identity_tables(connection, schema):
    sql = """
        SELECT t.name, c.name
        FROM sys.tables t
        INNER JOIN sys.schemas s ON t.schema_id = s.schema_id
        INNER JOIN sys.columns c ON c.object_id = t.object_id
        INNER JOIN sys.identity_columns ic
            ON ic.object_id = c.object_id
           AND ic.column_id = c.column_id
        WHERE s.name = ?
        ORDER BY t.name
    """
    identities = {}
    with connection.cursor() as cursor:
        cursor.execute(sql, schema)
        for table_name, column_name in cursor.fetchall():
            identities[table_name.lower()] = (table_name, column_name)
    return identities


def sync_identities(oracle_connection, mssql_connection, config):
    oracle_schema = config.oracle.schema
    mssql_schema = config.sqlserver.schema
    exclude = _excluded_tables(config)
    results = []

    logger.info(
        "Starting identity sync: Oracle schema %s -> SQL Server schema %s",
        oracle_schema,
        mssql_schema,
    )
    if exclude:
        logger.info(
            "Excluding %s table(s) from identity sync: %s",
            len(exclude),
            ", ".join(sorted(exclude)),
        )

    oracle_sequences = get_oracle_sequences(oracle_connection, oracle_schema)
    mssql_identities = get_sqlserver_identity_tables(mssql_connection, mssql_schema)

    logger.info(
        "Found %s Oracle sequence(s) and %s SQL Server IDENTITY table(s)",
        len(oracle_sequences),
        len(mssql_identities),
    )

    for sequence_key in sorted(oracle_sequences.keys()):
        sequence_name, source_next = oracle_sequences[sequence_key]
        table_key = _table_from_sequence_name(sequence_key)
        if not table_key:
            results.append(
                IdentitySyncResult(
                    source_sequence=sequence_name,
                    sqlserver_table="",
                    identity_column="",
                    source_next_value=source_next,
                    max_column_value=None,
                    reseed_value=None,
                    status="SKIPPED",
                    error_message="Cannot infer table from sequence name",
                )
            )
            continue

        if table_key in exclude:
            results.append(
                IdentitySyncResult(
                    source_sequence=sequence_name,
                    sqlserver_table=table_key,
                    identity_column="",
                    source_next_value=source_next,
                    max_column_value=None,
                    reseed_value=None,
                    status="SKIPPED",
                    error_message="Table excluded via migrate.exclude_tables",
                )
            )
            continue

        identity_entry = mssql_identities.get(table_key)
        if not identity_entry:
            results.append(
                IdentitySyncResult(
                    source_sequence=sequence_name,
                    sqlserver_table=table_key,
                    identity_column="",
                    source_next_value=source_next,
                    max_column_value=None,
                    reseed_value=None,
                    status="SKIPPED",
                    error_message="No matching SQL Server IDENTITY table",
                )
            )
            continue

        table_name, column_name = identity_entry
        try:
            max_col = _get_max_identity_value(
                mssql_connection, mssql_schema, table_name, column_name
            )
            target_next = max(source_next, max_col + 1)
            # Next insert should be target_next => reseed last-used = target_next - 1
            reseed_value = max(target_next - 1, 0)
            _reseed_identity(mssql_connection, mssql_schema, table_name, reseed_value)
            logger.info(
                "Synced %s -> %s.%s (%s): source_next=%s max_col=%s reseed=%s",
                sequence_name,
                mssql_schema,
                table_name,
                column_name,
                source_next,
                max_col,
                reseed_value,
            )
            results.append(
                IdentitySyncResult(
                    source_sequence=sequence_name,
                    sqlserver_table=table_name,
                    identity_column=column_name,
                    source_next_value=source_next,
                    max_column_value=max_col,
                    reseed_value=reseed_value,
                    status="SUCCESS",
                )
            )
        except Exception as exc:
            mssql_connection.rollback()
            logger.exception("Failed to sync identity for %s", sequence_name)
            results.append(
                IdentitySyncResult(
                    source_sequence=sequence_name,
                    sqlserver_table=table_name,
                    identity_column=column_name,
                    source_next_value=source_next,
                    max_column_value=None,
                    reseed_value=None,
                    status="FAILED",
                    error_message=str(exc),
                )
            )

    success = len([r for r in results if r.status == "SUCCESS"])
    skipped = len([r for r in results if r.status == "SKIPPED"])
    failed = len([r for r in results if r.status == "FAILED"])
    logger.info(
        "Identity sync finished: %s updated, %s skipped, %s failed",
        success,
        skipped,
        failed,
    )
    return results
