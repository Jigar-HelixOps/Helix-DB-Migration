import logging

from psycopg2 import sql

logger = logging.getLogger(__name__)


class ValidationIssue(object):
    def __init__(self, constraint_type, constraint_name, table_name, violation_count, details):
        self.constraint_type = constraint_type
        self.constraint_name = constraint_name
        self.table_name = table_name
        self.violation_count = violation_count
        self.details = details


def _table_filter(table_names):
    if not table_names:
        return None
    return {name.lower() for name in table_names}


def _should_check_table(table_name, table_names):
    if not table_names:
        return True
    return table_name.lower() in table_names


def _get_foreign_keys(connection, schema):
    query = """
        SELECT
            c.conname,
            c.conrelid::regclass::text AS child_table,
            c.confrelid::regclass::text AS parent_table,
            (
                SELECT string_agg(quote_ident(a.attname), ', ' ORDER BY u.ord)
                FROM unnest(c.conkey) WITH ORDINALITY AS u(attnum, ord)
                JOIN pg_attribute a
                  ON a.attnum = u.attnum
                 AND a.attrelid = c.conrelid
                 AND NOT a.attisdropped
            ) AS child_cols,
            (
                SELECT string_agg(quote_ident(a.attname), ', ' ORDER BY u.ord)
                FROM unnest(c.confkey) WITH ORDINALITY AS u(attnum, ord)
                JOIN pg_attribute a
                  ON a.attnum = u.attnum
                 AND a.attrelid = c.confrelid
                 AND NOT a.attisdropped
            ) AS parent_cols,
            (
                SELECT string_agg(format('c.%%s IS NOT NULL', quote_ident(a.attname)), ' AND ' ORDER BY u.ord)
                FROM unnest(c.conkey) WITH ORDINALITY AS u(attnum, ord)
                JOIN pg_attribute a
                  ON a.attnum = u.attnum
                 AND a.attrelid = c.conrelid
                 AND NOT a.attisdropped
            ) AS child_not_null_expr,
            (
                SELECT string_agg(
                    format(
                        'p.%%s IS NOT DISTINCT FROM c.%%s',
                        quote_ident(pa.attname),
                        quote_ident(ca.attname)
                    ),
                    ' AND ' ORDER BY u.ord
                )
                FROM unnest(c.conkey) WITH ORDINALITY AS u(child_attnum, ord)
                JOIN unnest(c.confkey) WITH ORDINALITY AS v(parent_attnum, ord2)
                  ON u.ord = v.ord2
                JOIN pg_attribute ca
                  ON ca.attnum = u.child_attnum
                 AND ca.attrelid = c.conrelid
                 AND NOT ca.attisdropped
                JOIN pg_attribute pa
                  ON pa.attnum = v.parent_attnum
                 AND pa.attrelid = c.confrelid
                 AND NOT pa.attisdropped
            ) AS join_expr
        FROM pg_constraint c
        JOIN pg_class cl ON cl.oid = c.conrelid
        JOIN pg_namespace n ON n.oid = cl.relnamespace
        WHERE c.contype = 'f'
          AND n.nspname = %s
        ORDER BY c.conname
    """
    with connection.cursor() as cursor:
        cursor.execute(query, (schema,))
        return cursor.fetchall()


def _validate_foreign_keys(connection, schema, table_names):
    issues = []
    for row in _get_foreign_keys(connection, schema):
        conname, child_table, parent_table, child_cols, parent_cols, not_null_expr, join_expr = row
        child_name = child_table.split(".")[-1]
        if not _should_check_table(child_name, table_names):
            continue

        violation_sql = """
            SELECT COUNT(*)
            FROM {child} c
            WHERE {not_null_expr}
              AND NOT EXISTS (
                SELECT 1
                FROM {parent} p
                WHERE {join_expr}
              )
        """.format(
            child=child_table,
            parent=parent_table,
            not_null_expr=not_null_expr,
            join_expr=join_expr,
        )

        with connection.cursor() as cursor:
            cursor.execute(violation_sql)
            violation_count = cursor.fetchone()[0]

        if violation_count:
            details = "orphan rows in {} referencing {}".format(child_table, parent_table)
            issues.append(
                ValidationIssue("FOREIGN KEY", conname, child_table, violation_count, details)
            )
            logger.warning(
                "FK violation: %s on %s (%s rows)",
                conname,
                child_table,
                violation_count,
            )
        else:
            logger.info("FK valid: %s on %s", conname, child_table)

    return issues


def _get_unique_constraints(connection, schema):
    query = """
        SELECT
            c.conname,
            c.conrelid::regclass::text AS table_name,
            (
                SELECT string_agg(quote_ident(a.attname), ', ' ORDER BY u.ord)
                FROM unnest(c.conkey) WITH ORDINALITY AS u(attnum, ord)
                JOIN pg_attribute a
                  ON a.attnum = u.attnum
                 AND a.attrelid = c.conrelid
                 AND NOT a.attisdropped
            ) AS columns
        FROM pg_constraint c
        JOIN pg_class cl ON cl.oid = c.conrelid
        JOIN pg_namespace n ON n.oid = cl.relnamespace
        WHERE c.contype = 'u'
          AND n.nspname = %s
        ORDER BY c.conname
    """
    with connection.cursor() as cursor:
        cursor.execute(query, (schema,))
        return cursor.fetchall()


def _validate_unique_constraints(connection, schema, table_names):
    issues = []
    for conname, table_name, columns in _get_unique_constraints(connection, schema):
        rel_name = table_name.split(".")[-1]
        if not _should_check_table(rel_name, table_names):
            continue

        violation_sql = """
            SELECT COUNT(*) FROM (
                SELECT {columns}
                FROM {table_name}
                GROUP BY {columns}
                HAVING COUNT(*) > 1
            ) duplicates
        """.format(columns=columns, table_name=table_name)

        with connection.cursor() as cursor:
            cursor.execute(violation_sql)
            duplicate_groups = cursor.fetchone()[0]

        if duplicate_groups:
            details = "duplicate values for columns ({})".format(columns)
            issues.append(
                ValidationIssue("UNIQUE", conname, table_name, duplicate_groups, details)
            )
            logger.warning(
                "UNIQUE violation: %s on %s (%s duplicate groups)",
                conname,
                table_name,
                duplicate_groups,
            )
        else:
            logger.info("UNIQUE valid: %s on %s", conname, table_name)

    return issues


def _get_not_null_columns(connection, schema):
    query = """
        SELECT
            c.relname AS table_name,
            a.attname AS column_name
        FROM pg_attribute a
        JOIN pg_class c ON c.oid = a.attrelid
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = %s
          AND a.attnum > 0
          AND NOT a.attisdropped
          AND a.attnotnull
          AND c.relkind = 'r'
        ORDER BY c.relname, a.attname
    """
    with connection.cursor() as cursor:
        cursor.execute(query, (schema,))
        return cursor.fetchall()


def _validate_not_null_columns(connection, schema, table_names):
    issues = []
    for table_name, column_name in _get_not_null_columns(connection, schema):
        if not _should_check_table(table_name, table_names):
            continue

        query = sql.SQL("SELECT COUNT(*) FROM {}.{} WHERE {} IS NULL").format(
            sql.Identifier(schema),
            sql.Identifier(table_name),
            sql.Identifier(column_name),
        )
        with connection.cursor() as cursor:
            cursor.execute(query)
            violation_count = cursor.fetchone()[0]

        if violation_count:
            details = "NULL values in NOT NULL column {}".format(column_name)
            issues.append(
                ValidationIssue(
                    "NOT NULL",
                    "{}.{}".format(table_name, column_name),
                    "{}.{}".format(schema, table_name),
                    violation_count,
                    details,
                )
            )
            logger.warning(
                "NOT NULL violation: %s.%s (%s rows)",
                table_name,
                column_name,
                violation_count,
            )

    return issues


def _get_unvalidated_constraints(connection, schema):
    query = """
        SELECT
            c.conname,
            c.contype,
            c.conrelid::regclass::text AS table_name
        FROM pg_constraint c
        JOIN pg_class cl ON cl.oid = c.conrelid
        JOIN pg_namespace n ON n.oid = cl.relnamespace
        WHERE n.nspname = %s
          AND NOT c.convalidated
          AND c.contype IN ('c', 'f')
        ORDER BY c.conname
    """
    with connection.cursor() as cursor:
        cursor.execute(query, (schema,))
        return cursor.fetchall()


def _validate_pending_constraints(connection, schema, table_names):
    issues = []
    type_names = {"c": "CHECK", "f": "FOREIGN KEY"}

    for conname, contype, table_name in _get_unvalidated_constraints(connection, schema):
        rel_name = table_name.split(".")[-1]
        if not _should_check_table(rel_name, table_names):
            continue

        parts = table_name.split(".")
        if len(parts) == 2:
            table_schema, table_only = parts[0], parts[1]
        else:
            table_schema, table_only = schema, rel_name

        query = sql.SQL("ALTER TABLE {}.{} VALIDATE CONSTRAINT {}").format(
            sql.Identifier(table_schema),
            sql.Identifier(table_only),
            sql.Identifier(conname),
        )

        try:
            with connection.cursor() as cursor:
                cursor.execute(query)
            connection.commit()
            logger.info(
                "Validated %s constraint %s on %s",
                type_names.get(contype, contype),
                conname,
                table_name,
            )
        except Exception as exc:
            connection.rollback()
            details = str(exc).strip()
            issues.append(
                ValidationIssue(
                    type_names.get(contype, contype),
                    conname,
                    table_name,
                    1,
                    details,
                )
            )
            logger.warning(
                "Could not validate %s on %s: %s",
                conname,
                table_name,
                details,
            )

    return issues


def validate_constraints(connection, config, pairs):
    schema = config.postgres.schema
    table_names = [pair.postgres_table for pair in pairs]
    issues = []

    logger.info("Starting PostgreSQL constraint validation for schema %s", schema)

    try:
        issues.extend(_validate_foreign_keys(connection, schema, table_names))
        issues.extend(_validate_unique_constraints(connection, schema, table_names))
        issues.extend(_validate_not_null_columns(connection, schema, table_names))
        issues.extend(_validate_pending_constraints(connection, schema, table_names))
    except Exception as exc:
        connection.rollback()
        logger.exception("Constraint validation failed")
        issues.append(
            ValidationIssue(
                "VALIDATION",
                "constraint_validation",
                schema,
                1,
                str(exc),
            )
        )

    if issues:
        logger.warning("Constraint validation found %s issue(s)", len(issues))
    else:
        logger.info("Constraint validation passed with no issues")

    return issues
