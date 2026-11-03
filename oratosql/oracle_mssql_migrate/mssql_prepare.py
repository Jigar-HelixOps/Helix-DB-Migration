import logging

from .import_mssql import get_sqlserver_tables

logger = logging.getLogger(__name__)


def _quote_ident(name):
    return "[{}]".format(name.replace("]", "]]"))


def _qualified(schema, table):
    return "{}.{}".format(_quote_ident(schema), _quote_ident(table))


class MssqlTableRef(object):
    def __init__(self, schema, table):
        self.sqlserver_schema = schema
        self.sqlserver_table = table


class MssqlSessionState(object):
    def __init__(self):
        self.fk_disabled_tables = []
        self.triggers_disabled_tables = []
        self.schema = None


def _is_link_failure(exc):
    text = str(exc).lower()
    return (
        "08s01" in text
        or "communication link failure" in text
        or "connection is busy" in text
        or "not connected" in text
    )


def _execute_batches(connection, statements, batch_size=100):
    """Execute many DDL/DML statements in fewer round-trips."""
    if not statements:
        return
    cursor = connection.cursor()
    try:
        for start in range(0, len(statements), batch_size):
            chunk = statements[start : start + batch_size]
            cursor.execute(";\n".join(chunk))
            connection.commit()
    finally:
        cursor.close()


def _schema_table_refs(connection, schema):
    tables = get_sqlserver_tables(connection, schema)
    return [MssqlTableRef(schema, table_name) for table_name in sorted(tables.values())]


def disable_schema_triggers(connection, schema_tables):
    statements = [
        "DISABLE TRIGGER ALL ON {}".format(
            _qualified(ref.sqlserver_schema, ref.sqlserver_table)
        )
        for ref in schema_tables
    ]
    logger.info("Disabling triggers on %s table(s) (batched)", len(statements))
    _execute_batches(connection, statements)
    logger.info("Disabled triggers on %s table(s)", len(statements))
    return list(schema_tables)


def enable_schema_triggers(connection, schema_tables):
    statements = [
        "ENABLE TRIGGER ALL ON {}".format(
            _qualified(ref.sqlserver_schema, ref.sqlserver_table)
        )
        for ref in schema_tables
    ]
    logger.info("Re-enabling triggers on %s table(s) (batched)", len(statements))
    _execute_batches(connection, statements)
    logger.info("Enabled triggers on %s table(s)", len(statements))


def disable_schema_foreign_keys(connection, schema_tables):
    statements = [
        "ALTER TABLE {} NOCHECK CONSTRAINT ALL".format(
            _qualified(ref.sqlserver_schema, ref.sqlserver_table)
        )
        for ref in schema_tables
    ]
    logger.info("Disabling FK checks on %s table(s) (batched)", len(statements))
    _execute_batches(connection, statements)
    logger.info("Disabled FK checks on %s table(s)", len(statements))
    return list(schema_tables)


def enable_schema_foreign_keys(connection, schema_tables):
    statements = [
        "ALTER TABLE {} WITH NOCHECK CHECK CONSTRAINT ALL".format(
            _qualified(ref.sqlserver_schema, ref.sqlserver_table)
        )
        for ref in schema_tables
    ]
    logger.info("Re-enabling FK checks on %s table(s) (batched)", len(statements))
    _execute_batches(connection, statements)
    logger.info("Re-enabled FK checks on %s table(s)", len(statements))


def _tables_referenced_by_fk(connection, schema):
    """Return lowercase table names that are referenced by any FK (blocks TRUNCATE)."""
    sql = """
        SELECT DISTINCT OBJECT_NAME(fk.referenced_object_id)
        FROM sys.foreign_keys fk
        INNER JOIN sys.tables t ON t.object_id = fk.referenced_object_id
        INNER JOIN sys.schemas s ON s.schema_id = t.schema_id
        WHERE s.name = ?
    """
    referenced = set()
    with connection.cursor() as cursor:
        cursor.execute(sql, schema)
        for row in cursor.fetchall():
            if row[0]:
                referenced.add(row[0].lower())
    return referenced


def clear_table(connection, schema, table, restart_identity=True):
    """Clear one table (used for per-table import fallback)."""
    qualified = _qualified(schema, table)
    try:
        with connection.cursor() as cursor:
            cursor.execute("TRUNCATE TABLE {}".format(qualified))
        connection.commit()
        logger.info("Truncated %s.%s", schema, table)
        return
    except Exception as exc:
        connection.rollback()
        logger.warning(
            "TRUNCATE failed for %s.%s (%s); using DELETE instead",
            schema,
            table,
            exc,
        )

    with connection.cursor() as cursor:
        cursor.execute("DELETE FROM {}".format(qualified))
    connection.commit()
    logger.info("Deleted all rows from %s.%s", schema, table)

    if restart_identity:
        _reseed_identity(connection, schema, table)


def _reseed_identity(connection, schema, table):
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "DBCC CHECKIDENT ('{}.{}', RESEED, 0)".format(
                    schema.replace("'", "''"),
                    table.replace("'", "''"),
                )
            )
        connection.commit()
    except Exception as exc:
        connection.rollback()
        logger.debug("IDENTITY reseed skipped for %s.%s: %s", schema, table, exc)


def clear_tables_for_migration(connection, pairs, migrate):
    if not pairs:
        return

    schema = pairs[0].sqlserver_schema
    referenced = _tables_referenced_by_fk(connection, schema)

    truncate_pairs = []
    delete_pairs = []
    for pair in pairs:
        if pair.sqlserver_table.lower() in referenced:
            delete_pairs.append(pair)
        else:
            truncate_pairs.append(pair)

    logger.info(
        "Clearing %s table(s) before import "
        "(%s TRUNCATE batched, %s DELETE batched — FK-referenced)",
        len(pairs),
        len(truncate_pairs),
        len(delete_pairs),
    )

    # SQL Server allows only one table per TRUNCATE statement.
    # Batch many single-table TRUNCATEs in one round-trip (same pattern as DELETE).
    truncate_statements = [
        "TRUNCATE TABLE {}".format(_qualified(p.sqlserver_schema, p.sqlserver_table))
        for p in truncate_pairs
    ]
    truncated = 0
    truncate_failed = []
    if truncate_statements:
        logger.info(
            "Truncating %s non-FK-referenced table(s) (batched)",
            len(truncate_statements),
        )
        try:
            _execute_batches(connection, truncate_statements, batch_size=50)
            truncated = len(truncate_statements)
        except Exception as exc:
            connection.rollback()
            logger.warning(
                "Batched TRUNCATE failed: %s; retrying in smaller groups",
                exc,
            )
            for start in range(0, len(truncate_pairs), 10):
                chunk = truncate_pairs[start : start + 10]
                statements = [
                    "TRUNCATE TABLE {}".format(
                        _qualified(p.sqlserver_schema, p.sqlserver_table)
                    )
                    for p in chunk
                ]
                try:
                    _execute_batches(connection, statements, batch_size=10)
                    truncated += len(chunk)
                except Exception:
                    connection.rollback()
                    for pair in chunk:
                        try:
                            with connection.cursor() as cursor:
                                cursor.execute(
                                    "TRUNCATE TABLE {}".format(
                                        _qualified(
                                            pair.sqlserver_schema, pair.sqlserver_table
                                        )
                                    )
                                )
                            connection.commit()
                            truncated += 1
                        except Exception as one_exc:
                            connection.rollback()
                            truncate_failed.append(pair)
                            logger.debug(
                                "TRUNCATE failed for %s.%s: %s",
                                pair.sqlserver_schema,
                                pair.sqlserver_table,
                                one_exc,
                            )

    if truncate_failed:
        delete_pairs.extend(truncate_failed)
        logger.info(
            "Moved %s table(s) from TRUNCATE to DELETE after failures",
            len(truncate_failed),
        )

    # Batch DELETE statements.
    delete_statements = [
        "DELETE FROM {}".format(_qualified(p.sqlserver_schema, p.sqlserver_table))
        for p in delete_pairs
    ]
    if delete_statements:
        logger.info("Deleting rows from %s FK-referenced table(s) (batched)", len(delete_statements))
        _execute_batches(connection, delete_statements, batch_size=50)

    if migrate.restart_identity and delete_pairs:
        reseed_statements = [
            "DBCC CHECKIDENT ('{}.{}', RESEED, 0)".format(
                p.sqlserver_schema.replace("'", "''"),
                p.sqlserver_table.replace("'", "''"),
            )
            for p in delete_pairs
        ]
        logger.info("Reseeding IDENTITY on %s table(s) (batched)", len(reseed_statements))
        try:
            _execute_batches(connection, reseed_statements, batch_size=50)
        except Exception as exc:
            connection.rollback()
            logger.warning("Batched IDENTITY reseed had errors (continuing): %s", exc)

    logger.info(
        "Clear complete: truncated=%s deleted=%s",
        truncated,
        len(delete_pairs),
    )


def prepare_sqlserver_for_migration(connection, config, pairs, state=None):
    """Prepare target DB. Mutates and returns state so callers can restore even on failure."""
    if state is None:
        state = MssqlSessionState()

    migrate = config.migrate
    schema = config.sqlserver.schema
    schema_tables = _schema_table_refs(connection, schema)
    state.schema = schema

    logger.info(
        "Preparing SQL Server for data migration (%s table(s) in schema %s)",
        len(schema_tables),
        schema,
    )

    if migrate.disable_triggers:
        try:
            state.triggers_disabled_tables = disable_schema_triggers(
                connection, schema_tables
            )
        except Exception as exc:
            connection.rollback()
            logger.warning("Batched trigger disable failed: %s", exc)
            raise

    if migrate.disable_foreign_keys:
        try:
            state.fk_disabled_tables = disable_schema_foreign_keys(
                connection, schema_tables
            )
        except Exception as exc:
            connection.rollback()
            logger.warning("Batched FK disable failed: %s", exc)
            raise

    if migrate.truncate_before_import and migrate.truncate_all_before_migration:
        clear_tables_for_migration(connection, pairs, migrate)

    return state


def restore_sqlserver_after_migration(connection, config, pairs, state):
    logger.info("Restoring SQL Server session settings after migration")
    if not state:
        return

    try:
        if state.fk_disabled_tables:
            enable_schema_foreign_keys(connection, state.fk_disabled_tables)
        if state.triggers_disabled_tables:
            enable_schema_triggers(connection, state.triggers_disabled_tables)
    except Exception as exc:
        try:
            connection.rollback()
        except Exception:
            pass
        if _is_link_failure(exc):
            logger.warning(
                "Skipping remaining restore steps; SQL Server connection lost: %s",
                exc,
            )
            return
        logger.warning("Could not fully restore SQL Server settings: %s", exc)
