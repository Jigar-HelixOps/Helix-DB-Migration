import logging
import time

from .column_mapping import get_table_column_mapping, resolve_export_plan
from .discover import resolve_table_pairs
from .export_common import default_export_path
from .export_oracle import connect_oracle, export_table_to_csv
from .identity_sync import sync_identities
from .import_mssql import (
    connect_sqlserver,
    get_sqlserver_column_types,
    get_sqlserver_columns_ordered,
    get_table_row_count,
    import_csv_to_table,
)
from .mssql_prepare import (
    MssqlSessionState,
    prepare_sqlserver_for_migration,
    restore_sqlserver_after_migration,
)

logger = logging.getLogger(__name__)


class MigrationResult(object):
    def __init__(
        self,
        table,
        source_schema,
        source_table,
        sqlserver_schema,
        sqlserver_table,
        exported_rows=0,
        imported_rows=0,
        csv_file=None,
        status="SUCCESS",
        error_message=None,
        duration_seconds=0,
    ):
        self.table = table
        self.source_schema = source_schema
        self.source_table = source_table
        self.sqlserver_schema = sqlserver_schema
        self.sqlserver_table = sqlserver_table
        # Aliases used by shared-style reporting
        self.postgres_schema = sqlserver_schema
        self.postgres_table = sqlserver_table
        self.exported_rows = exported_rows
        self.imported_rows = imported_rows
        self.csv_file = csv_file
        self.status = status
        self.error_message = error_message
        self.duration_seconds = duration_seconds


def _log_migration_options(config):
    migrate = config.migrate
    logger.info("Truncate before import: %s", migrate.truncate_before_import)
    logger.info(
        "Truncate all tables upfront: %s", migrate.truncate_all_before_migration
    )
    logger.info("Disable foreign keys: %s", migrate.disable_foreign_keys)
    logger.info("Disable triggers: %s", migrate.disable_triggers)
    logger.info("Restart identity: %s", migrate.restart_identity)
    logger.info(
        "Sync identities after migration: %s",
        migrate.sync_identities_after_migration,
    )


def _migrate_pair(pair, oracle_conn, mssql_conn, config, skip_truncate=False):
    started = time.time()
    csv_file = default_export_path(
        config.export.output_dir, pair.oracle_schema, pair.oracle_table
    )

    logger.info(
        "Starting migration: %s.%s -> %s.%s",
        pair.oracle_schema,
        pair.oracle_table,
        pair.sqlserver_schema,
        pair.sqlserver_table,
    )

    target_columns = [
        name
        for name, _data_type in get_sqlserver_columns_ordered(
            mssql_conn, pair.sqlserver_schema, pair.sqlserver_table
        )
    ]
    table_mapping = get_table_column_mapping(
        config.column_mappings, pair.sqlserver_table
    )
    select_columns, csv_headers, source_column_names = resolve_export_plan(
        config,
        oracle_conn,
        pair.oracle_schema,
        pair.oracle_table,
        target_columns,
        table_mapping=table_mapping,
        global_mapping=config.global_column_mappings,
    )
    column_types = get_sqlserver_column_types(
        mssql_conn, pair.sqlserver_schema, pair.sqlserver_table
    )

    exported = export_table_to_csv(
        connection=oracle_conn,
        schema=pair.oracle_schema,
        table=pair.oracle_table,
        output_file=csv_file,
        settings=config.export,
        lowercase_columns=config.migrate.lowercase_columns,
        column_types=column_types,
        select_columns=select_columns,
        csv_headers=csv_headers,
        source_column_names=source_column_names,
    )

    imported = import_csv_to_table(
        connection=mssql_conn,
        schema=pair.sqlserver_schema,
        table=pair.sqlserver_table,
        input_file=csv_file,
        config=config,
        skip_truncate=skip_truncate,
    )

    actual_rows = get_table_row_count(
        mssql_conn, pair.sqlserver_schema, pair.sqlserver_table
    )
    if actual_rows != imported:
        logger.warning(
            "Row count mismatch for %s.%s: import reported %s but database has %s",
            pair.sqlserver_schema,
            pair.sqlserver_table,
            imported,
            actual_rows,
        )

    duration = time.time() - started
    logger.info(
        "Completed migration: %s exported=%s imported=%s actual_in_db=%s duration=%.1fs csv=%s",
        pair.name,
        exported,
        imported,
        actual_rows,
        duration,
        csv_file,
    )

    return MigrationResult(
        table=pair.name,
        source_schema=pair.oracle_schema,
        source_table=pair.oracle_table,
        sqlserver_schema=pair.sqlserver_schema,
        sqlserver_table=pair.sqlserver_table,
        exported_rows=exported,
        imported_rows=imported,
        csv_file=str(csv_file),
        status="SUCCESS",
        duration_seconds=duration,
    )


def _finalize(
    oracle_conn,
    mssql_conn,
    config,
    pairs,
    mssql_state,
    sync_identities_now=True,
):
    try:
        restore_sqlserver_after_migration(mssql_conn, config, pairs, mssql_state)
    except Exception:
        logger.exception("Failed while restoring SQL Server settings")

    identity_results = []
    if sync_identities_now and config.migrate.sync_identities_after_migration:
        try:
            identity_results = sync_identities(oracle_conn, mssql_conn, config)
        except KeyboardInterrupt:
            logger.warning("Identity sync interrupted")
            raise
        except Exception:
            logger.exception("Identity sync failed during finalize")
    return identity_results


def sync_identities_only(config):
    logger.info("Identity sync started")
    with connect_oracle(config.oracle) as oracle_conn:
        with connect_sqlserver(config.sqlserver) as mssql_conn:
            return sync_identities(oracle_conn, mssql_conn, config)


def migrate_all(config):
    pairs = resolve_table_pairs(config)
    results = []
    identity_results = []
    mssql_state = MssqlSessionState()
    prepare_ok = False
    skip_truncate = (
        config.migrate.truncate_before_import
        and config.migrate.truncate_all_before_migration
    )

    logger.info("Migration started for %s table(s)", len(pairs))
    _log_migration_options(config)

    with connect_oracle(config.oracle) as oracle_conn:
        with connect_sqlserver(config.sqlserver) as mssql_conn:
            try:
                prepare_sqlserver_for_migration(
                    mssql_conn, config, pairs, state=mssql_state
                )
                prepare_ok = True
                for pair in pairs:
                    try:
                        results.append(
                            _migrate_pair(
                                pair,
                                oracle_conn,
                                mssql_conn,
                                config,
                                skip_truncate=skip_truncate,
                            )
                        )
                    except Exception as exc:
                        mssql_conn.rollback()
                        logger.exception("Failed to migrate table %s", pair.name)
                        results.append(
                            MigrationResult(
                                table=pair.name,
                                source_schema=pair.oracle_schema,
                                source_table=pair.oracle_table,
                                sqlserver_schema=pair.sqlserver_schema,
                                sqlserver_table=pair.sqlserver_table,
                                status="FAILED",
                                error_message=str(exc),
                            )
                        )
            finally:
                identity_results = _finalize(
                    oracle_conn,
                    mssql_conn,
                    config,
                    pairs,
                    mssql_state,
                    sync_identities_now=prepare_ok,
                )

    success_count = len([r for r in results if r.status == "SUCCESS"])
    failed_count = len([r for r in results if r.status == "FAILED"])
    logger.info(
        "Migration finished: %s succeeded, %s failed",
        success_count,
        failed_count,
    )
    return results, identity_results


def migrate_one(config, table_name):
    pairs = resolve_table_pairs(config)
    match = None
    for pair in pairs:
        if pair.name.lower() == table_name.lower():
            match = pair
            break
    if not match:
        raise ValueError(
            "Table '{}' not found in matching Oracle/SQL Server tables.".format(
                table_name
            )
        )

    logger.info("Single-table migration started for %s", match.name)
    _log_migration_options(config)
    mssql_state = MssqlSessionState()
    identity_results = []
    prepare_ok = False
    result = None

    with connect_oracle(config.oracle) as oracle_conn:
        with connect_sqlserver(config.sqlserver) as mssql_conn:
            try:
                prepare_sqlserver_for_migration(
                    mssql_conn, config, [match], state=mssql_state
                )
                prepare_ok = True
                skip_truncate = (
                    config.migrate.truncate_before_import
                    and config.migrate.truncate_all_before_migration
                )
                result = _migrate_pair(
                    match,
                    oracle_conn,
                    mssql_conn,
                    config,
                    skip_truncate=skip_truncate,
                )
            except Exception as exc:
                mssql_conn.rollback()
                logger.exception("Failed to migrate table %s", match.name)
                result = MigrationResult(
                    table=match.name,
                    source_schema=match.oracle_schema,
                    source_table=match.oracle_table,
                    sqlserver_schema=match.sqlserver_schema,
                    sqlserver_table=match.sqlserver_table,
                    status="FAILED",
                    error_message=str(exc),
                )
            finally:
                identity_results = _finalize(
                    oracle_conn,
                    mssql_conn,
                    config,
                    [match],
                    mssql_state,
                    sync_identities_now=prepare_ok,
                )

    return result, identity_results
