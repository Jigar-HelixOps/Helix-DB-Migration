import argparse
import logging
import sys
from datetime import datetime

from .config import load_config, merge_env_overrides
from .logging_setup import setup_logging
from .migrate import migrate_all, migrate_one, sync_identities_only
from .report import write_identity_sync_report, write_summary_report

logger = logging.getLogger(__name__)


def build_parser():
    parser = argparse.ArgumentParser(
        description="Migrate data from Oracle to SQL Server using matching table names."
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
        help="Truncate SQL Server tables before import",
    )
    parser.add_argument(
        "--list-tables",
        action="store_true",
        help="List matching tables and exit without migrating",
    )
    parser.add_argument(
        "--sync-identities-only",
        action="store_true",
        help="Sync SQL Server IDENTITY from Oracle sequences without migrating data",
    )
    return parser


def _print_results(results):
    success = [r for r in results if r.status == "SUCCESS"]
    failed = [r for r in results if r.status == "FAILED"]
    total_rows = sum(r.imported_rows for r in success)

    if failed:
        print("Migration completed with errors.")
    else:
        print("Migration completed successfully.")

    print(
        "Migrated {} table(s), {} total rows imported".format(
            len(success), total_rows
        )
    )
    for result in success:
        print(
            "  {}: exported {} rows, imported {} rows".format(
                result.table, result.exported_rows, result.imported_rows
            )
        )
    for result in failed:
        print("  FAILED {}: {}".format(result.table, result.error_message))


def _print_identity_results(sequence_results):
    if not sequence_results:
        print("No identities synced.")
        return

    success = [r for r in sequence_results if r.status == "SUCCESS"]
    failed = [r for r in sequence_results if r.status == "FAILED"]
    skipped = [r for r in sequence_results if r.status == "SKIPPED"]
    print(
        "Identities synced: {} updated, {} skipped, {} failed".format(
            len(success), len(skipped), len(failed)
        )
    )
    for result in success[:10]:
        print(
            "  {} -> {}.{}: reseed={} (source next {})".format(
                result.source_sequence,
                result.sqlserver_table,
                result.identity_column,
                result.reseed_value,
                result.source_next_value,
            )
        )
    if len(success) > 10:
        print("  ... and {} more".format(len(success) - 10))
    for result in failed + skipped[:20]:
        print(
            "  {} - {}: {}".format(
                result.source_sequence,
                result.status,
                result.error_message or "",
            )
        )


def run(args, log_file, summary_file):
    started_at = datetime.now()
    config = merge_env_overrides(load_config(args.config))

    logger.info("Config file: %s", args.config)
    logger.info(
        "Oracle connection: user=%s dsn=%s schema=%s",
        config.oracle.user,
        config.oracle.dsn,
        config.oracle.schema,
    )
    logger.info(
        "SQL Server connection: host=%s port=%s database=%s user=%s schema=%s",
        config.sqlserver.host,
        config.sqlserver.port,
        config.sqlserver.database,
        config.sqlserver.user,
        config.sqlserver.schema,
    )

    if args.truncate:
        config.migrate.truncate_before_import = True
        logger.info("Truncate before import enabled")

    if args.list_tables:
        from .discover import resolve_table_pairs

        pairs = resolve_table_pairs(config)
        print("Found {} matching table(s):".format(len(pairs)))
        for pair in pairs:
            line = "  {}.{} -> {}.{}".format(
                pair.oracle_schema,
                pair.oracle_table,
                pair.sqlserver_schema,
                pair.sqlserver_table,
            )
            print(line)
            logger.info("Matching table: %s", line.strip())
        return 0

    if args.sync_identities_only:
        command = "sync-identities-only"
        sequence_results = sync_identities_only(config)
        finished_at = datetime.now()
        report_path, sequences_csv = write_identity_sync_report(
            summary_file,
            sequence_results,
            started_at,
            finished_at,
            config,
            command,
        )
        _print_identity_results(sequence_results)
        print("")
        print("Log file         : {}".format(log_file))
        print("Summary file     : {}".format(report_path))
        print("Sequences CSV    : {}".format(sequences_csv))
        failed = [r for r in sequence_results if r.status == "FAILED"]
        return 1 if failed else 0

    command = "migrate"
    sequence_results = []
    if args.table:
        command = "migrate --table {}".format(args.table)
        result, sequence_results = migrate_one(config, args.table)
        results = [result]
    else:
        results, sequence_results = migrate_all(config)

    finished_at = datetime.now()
    report_path, tables_csv, sequences_csv = write_summary_report(
        summary_file,
        results,
        started_at,
        finished_at,
        config,
        command,
        sequence_results=sequence_results,
    )

    _print_results(results)
    print("")
    _print_identity_results(sequence_results)
    print("")
    print("Log file         : {}".format(log_file))
    print("Summary file     : {}".format(report_path))
    print("Tables CSV       : {}".format(tables_csv))
    print("Sequences CSV    : {}".format(sequences_csv))

    failed = [r for r in results if r.status == "FAILED"]
    identity_failed = [r for r in sequence_results if r.status == "FAILED"]
    if failed or identity_failed:
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
