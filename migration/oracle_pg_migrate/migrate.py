import logging
import time

from .discover import resolve_table_pairs
from .export_common import default_export_path
from .column_mapping import get_table_column_mapping, resolve_export_plan
from .import_pg import (
    connect_postgres,
    get_postgres_column_types,
    get_postgres_columns_ordered,
    get_table_row_count,
    import_table_from_csv,
)
from .pg_prepare import prepare_postgres_for_migration, restore_postgres_after_migration
from .pg_validate import validate_constraints
from .post_migrate import run_post_migration_sql
from .sequence_sync import sync_sequences
from .source_db import (
    connect_source,
    get_source_schema,
    get_source_tables,
    reset_source_connection,
    source_display_name,
)

logger = logging.getLogger(__name__)


class MigrationResult(object):
    def __init__(
        self,
        table,
        source_schema,
        source_table,
        postgres_schema,
        postgres_table,
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
        self.postgres_schema = postgres_schema
        self.postgres_table = postgres_table
        self.exported_rows = exported_rows
        self.imported_rows = imported_rows
        self.csv_file = csv_file
        self.status = status
        self.error_message = error_message
        self.duration_seconds = duration_seconds

    @property
    def oracle_schema(self):
        return self.source_schema

    @property
    def oracle_table(self):
        return self.source_table

    @property
    def rows(self):
        return self.imported_rows


def _effective_truncate_cascade(config, table_count):
    if table_count == 1 and not config.migrate.truncate_cascade_single_table:
        return False
    return config.migrate.truncate_cascade


def _migrate_pair(
    pair,
    source_conn,
    pg_conn,
    config,
    csv_file=None,
    skip_truncate=False,
    table_count=1,
):
    from .source_db import export_table_to_csv

    started = time.time()
    if csv_file is None:
        csv_file = default_export_path(
            config.export.output_dir, pair.source_schema, pair.source_table
        )

    logger.info(
        "Starting migration: %s.%s -> %s.%s",
        pair.source_schema,
        pair.source_table,
        pair.postgres_schema,
        pair.postgres_table,
    )

    pg_columns_ordered = [
        name for name, _data_type in get_postgres_columns_ordered(
            pg_conn, pair.postgres_schema, pair.postgres_table
        )
    ]
    table_mapping = get_table_column_mapping(
        config.column_mappings, pair.postgres_table
    )
    select_columns, csv_headers, source_column_names = resolve_export_plan(
        config,
        source_conn,
        pair.source_schema,
        pair.source_table,
        pg_columns_ordered,
        table_mapping=table_mapping,
        global_mapping=config.global_column_mappings,
    )
    column_types = get_postgres_column_types(
        pg_conn, pair.postgres_schema, pair.postgres_table
    )

    exported = export_table_to_csv(
        connection=source_conn,
        config=config,
        schema=pair.source_schema,
        table=pair.source_table,
        output_file=csv_file,
        lowercase_columns=config.migrate.lowercase_columns,
        column_types=column_types,
        select_columns=select_columns,
        csv_headers=csv_headers,
        source_column_names=source_column_names,
    )

    imported = import_table_from_csv(
        connection=pg_conn,
        schema=pair.postgres_schema,
        table=pair.postgres_table,
        input_file=csv_file,
        config=config,
        skip_truncate=skip_truncate,
        truncate_cascade=_effective_truncate_cascade(config, table_count),
    )

    actual_rows = get_table_row_count(
        pg_conn, pair.postgres_schema, pair.postgres_table
    )
    if actual_rows != imported:
        logger.warning(
            "Row count mismatch for %s.%s: csv/import reported %s but database has %s",
            pair.postgres_schema,
            pair.postgres_table,
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
        source_schema=pair.source_schema,
        source_table=pair.source_table,
        postgres_schema=pair.postgres_schema,
        postgres_table=pair.postgres_table,
        exported_rows=exported,
        imported_rows=imported,
        csv_file=csv_file,
        status="SUCCESS",
        duration_seconds=duration,
    )


def _log_migration_options(config):
    migrate = config.migrate
    logger.info("Source database type: %s", config.source.type)
    logger.info("Truncate before import: %s", migrate.truncate_before_import)
    logger.info("Truncate all tables upfront: %s", migrate.truncate_all_before_migration)
    logger.info("Truncate cascade: %s", migrate.truncate_cascade)
    logger.info("Restart identity: %s", migrate.restart_identity)
    logger.info("Disable triggers: %s", migrate.disable_triggers)
    logger.info("Disable foreign keys: %s", migrate.disable_foreign_keys)
    logger.info("Disable row security: %s", migrate.disable_row_security)
    logger.info("Analyze after migration: %s", migrate.analyze_after_migration)
    logger.info(
        "Validate constraints after migration: %s",
        migrate.validate_constraints_after_migration,
    )
    logger.info(
        "Sync sequences after migration: %s",
        migrate.sync_sequences_after_migration,
    )
    logger.info(
        "Partition column handling: %s",
        migrate.partition_column_handling,
    )
    post = config.post_migration
    logger.info(
        "Post-migration SQL: enabled=%s file=%s",
        bool(post and post.enabled),
        post.sql_file if post else "",
    )


def _finalize_postgres(source_conn, pg_conn, config, pairs, pg_state):
    restore_postgres_after_migration(pg_conn, config, pairs, pg_state)

    reset_source_connection(source_conn, config)

    sequence_results = []
    if config.migrate.sync_sequences_after_migration:
        sequence_results = sync_sequences(source_conn, pg_conn, config)

    validation_issues = []
    if config.migrate.validate_constraints_after_migration:
        validation_issues = validate_constraints(pg_conn, config, pairs)

    try:
        run_post_migration_sql(pg_conn, config)
    except Exception:
        logger.exception("Post-migration SQL failed")
        raise

    return validation_issues, sequence_results


def sync_sequences_only(config):
    logger.info("Sequence sync started")
    with connect_source(config) as source_conn:
        with connect_postgres(config.postgres) as pg_conn:
            return sync_sequences(source_conn, pg_conn, config)


def migrate_all(config):
    pairs = resolve_table_pairs(config)
    results = []
    validation_issues = []
    sequence_results = []
    pg_state = None
    skip_truncate = (
        config.migrate.truncate_before_import
        and config.migrate.truncate_all_before_migration
    )

    logger.info("Migration started for %s table(s)", len(pairs))
    _log_migration_options(config)

    with connect_source(config) as source_conn:
        with connect_postgres(config.postgres) as pg_conn:
            try:
                pg_state = prepare_postgres_for_migration(
                    pg_conn, config, pairs, table_count=len(pairs)
                )

                for pair in pairs:
                    try:
                        results.append(
                            _migrate_pair(
                                pair,
                                source_conn,
                                pg_conn,
                                config,
                                skip_truncate=skip_truncate,
                                table_count=len(pairs),
                            )
                        )
                    except Exception as exc:
                        pg_conn.rollback()
                        reset_source_connection(source_conn, config)
                        logger.exception("Failed to migrate table %s", pair.name)
                        results.append(
                            MigrationResult(
                                table=pair.name,
                                source_schema=pair.source_schema,
                                source_table=pair.source_table,
                                postgres_schema=pair.postgres_schema,
                                postgres_table=pair.postgres_table,
                                status="FAILED",
                                error_message=str(exc),
                            )
                        )
            finally:
                validation_issues, sequence_results = _finalize_postgres(
                    source_conn, pg_conn, config, pairs, pg_state
                )

    success_count = len([r for r in results if r.status == "SUCCESS"])
    failed_count = len([r for r in results if r.status == "FAILED"])
    logger.info(
        "Migration finished: %s succeeded, %s failed",
        success_count,
        failed_count,
    )

    return results, validation_issues, sequence_results


def _resolve_source_table(config, table_name):
    source_schema = get_source_schema(config)
    if not source_schema:
        raise ValueError(
            "{}.schema is required.".format(source_display_name(config))
        )

    source_table = table_name
    with connect_source(config) as source_conn:
        source_tables = get_source_tables(source_conn, config)
        for actual in source_tables.values():
            if actual.lower() == table_name.lower():
                source_table = actual
                break

    return source_schema, source_table


def migrate_one(config, table_name, csv_file=None):
    if not get_source_schema(config) or not config.postgres.schema:
        raise ValueError(
            "source schema and postgres.schema are required."
        )

    source_schema, source_table = _resolve_source_table(config, table_name)
    postgres_schema = config.postgres.schema
    postgres_table = table_name
    for actual in _postgres_table_names(config, postgres_schema):
        if actual.lower() == table_name.lower():
            postgres_table = actual
            break

    from .discover import TablePair

    pair = TablePair(
        source_schema=source_schema,
        source_table=source_table,
        postgres_schema=postgres_schema,
        postgres_table=postgres_table,
    )

    target_csv = csv_file or default_export_path(
        config.export.output_dir, source_schema, source_table
    )

    pg_state = None
    validation_issues = []
    sequence_results = []
    result = None
    with connect_source(config) as source_conn:
        with connect_postgres(config.postgres) as pg_conn:
            try:
                pg_state = prepare_postgres_for_migration(
                    pg_conn, config, [pair], table_count=1
                )
                result = _migrate_pair(
                    pair,
                    source_conn,
                    pg_conn,
                    config,
                    csv_file=target_csv,
                    skip_truncate=config.migrate.truncate_all_before_migration,
                    table_count=1,
                )
            except Exception:
                pg_conn.rollback()
                reset_source_connection(source_conn, config)
                raise
            finally:
                if pg_state is not None:
                    try:
                        validation_issues, sequence_results = _finalize_postgres(
                            source_conn, pg_conn, config, [pair], pg_state
                        )
                    except Exception:
                        pg_conn.rollback()
                        raise

    return result, validation_issues, sequence_results


def _postgres_table_names(config, schema):
    from .discover import get_postgres_tables

    with connect_postgres(config.postgres) as pg_conn:
        return list(get_postgres_tables(pg_conn, schema).values())


def _resolve_pairs_for_validation(config, table_name=None):
    from .discover import TablePair, resolve_table_pairs

    if not table_name:
        return resolve_table_pairs(config)

    if not get_source_schema(config) or not config.postgres.schema:
        raise ValueError("source schema and postgres.schema are required.")

    source_schema, source_table = _resolve_source_table(config, table_name)
    postgres_schema = config.postgres.schema
    postgres_table = table_name
    for actual in _postgres_table_names(config, postgres_schema):
        if actual.lower() == table_name.lower():
            postgres_table = actual
            break

    return [
        TablePair(
            source_schema=source_schema,
            source_table=source_table,
            postgres_schema=postgres_schema,
            postgres_table=postgres_table,
        )
    ]


def validate_only(config, table_name=None):
    pairs = _resolve_pairs_for_validation(config, table_name)
    logger.info("Constraint validation started for %s table(s)", len(pairs))

    with connect_postgres(config.postgres) as pg_conn:
        return validate_constraints(pg_conn, config, pairs)
