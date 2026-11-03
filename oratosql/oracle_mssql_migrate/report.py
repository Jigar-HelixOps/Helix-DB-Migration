import csv
from datetime import datetime


def _report_base_path(summary_path):
    return str(summary_path).replace(".summary.txt", "")


def _tables_csv_path(summary_path):
    return _report_base_path(summary_path) + "_tables.csv"


def _sequences_csv_path(summary_path):
    return _report_base_path(summary_path) + "_sequences.csv"


def _write_csv(path, headers, rows):
    with open(str(path), "w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(headers)
        for row in rows:
            writer.writerow(row)
    return path


def write_migration_tables_csv(summary_path, results):
    headers = [
        "table_name",
        "oracle_schema",
        "oracle_table",
        "sqlserver_schema",
        "sqlserver_table",
        "status",
        "rows_exported",
        "rows_imported",
        "rows_match",
        "duration_seconds",
        "csv_file",
        "error_message",
    ]
    rows = []
    for result in results:
        rows_match = ""
        if result.status == "SUCCESS":
            rows_match = (
                "YES" if result.exported_rows == result.imported_rows else "NO"
            )
        rows.append([
            result.table,
            result.source_schema,
            result.source_table,
            result.sqlserver_schema,
            result.sqlserver_table,
            result.status,
            result.exported_rows,
            result.imported_rows,
            rows_match,
            "{:.1f}".format(result.duration_seconds),
            result.csv_file or "",
            result.error_message or "",
        ])
    return _write_csv(_tables_csv_path(summary_path), headers, rows)


def write_sequences_csv(summary_path, sequence_results):
    headers = [
        "source_sequence",
        "sqlserver_table",
        "identity_column",
        "source_next_value",
        "max_column_value",
        "reseed_value",
        "status",
        "error_message",
    ]
    rows = [
        [
            result.source_sequence,
            result.sqlserver_table,
            result.identity_column,
            result.source_next_value,
            result.max_column_value,
            result.reseed_value,
            result.status,
            result.error_message or "",
        ]
        for result in sequence_results
    ]
    return _write_csv(_sequences_csv_path(summary_path), headers, rows)


def _append_identity_section(lines, sequence_results):
    lines.append("IDENTITY / SEQUENCE SYNC")
    lines.append("-" * 72)
    if not sequence_results:
        lines.append("  (none)")
        lines.append("")
        return

    success = [r for r in sequence_results if r.status == "SUCCESS"]
    failed = [r for r in sequence_results if r.status == "FAILED"]
    skipped = [r for r in sequence_results if r.status == "SKIPPED"]
    lines.append(
        "  Updated={}, skipped={}, failed={}".format(
            len(success), len(skipped), len(failed)
        )
    )
    lines.append("")
    for result in sequence_results:
        lines.append(
            "  {} -> {}.{} [{}] next={} max_col={} reseed={} {}".format(
                result.source_sequence,
                result.sqlserver_table or "-",
                result.identity_column or "-",
                result.status,
                result.source_next_value,
                result.max_column_value,
                result.reseed_value,
                result.error_message or "",
            )
        )
    lines.append("")


def write_summary_report(
    path,
    results,
    started_at,
    finished_at,
    config,
    command,
    sequence_results=None,
):
    sequence_results = sequence_results or []
    duration = finished_at - started_at
    success = [r for r in results if r.status == "SUCCESS"]
    failed = [r for r in results if r.status == "FAILED"]

    lines = []
    lines.append("=" * 72)
    lines.append("ORACLE TO SQL SERVER MIGRATION SUMMARY")
    lines.append("=" * 72)
    lines.append("Command        : {}".format(command))
    lines.append("Started        : {}".format(started_at.strftime("%Y-%m-%d %H:%M:%S")))
    lines.append("Finished       : {}".format(finished_at.strftime("%Y-%m-%d %H:%M:%S")))
    lines.append("Duration       : {}".format(duration))
    lines.append("Status         : {}".format("FAILED" if failed else "SUCCESS"))
    lines.append("")
    lines.append("Oracle")
    lines.append("  User           : {}".format(config.oracle.user))
    lines.append("  DSN            : {}".format(config.oracle.dsn))
    lines.append("  Schema         : {}".format(config.oracle.schema))
    lines.append("")
    lines.append("SQL Server")
    lines.append(
        "  Host           : {}:{}".format(
            config.sqlserver.host, config.sqlserver.port
        )
    )
    lines.append("  Database       : {}".format(config.sqlserver.database))
    lines.append("  User           : {}".format(config.sqlserver.user))
    lines.append("  Schema         : {}".format(config.sqlserver.schema))
    lines.append("")
    lines.append("RESULTS")
    lines.append("-" * 72)
    lines.append(
        "  Succeeded={}, failed={}, rows_imported={}".format(
            len(success),
            len(failed),
            sum(r.imported_rows for r in success),
        )
    )
    lines.append("")

    for result in results:
        lines.append("  Table          : {}".format(result.table))
        lines.append(
            "  Oracle         : {}.{}".format(
                result.source_schema, result.source_table
            )
        )
        lines.append(
            "  SQL Server     : {}.{}".format(
                result.sqlserver_schema, result.sqlserver_table
            )
        )
        lines.append("  Status         : {}".format(result.status))
        lines.append("  Exported       : {}".format(result.exported_rows))
        lines.append("  Imported       : {}".format(result.imported_rows))
        if result.error_message:
            lines.append("  Error          : {}".format(result.error_message))
        lines.append("")

    if failed:
        lines.append("FAILED TABLES")
        lines.append("-" * 72)
        for result in failed:
            lines.append(
                "  {} - {}".format(result.table, result.error_message or "")
            )
        lines.append("")

    _append_identity_section(lines, sequence_results)

    with open(str(path), "w", encoding="utf-8") as handle:
        handle.write("\n".join(lines))
        handle.write("\n")

    tables_csv = write_migration_tables_csv(path, results)
    sequences_csv = write_sequences_csv(path, sequence_results)
    return path, tables_csv, sequences_csv


def write_identity_sync_report(
    path, sequence_results, started_at, finished_at, config, command
):
    duration = finished_at - started_at
    failed = [r for r in sequence_results if r.status == "FAILED"]

    lines = []
    lines.append("=" * 72)
    lines.append("ORACLE TO SQL SERVER IDENTITY SYNC SUMMARY")
    lines.append("=" * 72)
    lines.append("Command        : {}".format(command))
    lines.append("Started        : {}".format(started_at.strftime("%Y-%m-%d %H:%M:%S")))
    lines.append("Finished       : {}".format(finished_at.strftime("%Y-%m-%d %H:%M:%S")))
    lines.append("Duration       : {}".format(duration))
    lines.append("Status         : {}".format("FAILED" if failed else "SUCCESS"))
    lines.append("")
    _append_identity_section(lines, sequence_results)

    with open(str(path), "w", encoding="utf-8") as handle:
        handle.write("\n".join(lines))
        handle.write("\n")

    sequences_csv = write_sequences_csv(path, sequence_results)
    return path, sequences_csv
