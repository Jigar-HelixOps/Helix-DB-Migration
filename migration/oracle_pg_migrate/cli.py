import argparse
import logging
import sys
from datetime import datetime

from .config import load_config, merge_env_overrides
from .source_db import SOURCE_SQLSERVER, get_source_schema
from .logging_setup import setup_logging
from .migrate import migrate_all, migrate_one, sync_sequences_only, validate_only
from .report import (
    write_sequence_sync_report,
    write_summary_report,
    write_validation_report,
)

logger = logging.getLogger(__name__)


def build_parser():
    parser = argparse.ArgumentParser(
        description="Migrate data from Oracle or SQL Server to PostgreSQL using matching table names."
    )
    parser.add_argument(
        "-c",
        "--config",
        default="config.yaml",
        help="Path to YAML config file (default: config.yaml)",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="count",
        default=0,
        help="Increase log verbosity (-v info, -vv debug)",
    )
    parser.add_argument(
        "--table",
        help="Migrate only this table name (must exist in both schemas)",
    )
    parser.add_argument(
        "--truncate",
        action="store_true",
        help="Truncate PostgreSQL tables before import",
    )
    parser.add_argument(
        "--list-tables",
        action="store_true",
        help="List matching tables and exit without migrating",
    )
    parser.add_argument(
        "--validate-only",
        action="store_true",
        help="Validate PostgreSQL constraints without running migration",
    )
    parser.add_argument(
        "--sync-sequences-only",
        action="store_true",
        help="Sync PostgreSQL sequences from source database without running migration",
    )
    return parser


def _print_report_paths(report_path, tables_csv, validation_csv, sequences_csv=None):
    print("Summary file     : {}".format(report_path))
    if tables_csv:
        print("Tables CSV       : {}".format(tables_csv))
    if validation_csv:
        print("Validation CSV   : {}".format(validation_csv))
    if sequences_csv:
        print("Sequences CSV    : {}".format(sequences_csv))


def _print_sequence_results(sequence_results):
    if not sequence_results:
        print("No sequences synced.")
        return

    success = [r for r in sequence_results if r.status == "SUCCESS"]
    failed = [r for r in sequence_results if r.status == "FAILED"]
    skipped = [r for r in sequence_results if r.status == "SKIPPED"]
    print(
        "Sequences synced: {} updated, {} skipped, {} failed".format(
            len(success), len(skipped), len(failed)
        )
    )
    for result in success[:10]:
        print(
            "  {} -> {}: next value set to {} (source next {})".format(
                result.source_sequence,
                result.postgres_sequence,
                result.postgres_new_value,
                result.source_next_value,
            )
        )
    if len(success) > 10:
        print("  ... and {} more".format(len(success) - 10))
    for result in failed + skipped:
        print("  {} - {}: {}".format(
            result.source_sequence, result.status, result.error_message or ""
        ))


def _print_validation_issues(validation_issues):
    if not validation_issues:
        print("Constraint validation passed.")
        return

    print("Constraint validation found {} issue(s):".format(len(validation_issues)))
    for issue in validation_issues:
        print(
            "  {} {} on {}: {} violation(s) - {}".format(
                issue.constraint_type,
                issue.constraint_name,
                issue.table_name,
                issue.violation_count,
                issue.details,
            )
        )


def _print_results(results):
    success = [r for r in results if r.status == "SUCCESS"]
    failed = [r for r in results if r.status == "FAILED"]
    total_rows = sum(r.imported_rows for r in success)

    if failed:
        print("Migration completed with errors.")
    else:
        print("Migration completed successfully.")

    print("Migrated {} table(s), {} total rows imported".format(len(success), total_rows))
    for result in success:
        print(
            "  {}: exported {} rows, imported {} rows".format(
                result.table, result.exported_rows, result.imported_rows
            )
        )

    for result in failed:
        print("  FAILED {}: {}".format(result.table, result.error_message))


def run(args, log_file, summary_file):
    started_at = datetime.now()
    config = merge_env_overrides(load_config(args.config))

    logger.info("Config file: %s", args.config)
    logger.info("Source database type: %s", config.source.type)
    if config.source.type == SOURCE_SQLSERVER:
        logger.info(
            "SQL Server connection: host=%s port=%s database=%s user=%s schema=%s",
            config.sqlserver.host,
            config.sqlserver.port,
            config.sqlserver.database,
            config.sqlserver.user,
            get_source_schema(config),
        )
    else:
        logger.info(
            "Oracle connection: user=%s dsn=%s schema=%s",
            config.oracle.user,
            config.oracle.dsn,
            get_source_schema(config),
        )
    logger.info(
        "PostgreSQL connection: host=%s port=%s database=%s user=%s schema=%s",
        config.postgres.host,
        config.postgres.port,
        config.postgres.database,
        config.postgres.user,
        config.postgres.schema,
    )

    if args.truncate:
        config.migrate.truncate_before_import = True
        config.import_.truncate_before_import = True
        logger.info("Truncate before import enabled")

    if args.list_tables:
        from .discover import resolve_table_pairs

        pairs = resolve_table_pairs(config)
        print("Found {} matching table(s):".format(len(pairs)))
        for pair in pairs:
            line = "  {}.{} -> {}.{}".format(
                pair.source_schema,
                pair.source_table,
                pair.postgres_schema,
                pair.postgres_table,
            )
            print(line)
            logger.info("Matching table: %s", line.strip())
        logger.info("List tables completed: %s table(s) found", len(pairs))
        return 0

    if args.validate_only:
        from .migrate import _resolve_pairs_for_validation

        command = "validate-only"
        if args.table:
            command = "validate-only --table {}".format(args.table)

        pairs = _resolve_pairs_for_validation(config, args.table)
        validation_issues = validate_only(config, args.table)
        finished_at = datetime.now()
        report_path, tables_csv, validation_csv, sequences_csv = write_validation_report(
            summary_file,
            validation_issues,
            started_at,
            finished_at,
            config,
            command,
            pairs,
        )
        print("Validated {} table(s)".format(len(pairs)))
        _print_validation_issues(validation_issues)
        print("")
        print("Log file         : {}".format(log_file))
        _print_report_paths(report_path, tables_csv, validation_csv, sequences_csv)
        logger.info("Validation report written to %s", report_path)
        logger.info("Tables CSV written to %s", tables_csv)
        logger.info("Validation CSV written to %s", validation_csv)
        return 1 if validation_issues else 0

    if args.sync_sequences_only:
        command = "sync-sequences-only"
        sequence_results = sync_sequences_only(config)
        finished_at = datetime.now()
        report_path, sequences_csv = write_sequence_sync_report(
            summary_file,
            sequence_results,
            started_at,
            finished_at,
            config,
            command,
        )
        _print_sequence_results(sequence_results)
        print("")
        print("Log file         : {}".format(log_file))
        print("Summary file     : {}".format(report_path))
        print("Sequences CSV    : {}".format(sequences_csv))
        logger.info("Sequence sync report written to %s", report_path)
        logger.info("Sequences CSV written to %s", sequences_csv)
        failed = [r for r in sequence_results if r.status == "FAILED"]
        return 1 if failed else 0

    command = "migrate"
    validation_issues = []
    sequence_results = []
    if args.table:
        command = "migrate --table {}".format(args.table)
        result, validation_issues, sequence_results = migrate_one(config, args.table)
        results = [result]
    else:
        results, validation_issues, sequence_results = migrate_all(config)

    finished_at = datetime.now()
    report_path, tables_csv, validation_csv, sequences_csv = write_summary_report(
        summary_file,
        results,
        started_at,
        finished_at,
        config,
        command,
        validation_issues=validation_issues,
        sequence_results=sequence_results,
    )

    _print_results(results)
    print("")
    _print_sequence_results(sequence_results)
    print("")
    _print_validation_issues(validation_issues)
    print("")
    print("Log file         : {}".format(log_file))
    _print_report_paths(report_path, tables_csv, validation_csv, sequences_csv)
    logger.info("Summary written to %s", report_path)
    logger.info("Tables CSV written to %s", tables_csv)
    logger.info("Validation CSV written to %s", validation_csv)
    logger.info("Sequences CSV written to %s", sequences_csv)

    failed = [r for r in results if r.status == "FAILED"]
    sequence_failed = [r for r in sequence_results if r.status == "FAILED"]
    if failed or validation_issues or sequence_failed:
        return 1
    return 0


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)

    config = merge_env_overrides(load_config(args.config))
    log_file, summary_file = setup_logging(config.logging.log_dir, args.verbose)

    try:
        return run(args, log_file, summary_file)
    except Exception as exc:
        logger.exception("Migration failed")
        print("Error: {}".format(exc), file=sys.stderr)
        print("Log file     : {}".format(log_file), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
