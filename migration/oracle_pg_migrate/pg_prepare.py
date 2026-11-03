import logging

import psycopg2
from psycopg2 import sql

logger = logging.getLogger(__name__)


class PostgresSessionState(object):
    def __init__(self):
        self.replica_role_enabled = False
        self.row_security_disabled = False
        self.triggers_disabled_tables = []


def _execute(connection, statement, params=None):
    with connection.cursor() as cursor:
        cursor.execute(statement, params)
    connection.commit()


def _set_session_replication_role(connection, role):
    _execute(connection, "SET session_replication_role = %s", (role,))


def _set_row_security(connection, enabled):
    value = "on" if enabled else "off"
    _execute(connection, "SET row_security = %s", (value,))


def disable_table_triggers(connection, schema, table):
    query = sql.SQL("ALTER TABLE {}.{} DISABLE TRIGGER ALL").format(
        sql.Identifier(schema),
        sql.Identifier(table),
    )
    with connection.cursor() as cursor:
        cursor.execute(query)
    connection.commit()
    logger.info("Disabled triggers on %s.%s", schema, table)


def enable_table_triggers(connection, schema, table):
    query = sql.SQL("ALTER TABLE {}.{} ENABLE TRIGGER ALL").format(
        sql.Identifier(schema),
        sql.Identifier(table),
    )
    with connection.cursor() as cursor:
        cursor.execute(query)
    connection.commit()
    logger.info("Enabled triggers on %s.%s", schema, table)


def _build_truncate_query(pairs, cascade, restart_identity):
    schema = pairs[0].postgres_schema
    table_list = sql.SQL(", ").join(
        sql.SQL("{}.{}").format(
            sql.Identifier(schema),
            sql.Identifier(pair.postgres_table),
        )
        for pair in pairs
    )

    options = []
    if restart_identity:
        options.append(sql.SQL("RESTART IDENTITY"))
    if cascade:
        options.append(sql.SQL("CASCADE"))

    if options:
        return sql.SQL("TRUNCATE TABLE {} {}").format(
            table_list,
            sql.SQL(" ").join(options),
        )
    return sql.SQL("TRUNCATE TABLE {}").format(table_list)


def truncate_tables(connection, pairs, cascade, restart_identity):
    if not pairs:
        return

    schema = pairs[0].postgres_schema
    query = _build_truncate_query(pairs, cascade, restart_identity)

    try:
        with connection.cursor() as cursor:
            cursor.execute(query)
        connection.commit()
    except Exception:
        connection.rollback()
        raise

    logger.info(
        "Truncated %s table(s) in schema %s (cascade=%s, restart_identity=%s)",
        len(pairs),
        schema,
        cascade,
        restart_identity,
    )


def delete_table_contents(connection, schema, table):
    query = sql.SQL("DELETE FROM {}.{}").format(
        sql.Identifier(schema),
        sql.Identifier(table),
    )
    try:
        with connection.cursor() as cursor:
            cursor.execute(query)
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    logger.info("Deleted all rows from %s.%s", schema, table)


def restart_table_identity(connection, schema, table):
    query = """
        SELECT pg_get_serial_sequence(format('%I.%I', %s::text, %s::text), a.attname)
        FROM pg_attribute a
        JOIN pg_class c ON a.attrelid = c.oid
        JOIN pg_namespace n ON c.relnamespace = n.oid
        WHERE n.nspname = %s
          AND c.relname = %s
          AND a.attnum > 0
          AND NOT a.attisdropped
    """
    with connection.cursor() as cursor:
        cursor.execute(query, (schema, table, schema, table))
        sequences = [row[0] for row in cursor.fetchall() if row[0]]

    for sequence_name in sequences:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT setval(%s::regclass, 1, false)",
                (sequence_name,),
            )
        connection.commit()
        logger.info(
            "Restarted identity sequence %s for %s.%s",
            sequence_name,
            schema,
            table,
        )


def clear_tables_for_migration(connection, pairs, migrate, table_count=None):
    if not pairs:
        return

    table_count = table_count or len(pairs)
    cascade = migrate.truncate_cascade
    use_delete_instead = (
        table_count == 1 and not migrate.truncate_cascade_single_table
    )

    if use_delete_instead:
        pair = pairs[0]
        try:
            truncate_tables(
                connection,
                pairs,
                cascade=False,
                restart_identity=migrate.restart_identity,
            )
            return
        except psycopg2.Error as exc:
            connection.rollback()
            logger.warning(
                "TRUNCATE without CASCADE failed for %s.%s (%s); using DELETE instead",
                pair.postgres_schema,
                pair.postgres_table,
                exc.pgerror.strip() if exc.pgerror else exc,
            )
        delete_table_contents(
            connection, pair.postgres_schema, pair.postgres_table
        )
        if migrate.restart_identity:
            restart_table_identity(
                connection, pair.postgres_schema, pair.postgres_table
            )
        return

    truncate_tables(
        connection,
        pairs,
        cascade=cascade,
        restart_identity=migrate.restart_identity,
    )


def analyze_tables(connection, pairs):
    for pair in pairs:
        query = sql.SQL("ANALYZE {}.{}").format(
            sql.Identifier(pair.postgres_schema),
            sql.Identifier(pair.postgres_table),
        )
        with connection.cursor() as cursor:
            cursor.execute(query)
    connection.commit()
    logger.info("Analyzed %s table(s)", len(pairs))


def prepare_postgres_for_migration(connection, config, pairs, table_count=None):
    state = PostgresSessionState()
    migrate = config.migrate

    logger.info("Preparing PostgreSQL for data migration")

    if migrate.disable_triggers or migrate.disable_foreign_keys:
        try:
            _set_session_replication_role(connection, "replica")
            state.replica_role_enabled = True
            logger.info(
                "Set session_replication_role=replica (triggers and FK checks disabled)"
            )
        except Exception as exc:
            logger.warning(
                "Could not set session_replication_role=replica: %s", exc
            )
            if migrate.disable_triggers:
                for pair in pairs:
                    try:
                        disable_table_triggers(
                            connection, pair.postgres_schema, pair.postgres_table
                        )
                        state.triggers_disabled_tables.append(pair)
                    except Exception as trigger_exc:
                        logger.warning(
                            "Could not disable triggers on %s.%s: %s",
                            pair.postgres_schema,
                            pair.postgres_table,
                            trigger_exc,
                        )

    if migrate.disable_row_security:
        try:
            _set_row_security(connection, False)
            state.row_security_disabled = True
            logger.info("Set row_security=off")
        except Exception as exc:
            logger.warning("Could not disable row security: %s", exc)

    if migrate.truncate_before_import and migrate.truncate_all_before_migration:
        clear_tables_for_migration(
            connection,
            pairs,
            migrate,
            table_count=table_count or len(pairs),
        )

    return state


def restore_postgres_after_migration(connection, config, pairs, state):
    migrate = config.migrate

    logger.info("Restoring PostgreSQL session settings after migration")

    if state and state.triggers_disabled_tables:
        for pair in state.triggers_disabled_tables:
            try:
                enable_table_triggers(
                    connection, pair.postgres_schema, pair.postgres_table
                )
            except Exception as exc:
                logger.warning(
                    "Could not enable triggers on %s.%s: %s",
                    pair.postgres_schema,
                    pair.postgres_table,
                    exc,
                )

    if state and state.replica_role_enabled:
        try:
            _set_session_replication_role(connection, "origin")
            logger.info("Set session_replication_role=origin")
        except Exception as exc:
            logger.warning("Could not reset session_replication_role: %s", exc)

    if state and state.row_security_disabled:
        try:
            _set_row_security(connection, True)
            logger.info("Set row_security=on")
        except Exception as exc:
            logger.warning("Could not re-enable row security: %s", exc)

    if migrate.analyze_after_migration:
        try:
            analyze_tables(connection, pairs)
        except Exception as exc:
            connection.rollback()
            logger.warning("Could not analyze tables: %s", exc)
