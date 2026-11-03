import csv
import logging
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

logger = logging.getLogger(__name__)

POSTGRES_INTEGER_TYPES = set([
    "integer",
    "smallint",
    "bigint",
    "int2",
    "int4",
    "int8",
])

POSTGRES_NUMERIC_TYPES = POSTGRES_INTEGER_TYPES | set([
    "numeric",
    "decimal",
    "real",
    "double precision",
    "float4",
    "float8",
    "money",
    "smallmoney",
])

POSTGRES_BOOLEAN_TYPES = set([
    "boolean",
    "bool",
])


def split_qualified_name(name):
    parts = [part.strip() for part in name.split(".") if part.strip()]
    if len(parts) == 1:
        return None, parts[0]
    if len(parts) == 2:
        return parts[0], parts[1]
    raise ValueError("Invalid table name '{}'. Use SCHEMA.TABLE or TABLE.".format(name))


def default_export_path(output_dir, schema, table):
    return output_dir / "{}_{}.csv".format(schema.lower(), table.lower())


def _format_bytea(value):
    return "\\x" + bytes(value).hex()


def _format_integer_value(value):
    if isinstance(value, (Decimal, float)):
        return str(int(round(value)))
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str):
        try:
            return str(int(round(float(value))))
        except ValueError:
            return value
    return str(value)


def _format_boolean_value(value):
    if value is None:
        return None
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, Decimal)):
        return "true" if int(value) else "false"
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in ("1", "true", "t", "yes", "y"):
            return "true"
        if lowered in ("0", "false", "f", "no", "n"):
            return "false"
    return str(value)


def _should_export_as_null(value, pg_type):
    if value is None:
        return True
    pg_key = (pg_type or "").lower()
    if pg_key in POSTGRES_NUMERIC_TYPES or pg_key in POSTGRES_BOOLEAN_TYPES:
        if isinstance(value, str) and value.strip() == "":
            return True
    return False


def _normalize_text(value):
    text = str(value)
    return text.replace("\r\n", "\n").replace("\r", "\n")


def format_cell(value, null_marker, is_binary=False, pg_type=None):
    if _should_export_as_null(value, pg_type):
        return null_marker

    if pg_type and pg_type.lower() in POSTGRES_INTEGER_TYPES:
        if isinstance(value, (Decimal, float, int)):
            return _format_integer_value(value)
        if isinstance(value, str):
            try:
                return _format_integer_value(value)
            except (TypeError, ValueError):
                return null_marker

    if pg_type and pg_type.lower() in POSTGRES_BOOLEAN_TYPES:
        return _format_boolean_value(value)

    if is_binary:
        if isinstance(value, (bytes, bytearray, memoryview)):
            return _format_bytea(value)
        return str(value)

    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M:%S")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, (bytes, bytearray, memoryview)):
        return _format_bytea(value)

    return _normalize_text(value)


def _is_csv_number(cell):
    if cell is None:
        return False
    text = str(cell).strip()
    if not text:
        return False
    try:
        float(text)
        return True
    except (TypeError, ValueError):
        return False


def _csv_quote(text):
    quotechar = '"'
    escaped = str(text).replace(quotechar, quotechar + quotechar)
    return quotechar + escaped + quotechar


def _format_csv_field(cell, settings):
    null_marker = settings.null_marker
    delimiter = settings.delimiter

    if cell == null_marker:
        return ""

    text = str(cell)
    if settings.quote_all:
        return _csv_quote(text)

    if settings.quote_nonnumeric:
        if _is_csv_number(text):
            return text
        return _csv_quote(text)

    if delimiter in text or '"' in text or "\n" in text or "\r" in text:
        return _csv_quote(text)
    return text


def _write_csv_line(handle, cells, settings):
    line = settings.delimiter.join(_format_csv_field(cell, settings) for cell in cells)
    handle.write(line)
    handle.write("\n")


def write_csv_rows(
    output_file,
    settings,
    column_names,
    row_iter,
    binary_column_indexes=None,
    column_types=None,
    preformatted=False,
):
    binary_column_indexes = binary_column_indexes or set()
    column_types = column_types or {}
    output_file.parent.mkdir(parents=True, exist_ok=True)
    row_count = 0

    with output_file.open("w", encoding=settings.encoding, newline="") as handle:
        _write_csv_line(handle, column_names, settings)

        for row in row_iter:
            if preformatted:
                cells = row
            else:
                cells = [
                    format_cell(
                        value,
                        settings.null_marker,
                        is_binary=(idx in binary_column_indexes),
                        pg_type=column_types.get(column_names[idx]),
                    )
                    for idx, value in enumerate(row)
                ]
            _write_csv_line(handle, cells, settings)
            row_count += 1

    return row_count
