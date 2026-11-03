import os
from pathlib import Path

import yaml

from .source_types import SOURCE_ORACLE, normalize_source_type


class SourceSettings(object):
    def __init__(self, source_type):
        self.type = normalize_source_type(source_type)


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


class PostgresConfig(object):
    def __init__(self, host, port, database, user, password, schema):
        self.host = host
        self.port = port
        self.database = database
        self.user = user
        self.password = password
        self.schema = schema


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
    def __init__(self, truncate_before_import, use_copy, batch_size):
        self.truncate_before_import = truncate_before_import
        self.use_copy = use_copy
        self.batch_size = batch_size


class MigrateSettings(object):
    def __init__(
        self,
        tables,
        exclude_tables,
        truncate_before_import,
        lowercase_columns,
        truncate_all_before_migration,
        truncate_cascade,
        restart_identity,
        disable_triggers,
        disable_foreign_keys,
        disable_row_security,
        analyze_after_migration,
        validate_constraints_after_migration,
        sync_sequences_after_migration,
        column_mapping_file,
        truncate_cascade_single_table,
        partition_column_handling,
    ):
        self.tables = tables
        self.exclude_tables = exclude_tables
        self.truncate_before_import = truncate_before_import
        self.lowercase_columns = lowercase_columns
        self.truncate_all_before_migration = truncate_all_before_migration
        self.truncate_cascade = truncate_cascade
        self.restart_identity = restart_identity
        self.disable_triggers = disable_triggers
        self.disable_foreign_keys = disable_foreign_keys
        self.disable_row_security = disable_row_security
        self.analyze_after_migration = analyze_after_migration
        self.validate_constraints_after_migration = validate_constraints_after_migration
        self.sync_sequences_after_migration = sync_sequences_after_migration
        self.column_mapping_file = column_mapping_file
        self.truncate_cascade_single_table = truncate_cascade_single_table
        self.partition_column_handling = partition_column_handling


class LoggingSettings(object):
    def __init__(self, log_dir):
        self.log_dir = log_dir


class PostMigrationSettings(object):
    def __init__(
        self,
        enabled,
        sql_file,
        app_server_host,
        pxe_server_host,
        old_app_server_host,
    ):
        self.enabled = enabled
        self.sql_file = sql_file
        self.app_server_host = app_server_host
        self.pxe_server_host = pxe_server_host
        self.old_app_server_host = old_app_server_host


class AppConfig(object):
    def __init__(
        self,
        source,
        oracle,
        sqlserver,
        postgres,
        export,
        import_,
        migrate,
        logging,
        column_mappings,
        global_column_mappings,
        post_migration,
    ):
        self.source = source
        self.oracle = oracle
        self.sqlserver = sqlserver
        self.postgres = postgres
        self.export = export
        self.import_ = import_
        self.migrate = migrate
        self.logging = logging
        self.column_mappings = column_mappings
        self.global_column_mappings = global_column_mappings
        self.post_migration = post_migration


def _require(data, key, section):
    if key not in data or data[key] in (None, ""):
        raise ValueError("Missing required config value: {}.{}".format(section, key))
    return data[key]


def _load_oracle_config(oracle_raw, source_type):
    if source_type != SOURCE_ORACLE:
        return OracleConfig(
            user=oracle_raw.get("user", ""),
            password=oracle_raw.get("password", ""),
            dsn=oracle_raw.get("dsn", ""),
            schema=oracle_raw.get("schema", ""),
        )

    return OracleConfig(
        user=_require(oracle_raw, "user", "oracle"),
        password=_require(oracle_raw, "password", "oracle"),
        dsn=_require(oracle_raw, "dsn", "oracle"),
        schema=_require(oracle_raw, "schema", "oracle"),
    )


def _load_sqlserver_config(sqlserver_raw, source_type):
    if source_type != "sqlserver":
        return SqlServerConfig(
            host=sqlserver_raw.get("host", ""),
            port=int(sqlserver_raw.get("port", 1433) or 1433),
            database=sqlserver_raw.get("database", ""),
            user=sqlserver_raw.get("user", ""),
            password=sqlserver_raw.get("password", ""),
            schema=sqlserver_raw.get("schema", "dbo"),
            driver=sqlserver_raw.get("driver", ""),
            connection_string=sqlserver_raw.get("connection_string", ""),
            trust_server_certificate=bool(
                sqlserver_raw.get("trust_server_certificate", True)
            ),
        )

    return SqlServerConfig(
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


def load_config(path):
    config_path = Path(path)
    if not config_path.exists():
        raise FileNotFoundError("Config file not found: {}".format(config_path))

    with config_path.open("r", encoding="utf-8") as handle:
        raw = yaml.safe_load(handle) or {}

    source_raw = raw.get("source", {})
    oracle_raw = raw.get("oracle", {})
    sqlserver_raw = raw.get("sqlserver", {})
    postgres_raw = raw.get("postgres", {})
    export_raw = raw.get("export", {})
    import_raw = raw.get("import", {})
    migrate_raw = raw.get("migrate", {})
    logging_raw = raw.get("logging", {})
    post_raw = raw.get("post_migration", {}) or {}

    source_type = normalize_source_type(source_raw.get("type", SOURCE_ORACLE))
    source = SourceSettings(source_type=source_type)

    oracle = _load_oracle_config(oracle_raw, source_type)
    sqlserver = _load_sqlserver_config(sqlserver_raw, source_type)

    postgres = PostgresConfig(
        host=_require(postgres_raw, "host", "postgres"),
        port=int(postgres_raw.get("port", 5432)),
        database=_require(postgres_raw, "database", "postgres"),
        user=_require(postgres_raw, "user", "postgres"),
        password=_require(postgres_raw, "password", "postgres"),
        schema=_require(postgres_raw, "schema", "postgres"),
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
        exclude_tables=[str(name) for name in migrate_raw.get("exclude_tables", []) or []],
        truncate_before_import=truncate_before_import,
        lowercase_columns=bool(migrate_raw.get("lowercase_columns", True)),
        truncate_all_before_migration=bool(
            migrate_raw.get("truncate_all_before_migration", truncate_before_import)
        ),
        truncate_cascade=bool(migrate_raw.get("truncate_cascade", True)),
        restart_identity=bool(migrate_raw.get("restart_identity", True)),
        disable_triggers=bool(migrate_raw.get("disable_triggers", True)),
        disable_foreign_keys=bool(migrate_raw.get("disable_foreign_keys", True)),
        disable_row_security=bool(migrate_raw.get("disable_row_security", True)),
        analyze_after_migration=bool(migrate_raw.get("analyze_after_migration", True)),
        validate_constraints_after_migration=bool(
            migrate_raw.get("validate_constraints_after_migration", True)
        ),
        sync_sequences_after_migration=bool(
            migrate_raw.get("sync_sequences_after_migration", True)
        ),
        column_mapping_file=migrate_raw.get("column_mapping_file", ""),
        truncate_cascade_single_table=bool(
            migrate_raw.get("truncate_cascade_single_table", False)
        ),
        partition_column_handling=bool(
            migrate_raw.get("partition_column_handling", True)
        ),
    )

    import_settings = ImportSettings(
        truncate_before_import=migrate.truncate_before_import,
        use_copy=bool(import_raw.get("use_copy", True)),
        batch_size=int(import_raw.get("batch_size", 5000)),
    )

    logging_settings = LoggingSettings(
        log_dir=Path(logging_raw.get("log_dir", "./logs")),
    )

    post_migration = PostMigrationSettings(
        enabled=bool(post_raw.get("enabled", True)),
        sql_file=post_raw.get("sql_file", "./post_mig.sql"),
        app_server_host=str(post_raw.get("app_server_host", "") or ""),
        pxe_server_host=str(post_raw.get("pxe_server_host", "") or ""),
        old_app_server_host=str(post_raw.get("old_app_server_host", "") or ""),
    )

    from .column_mapping import load_column_mappings

    mapping_file = migrate.column_mapping_file or raw.get("column_mapping_file", "")
    column_mappings, global_column_mappings = load_column_mappings(mapping_file)

    return AppConfig(
        source=source,
        oracle=oracle,
        sqlserver=sqlserver,
        postgres=postgres,
        export=export,
        import_=import_settings,
        migrate=migrate,
        logging=logging_settings,
        column_mappings=column_mappings,
        global_column_mappings=global_column_mappings,
        post_migration=post_migration,
    )


def merge_env_overrides(config):
    if os.getenv("SOURCE_TYPE"):
        config.source.type = normalize_source_type(os.getenv("SOURCE_TYPE"))

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

    config.postgres.host = os.getenv("PGHOST", config.postgres.host)
    config.postgres.port = int(os.getenv("PGPORT", str(config.postgres.port)))
    config.postgres.database = os.getenv("PGDATABASE", config.postgres.database)
    config.postgres.user = os.getenv("PGUSER", config.postgres.user)
    config.postgres.password = os.getenv("PGPASSWORD", config.postgres.password)
    if os.getenv("PGSCHEMA"):
        config.postgres.schema = os.getenv("PGSCHEMA")

    if config.post_migration:
        if os.getenv("POST_MIG_APP_SERVER_HOST"):
            config.post_migration.app_server_host = os.getenv(
                "POST_MIG_APP_SERVER_HOST"
            )
        if os.getenv("POST_MIG_PXE_SERVER_HOST"):
            config.post_migration.pxe_server_host = os.getenv(
                "POST_MIG_PXE_SERVER_HOST"
            )
        if os.getenv("POST_MIG_OLD_APP_SERVER_HOST"):
            config.post_migration.old_app_server_host = os.getenv(
                "POST_MIG_OLD_APP_SERVER_HOST"
            )
        if os.getenv("POST_MIG_ENABLED"):
            config.post_migration.enabled = os.getenv(
                "POST_MIG_ENABLED"
            ).strip().lower() in ("1", "true", "yes", "y")

    return config
