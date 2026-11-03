from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

MSSQL_INTEGER_TYPES = set([
    "int",
    "bigint",
    "smallint",
    "tinyint",
])

MSSQL_NUMERIC_TYPES = MSSQL_INTEGER_TYPES | set([
    "numeric",
    "decimal",
    "float",
    "real",
    "money",
    "smallmoney",
])

MSSQL_BOOLEAN_TYPES = set([
    "bit",
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


def _format_binary_hex(value):
    return "0x" + bytes(value).hex()


def _format_integer_value(value):
    if isinstance(value, (Decimal, float)):
        return str(int(round(value)))
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str):
        return str(int(round(float(value))))
    return str(value)


def _should_export_as_null(value, target_type):
    if value is None:
        return True
    key = (target_type or "").lower()
    if key in MSSQL_NUMERIC_TYPES or key in MSSQL_BOOLEAN_TYPES:
        if isinstance(value, str) and value.strip() == "":
            return True
    return False


def _normalize_text(value):
    text = str(value)
    return text.replace("\r\n", "\n").replace("\r", "\n")


def format_cell(value, null_marker, is_binary=False, target_type=None):
    if _should_export_as_null(value, target_type):
        return null_marker

    if target_type and target_type.lower() in MSSQL_INTEGER_TYPES:
        if isinstance(value, (Decimal, float, int, str)):
            try:
                return _format_integer_value(value)
            except (TypeError, ValueError):
                return null_marker

    if target_type and target_type.lower() in MSSQL_BOOLEAN_TYPES:
        if isinstance(value, bool):
            return "1" if value else "0"
        if isinstance(value, (int, Decimal)):
            return "1" if int(value) else "0"
        if isinstance(value, str):
            lowered = value.strip().lower()
            if lowered in ("1", "true", "t", "yes", "y"):
                return "1"
            if lowered in ("0", "false", "f", "no", "n"):
                return "0"

    if is_binary:
        if isinstance(value, (bytes, bytearray, memoryview)):
            return _format_binary_hex(value)
        return str(value)

    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M:%S")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, (bytes, bytearray, memoryview)):
        return _format_binary_hex(value)

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
                        target_type=column_types.get(column_names[idx]),
                    )
                    for idx, value in enumerate(row)
                ]
            _write_csv_line(handle, cells, settings)
            row_count += 1

    return row_count
