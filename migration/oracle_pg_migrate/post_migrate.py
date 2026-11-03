import logging
from pathlib import Path

logger = logging.getLogger(__name__)


def _sql_literal(value):
    """Escape a string for safe single-quoted SQL literal substitution."""
    return str(value).replace("'", "''")


def render_post_mig_sql(sql_text, post_cfg):
    replacements = {
        "{{APP_SERVER_HOST}}": _sql_literal(post_cfg.app_server_host),
        "{{PXE_SERVER_HOST}}": _sql_literal(post_cfg.pxe_server_host),
        "{{OLD_APP_SERVER_HOST}}": _sql_literal(post_cfg.old_app_server_host),
    }
    rendered = sql_text
    for placeholder, value in replacements.items():
        rendered = rendered.replace(placeholder, value)
    return rendered


def _split_statements(sql_text):
    statements = []
    for chunk in sql_text.split(";"):
        lines = []
        for line in chunk.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("--"):
                continue
            lines.append(line)
        statement = "\n".join(lines).strip()
        if statement:
            statements.append(statement)
    return statements


def run_post_migration_sql(connection, config):
    post_cfg = config.post_migration
    if not post_cfg or not post_cfg.enabled:
        logger.info("Post-migration SQL skipped (post_migration.enabled=false)")
        return

    sql_path = Path(post_cfg.sql_file)
    if not sql_path.is_absolute():
        # Resolve relative to process cwd (typically the migration/ folder).
        sql_path = Path.cwd() / sql_path

    if not sql_path.exists():
        raise FileNotFoundError(
            "Post-migration SQL file not found: {}".format(sql_path)
        )

    missing = []
    if not post_cfg.app_server_host:
        missing.append("app_server_host")
    if not post_cfg.pxe_server_host:
        missing.append("pxe_server_host")
    if not post_cfg.old_app_server_host:
        missing.append("old_app_server_host")
    if missing:
        raise ValueError(
            "post_migration requires: {}".format(", ".join(missing))
        )

    logger.info(
        "Running post-migration SQL from %s "
        "(app_server_host=%s pxe_server_host=%s old_app_server_host=%s)",
        sql_path,
        post_cfg.app_server_host,
        post_cfg.pxe_server_host,
        post_cfg.old_app_server_host,
    )

    raw_sql = sql_path.read_text(encoding="utf-8")
    rendered = render_post_mig_sql(raw_sql, post_cfg)
    statements = _split_statements(rendered)
    if not statements:
        logger.warning("Post-migration SQL file contained no statements")
        return

    schema = config.postgres.schema
    with connection.cursor() as cursor:
        if schema:
            cursor.execute(
                "SET search_path TO {}".format(
                    '"{}"'.format(schema.replace('"', '""'))
                )
            )
        for index, statement in enumerate(statements, start=1):
            logger.info(
                "Post-migration statement %s/%s: %s...",
                index,
                len(statements),
                " ".join(statement.split())[:120],
            )
            cursor.execute(statement)
            logger.info(
                "Post-migration statement %s/%s done (rowcount=%s)",
                index,
                len(statements),
                cursor.rowcount,
            )
    connection.commit()
    logger.info("Post-migration SQL completed (%s statement(s))", len(statements))
