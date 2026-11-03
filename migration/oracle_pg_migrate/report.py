import csv
from datetime import datetime

from .source_db import SOURCE_SQLSERVER, get_source_schema, source_display_name


def _report_base_path(summary_path):
    return str(summary_path).replace(".summary.txt", "")


def _tables_csv_path(summary_path):
    return _report_base_path(summary_path) + "_tables.csv"


def _validation_csv_path(summary_path):
    return _report_base_path(summary_path) + "_validation.csv"


def _sequences_csv_path(summary_path):
    return _report_base_path(summary_path) + "_sequences.csv"


def _write_csv(path, headers, rows):
    with open(str(path), "w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(headers)
        for row in rows:
            writer.writerow(row)
    return path


def _migration_title(config):
    return "{} TO POSTGRESQL MIGRATION SUMMARY".format(
        source_display_name(config).upper()
    )


def _append_source_section(lines, config):
    source_name = source_display_name(config)
    source_schema = get_source_schema(config)
    lines.append(source_name)
    if config.source.type == SOURCE_SQLSERVER:
        lines.append("  Host           : {}:{}".format(
            config.sqlserver.host, config.sqlserver.port
        ))
        lines.append("  Database       : {}".format(config.sqlserver.database))
        lines.append("  User           : {}".format(config.sqlserver.user))
        lines.append("  Schema         : {}".format(source_schema))
    else:
        lines.append("  User           : {}".format(config.oracle.user))
        lines.append("  DSN            : {}".format(config.oracle.dsn))
        lines.append("  Schema         : {}".format(source_schema))
    lines.append("")


def write_migration_tables_csv(summary_path, results):
    headers = [
        "table_name",
        "source_schema",
        "source_table",
        "postgres_schema",
        "postgres_table",
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
            rows_match = "YES" if result.exported_rows == result.imported_rows else "NO"
        rows.append([
            result.table,
            result.source_schema,
            result.source_table,
            result.postgres_schema,
            result.postgres_table,
            result.status,
            result.exported_rows,
            result.imported_rows,
            rows_match,
            "{:.1f}".format(result.duration_seconds),
            result.csv_file or "",
            result.error_message or "",
        ])

    return _write_csv(_tables_csv_path(summary_path), headers, rows)


def write_validation_issues_csv(summary_path, validation_issues):
    headers = [
        "constraint_type",
        "constraint_name",
        "table_name",
        "violation_count",
        "details",
    ]
    rows = [
        [
            issue.constraint_type,
            issue.constraint_name,
            issue.table_name,
            issue.violation_count,
            issue.details,
        ]
        for issue in validation_issues
    ]
    return _write_csv(_validation_csv_path(summary_path), headers, rows)


def write_validation_tables_csv(summary_path, pairs, validation_issues):
    headers = [
        "table_name",
        "source_schema",
        "source_table",
        "postgres_schema",
        "postgres_table",
        "validation_status",
        "issue_count",
    ]

    issues_by_table = {}
    for issue in validation_issues:
        table_key = issue.table_name.split(".")[-1].lower()
        issues_by_table[table_key] = (
            issues_by_table.get(table_key, 0) + issue.violation_count
        )

    rows = []
    for pair in pairs:
        issue_count = issues_by_table.get(pair.postgres_table.lower(), 0)
        status = "FAILED" if issue_count > 0 else "PASSED"
        rows.append([
            pair.name,
            pair.source_schema,
            pair.source_table,
            pair.postgres_schema,
            pair.postgres_table,
            status,
            issue_count,
        ])

    return _write_csv(_tables_csv_path(summary_path), headers, rows)


def write_sequences_csv(summary_path, sequence_results):
    headers = [
        "source_kind",
        "source_sequence",
        "postgres_sequence",
        "source_next_value",
        "postgres_old_value",
        "postgres_new_value",
        "linked_table",
        "linked_column",
        "max_column_value",
        "status",
        "error_message",
    ]
    rows = [
        [
            getattr(result, "source_kind", "sequence"),
            result.source_sequence,
            result.postgres_sequence,
            result.source_next_value,
            result.postgres_old_value,
            result.postgres_new_value,
            result.linked_table,
            result.linked_column,
            result.max_column_value,
            result.status,
            result.error_message or "",
        ]
        for result in sequence_results
    ]
    return _write_csv(_sequences_csv_path(summary_path), headers, rows)


def _append_sequence_section(lines, sequence_results, source_name="Source"):
    lines.append("-" * 72)
    lines.append("SEQUENCE SYNC")
    lines.append("-" * 72)
    if not sequence_results:
        lines.append("  Status         : NOT RUN")
        lines.append("")
        return

    success = [r for r in sequence_results if r.status == "SUCCESS"]
    failed = [r for r in sequence_results if r.status == "FAILED"]
    skipped = [r for r in sequence_results if r.status == "SKIPPED"]
    lines.append("  Updated        : {}".format(len(success)))
    lines.append("  Skipped        : {}".format(len(skipped)))
    lines.append("  Failed         : {}".format(len(failed)))
    lines.append("")

    for result in sequence_results:
        if result.status != "SUCCESS":
            continue
        lines.append("  {} -> {}.{}".format(
            result.source_sequence,
            "postgres",
            result.postgres_sequence,
        ))
        lines.append("    Source next value : {}".format(result.source_next_value))
        lines.append("    PG old value      : {}".format(result.postgres_old_value))
        lines.append("    PG new next value : {}".format(result.postgres_new_value))
        if result.max_column_value is not None:
            lines.append("    Max column value  : {}".format(result.max_column_value))
        lines.append("")

    if failed or skipped:
        lines.append("  Issues:")
        for result in failed + skipped:
            lines.append(
                "    {} - {} ({})".format(
                    result.source_sequence,
                    result.status,
                    result.error_message or "",
                )
            )
        lines.append("")


def _format_duration(seconds):
    if seconds < 60:
        return "{:.1f}s".format(seconds)
    minutes = int(seconds // 60)
    secs = seconds % 60
    return "{}m {:.1f}s".format(minutes, secs)


def _append_validation_section(lines, validation_issues):
    lines.append("-" * 72)
    lines.append("CONSTRAINT VALIDATION")
    lines.append("-" * 72)
    if validation_issues:
        lines.append("  Issues found   : {}".format(len(validation_issues)))
        for issue in validation_issues:
            lines.append("  Type           : {}".format(issue.constraint_type))
            lines.append("  Constraint     : {}".format(issue.constraint_name))
            lines.append("  Table          : {}".format(issue.table_name))
            lines.append("  Violations     : {}".format(issue.violation_count))
            lines.append("  Details        : {}".format(issue.details))
            lines.append("")
    else:
        lines.append("  Status         : PASSED")
        lines.append("")


def write_validation_report(
    path, validation_issues, started_at, finished_at, config, command, pairs
):
    duration = (finished_at - started_at).total_seconds()
    lines = []
    lines.append("=" * 72)
    lines.append("POSTGRESQL CONSTRAINT VALIDATION REPORT")
    lines.append("=" * 72)
    lines.append("Command          : {}".format(command))
    lines.append("Started          : {}".format(started_at.strftime("%Y-%m-%d %H:%M:%S")))
    lines.append("Finished         : {}".format(finished_at.strftime("%Y-%m-%d %H:%M:%S")))
    lines.append("Duration         : {}".format(_format_duration(duration)))
    if validation_issues:
        lines.append("Overall status   : FAILED")
    else:
        lines.append("Overall status   : PASSED")
    lines.append("")
    lines.append("PostgreSQL")
    lines.append("  Host           : {}:{}".format(config.postgres.host, config.postgres.port))
    lines.append("  Database       : {}".format(config.postgres.database))
    lines.append("  User           : {}".format(config.postgres.user))
    lines.append("  Schema         : {}".format(config.postgres.schema))
    lines.append("")
    lines.append("Tables checked   : {}".format(len(pairs)))
    lines.append("")
    _append_validation_section(lines, validation_issues)
    lines.append("=" * 72)

    with open(str(path), "w", encoding="utf-8") as handle:
        handle.write("\n".join(lines))
        handle.write("\n")

    tables_csv = write_validation_tables_csv(path, pairs, validation_issues)
    validation_csv = write_validation_issues_csv(path, validation_issues)
    sequences_csv = write_sequences_csv(path, [])
    return path, tables_csv, validation_csv, sequences_csv


def write_sequence_sync_report(
    path, sequence_results, started_at, finished_at, config, command
):
    duration = (finished_at - started_at).total_seconds()
    lines = []
    lines.append("=" * 72)
    lines.append("POSTGRESQL SEQUENCE SYNC REPORT")
    lines.append("=" * 72)
    lines.append("Command          : {}".format(command))
    lines.append("Started          : {}".format(started_at.strftime("%Y-%m-%d %H:%M:%S")))
    lines.append("Finished         : {}".format(finished_at.strftime("%Y-%m-%d %H:%M:%S")))
    lines.append("Duration         : {}".format(_format_duration(duration)))
    if any(r.status == "FAILED" for r in sequence_results):
        lines.append("Overall status   : COMPLETED WITH ERRORS")
    else:
        lines.append("Overall status   : SUCCESS")
    lines.append("")
    lines.append("Source type      : {}".format(config.source.type))
    lines.append("Source schema    : {}".format(get_source_schema(config)))
    lines.append("PostgreSQL schema: {}".format(config.postgres.schema))
    lines.append("")
    _append_sequence_section(lines, sequence_results, source_display_name(config))
    lines.append("=" * 72)

    with open(str(path), "w", encoding="utf-8") as handle:
        handle.write("\n".join(lines))
        handle.write("\n")

    sequences_csv = write_sequences_csv(path, sequence_results)
    return path, sequences_csv


def write_summary_report(
    path,
    results,
    started_at,
    finished_at,
    config,
    command,
    validation_issues=None,
    sequence_results=None,
):
    validation_issues = validation_issues or []
    sequence_results = sequence_results or []
    duration = (finished_at - started_at).total_seconds()
    success = [r for r in results if r.status == "SUCCESS"]
    failed = [r for r in results if r.status == "FAILED"]
    total_exported = sum(r.exported_rows for r in success)
    total_imported = sum(r.imported_rows for r in success)

    lines = []
    lines.append("=" * 72)
    lines.append(_migration_title(config))
    lines.append("=" * 72)
    lines.append("Command          : {}".format(command))
    lines.append("Started          : {}".format(started_at.strftime("%Y-%m-%d %H:%M:%S")))
    lines.append("Finished         : {}".format(finished_at.strftime("%Y-%m-%d %H:%M:%S")))
    lines.append("Duration         : {}".format(_format_duration(duration)))
    if failed:
        overall_status = "COMPLETED WITH ERRORS"
    elif validation_issues:
        overall_status = "COMPLETED WITH CONSTRAINT VIOLATIONS"
    elif any(r.status == "FAILED" for r in sequence_results):
        overall_status = "COMPLETED WITH SEQUENCE SYNC ERRORS"
    else:
        overall_status = "SUCCESS"
    lines.append("Overall status   : {}".format(overall_status))
    lines.append("")
    lines.append("Source type      : {}".format(config.source.type))
    _append_source_section(lines, config)
    lines.append("PostgreSQL")
    lines.append("  Host           : {}:{}".format(config.postgres.host, config.postgres.port))
    lines.append("  Database       : {}".format(config.postgres.database))
    lines.append("  User           : {}".format(config.postgres.user))
    lines.append("  Schema         : {}".format(config.postgres.schema))
    lines.append("")
    lines.append("Options")
    lines.append("  Truncate before import      : {}".format(config.migrate.truncate_before_import))
    lines.append("  Truncate all upfront        : {}".format(config.migrate.truncate_all_before_migration))
    lines.append("  Truncate cascade            : {}".format(config.migrate.truncate_cascade))
    lines.append("  Restart identity            : {}".format(config.migrate.restart_identity))
    lines.append("  Disable triggers            : {}".format(config.migrate.disable_triggers))
    lines.append("  Disable foreign keys        : {}".format(config.migrate.disable_foreign_keys))
    lines.append("  Disable row security        : {}".format(config.migrate.disable_row_security))
    lines.append("  Analyze after migration     : {}".format(config.migrate.analyze_after_migration))
    lines.append(
        "  Validate constraints        : {}".format(
            config.migrate.validate_constraints_after_migration
        )
    )
    lines.append(
        "  Sync sequences              : {}".format(
            config.migrate.sync_sequences_after_migration
        )
    )
    lines.append("  Lowercase columns           : {}".format(config.migrate.lowercase_columns))
    lines.append("")
    lines.append("Results")
    lines.append("  Tables succeeded : {}".format(len(success)))
    lines.append("  Tables failed    : {}".format(len(failed)))
    lines.append("  Rows exported    : {}".format(total_exported))
    lines.append("  Rows imported    : {}".format(total_imported))
    lines.append("")
    lines.append("-" * 72)
    lines.append("TABLE DETAILS")
    lines.append("-" * 72)

    for result in results:
        lines.append("Table            : {}".format(result.table))
        lines.append("  Source         : {}.{}".format(result.source_schema, result.source_table))
        lines.append("  PostgreSQL     : {}.{}".format(result.postgres_schema, result.postgres_table))
        lines.append("  Status         : {}".format(result.status))
        if result.status == "SUCCESS":
            lines.append("  Rows exported  : {}".format(result.exported_rows))
            lines.append("  Rows imported  : {}".format(result.imported_rows))
            lines.append("  CSV file       : {}".format(result.csv_file))
            lines.append("  Duration       : {}".format(_format_duration(result.duration_seconds)))
        else:
            lines.append("  Error          : {}".format(result.error_message))
        lines.append("")

    if failed:
        lines.append("-" * 72)
        lines.append("FAILED TABLES")
        lines.append("-" * 72)
        for result in failed:
            lines.append("  {} - {}".format(result.table, result.error_message))
        lines.append("")

    _append_sequence_section(lines, sequence_results, source_display_name(config))
    _append_validation_section(lines, validation_issues)
    lines.append("=" * 72)

    with open(str(path), "w", encoding="utf-8") as handle:
        handle.write("\n".join(lines))
        handle.write("\n")

    tables_csv = write_migration_tables_csv(path, results)
    validation_csv = write_validation_issues_csv(path, validation_issues)
    sequences_csv = write_sequences_csv(path, sequence_results)
    return path, tables_csv, validation_csv, sequences_csv
