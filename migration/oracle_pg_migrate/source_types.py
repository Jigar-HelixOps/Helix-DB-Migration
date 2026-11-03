SOURCE_ORACLE = "oracle"
SOURCE_SQLSERVER = "sqlserver"
SUPPORTED_SOURCE_TYPES = (SOURCE_ORACLE, SOURCE_SQLSERVER)


def normalize_source_type(source_type):
    normalized = (source_type or SOURCE_ORACLE).strip().lower()
    if normalized in ("mssql", "sql_server", "sql-server"):
        normalized = SOURCE_SQLSERVER
    if normalized not in SUPPORTED_SOURCE_TYPES:
        raise ValueError(
            "Unsupported source type '{}'. Supported values: oracle, sqlserver".format(
                source_type
            )
        )
    return normalized
