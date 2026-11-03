import os
from pathlib import Path

import yaml


class OracleConfig(object):
    def __init__(self, user, password, dsn, schema):
        self.user = user
        self.password = password
        self.dsn = dsn
        self.schema = schema


class SqlServerConfig(object):
    def __init__(
        self,
        host,
        port,
        database,
        user,
        password,
        schema,
        driver,
        connection_string,
        trust_server_certificate,
    ):
        self.host = host
        self.port = port
        self.database = database
        self.user = user
        self.password = password
        self.schema = schema
        self.driver = driver
        self.connection_string = connection_string
        self.trust_server_certificate = trust_server_certificate

    def build_connection_string(self):
        if self.connection_string:
            return self.connection_string

        driver = self.driver or "ODBC Driver 17 for SQL Server"
        server = self.host
        if self.port:
            server = "{},{}".format(self.host, self.port)

        parts = [
            "DRIVER={{{}}}".format(driver),
            "SERVER={}".format(server),
            "DATABASE={}".format(self.database),
            "UID={}".format(self.user),
            "PWD={}".format(self.password),
        ]
        if self.trust_server_certificate:
            parts.append("TrustServerCertificate=yes")
        return ";".join(parts)


class ExportSettings(object):
    def __init__(
        self,
        output_dir,
        delimiter,
        encoding,
        null_marker,
        fetch_size,
        quote_all,
        quote_nonnumeric,
    ):
        self.output_dir = output_dir
        self.delimiter = delimiter
        self.encoding = encoding
        self.null_marker = null_marker
        self.fetch_size = fetch_size
        self.quote_all = quote_all
        self.quote_nonnumeric = quote_nonnumeric


class ImportSettings(object):
    def __init__(self, batch_size):
        self.batch_size = batch_size


class MigrateSettings(object):
    def __init__(
        self,
        tables,
        exclude_tables,
        truncate_before_import,
        lowercase_columns,
        truncate_all_before_migration,
        disable_foreign_keys,
        disable_triggers,
        restart_identity,
        sync_identities_after_migration,
        validate_after_migration,
        column_mapping_file,
    ):
        self.tables = tables
        self.exclude_tables = exclude_tables
        self.truncate_before_import = truncate_before_import
        self.lowercase_columns = lowercase_columns
        self.truncate_all_before_migration = truncate_all_before_migration
        self.disable_foreign_keys = disable_foreign_keys
        self.disable_triggers = disable_triggers
        self.restart_identity = restart_identity
        self.sync_identities_after_migration = sync_identities_after_migration
        self.validate_after_migration = validate_after_migration
        self.column_mapping_file = column_mapping_file


class LoggingSettings(object):
    def __init__(self, log_dir):
        self.log_dir = log_dir


class AppConfig(object):
    def __init__(
        self,
        oracle,
        sqlserver,
        export,
        import_,
        migrate,
        logging,
        column_mappings,
        global_column_mappings,
    ):
        self.oracle = oracle
        self.sqlserver = sqlserver
        self.export = export
        self.import_ = import_
        self.migrate = migrate
        self.logging = logging
        self.column_mappings = column_mappings
        self.global_column_mappings = global_column_mappings


def _require(data, key, section):
    if key not in data or data[key] in (None, ""):
        raise ValueError("Missing required config value: {}.{}".format(section, key))
    return data[key]


def load_config(path):
    config_path = Path(path)
    if not config_path.exists():
        raise FileNotFoundError("Config file not found: {}".format(config_path))

    with config_path.open("r", encoding="utf-8") as handle:
        raw = yaml.safe_load(handle) or {}

    oracle_raw = raw.get("oracle", {})
    sqlserver_raw = raw.get("sqlserver", {})
    export_raw = raw.get("export", {})
    import_raw = raw.get("import", {})
    migrate_raw = raw.get("migrate", {})
    logging_raw = raw.get("logging", {})

    oracle = OracleConfig(
        user=_require(oracle_raw, "user", "oracle"),
        password=_require(oracle_raw, "password", "oracle"),
        dsn=_require(oracle_raw, "dsn", "oracle"),
        schema=_require(oracle_raw, "schema", "oracle"),
    )

    sqlserver = SqlServerConfig(
        host=_require(sqlserver_raw, "host", "sqlserver"),
        port=int(sqlserver_raw.get("port", 1433)),
        database=_require(sqlserver_raw, "database", "sqlserver"),
        user=_require(sqlserver_raw, "user", "sqlserver"),
        password=_require(sqlserver_raw, "password", "sqlserver"),
        schema=sqlserver_raw.get("schema", "dbo"),
        driver=sqlserver_raw.get("driver", ""),
        connection_string=sqlserver_raw.get("connection_string", ""),
        trust_server_certificate=bool(
            sqlserver_raw.get("trust_server_certificate", True)
        ),
    )

    export = ExportSettings(
        output_dir=Path(export_raw.get("output_dir", "./export_data")),
        delimiter=export_raw.get("delimiter", ","),
        encoding=export_raw.get("encoding", "utf-8"),
        null_marker=export_raw.get("null_marker", ""),
        fetch_size=int(export_raw.get("fetch_size", 10000)),
        quote_all=bool(export_raw.get("quote_all", False)),
        quote_nonnumeric=bool(export_raw.get("quote_nonnumeric", True)),
    )

    truncate_before_import = bool(
        migrate_raw.get(
            "truncate_before_import",
            import_raw.get("truncate_before_import", False),
        )
    )

    migrate = MigrateSettings(
        tables=[str(name) for name in migrate_raw.get("tables", []) or []],
        exclude_tables=[
            str(name) for name in migrate_raw.get("exclude_tables", []) or []
        ],
        truncate_before_import=truncate_before_import,
        lowercase_columns=bool(migrate_raw.get("lowercase_columns", False)),
        truncate_all_before_migration=bool(
            migrate_raw.get("truncate_all_before_migration", truncate_before_import)
        ),
        disable_foreign_keys=bool(migrate_raw.get("disable_foreign_keys", True)),
        disable_triggers=bool(migrate_raw.get("disable_triggers", True)),
        restart_identity=bool(migrate_raw.get("restart_identity", True)),
        sync_identities_after_migration=bool(
            migrate_raw.get("sync_identities_after_migration", True)
        ),
        validate_after_migration=bool(
            migrate_raw.get("validate_after_migration", False)
        ),
        column_mapping_file=migrate_raw.get("column_mapping_file", ""),
    )

    import_settings = ImportSettings(
        batch_size=int(import_raw.get("batch_size", 5000)),
    )

    logging_settings = LoggingSettings(
        log_dir=Path(logging_raw.get("log_dir", "./logs")),
    )

    from .column_mapping import load_column_mappings

    mapping_file = migrate.column_mapping_file or raw.get("column_mapping_file", "")
    column_mappings, global_column_mappings = load_column_mappings(mapping_file)

    return AppConfig(
        oracle=oracle,
        sqlserver=sqlserver,
        export=export,
        import_=import_settings,
        migrate=migrate,
        logging=logging_settings,
        column_mappings=column_mappings,
        global_column_mappings=global_column_mappings,
    )


def merge_env_overrides(config):
    config.oracle.user = os.getenv("ORACLE_USER", config.oracle.user)
    config.oracle.password = os.getenv("ORACLE_PASSWORD", config.oracle.password)
    config.oracle.dsn = os.getenv("ORACLE_DSN", config.oracle.dsn)
    if os.getenv("ORACLE_SCHEMA"):
        config.oracle.schema = os.getenv("ORACLE_SCHEMA")

    config.sqlserver.host = os.getenv("MSSQL_HOST", config.sqlserver.host)
    if os.getenv("MSSQL_PORT"):
        config.sqlserver.port = int(os.getenv("MSSQL_PORT"))
    config.sqlserver.database = os.getenv("MSSQL_DATABASE", config.sqlserver.database)
    config.sqlserver.user = os.getenv("MSSQL_USER", config.sqlserver.user)
    config.sqlserver.password = os.getenv("MSSQL_PASSWORD", config.sqlserver.password)
    if os.getenv("MSSQL_SCHEMA"):
        config.sqlserver.schema = os.getenv("MSSQL_SCHEMA")
    if os.getenv("MSSQL_DRIVER"):
        config.sqlserver.driver = os.getenv("MSSQL_DRIVER")
    if os.getenv("MSSQL_CONNECTION_STRING"):
        config.sqlserver.connection_string = os.getenv("MSSQL_CONNECTION_STRING")

    return config
