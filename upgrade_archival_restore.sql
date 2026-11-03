DO $do$
BEGIN
    EXECUTE $archival_stmt$
CREATE OR REPLACE FUNCTION pg_sql_ident(p_name TEXT)
RETURNS TEXT
LANGUAGE sql
IMMUTABLE
AS $$
    SELECT lower(trim(p_name));
$$;
$archival_stmt$;
END;
$do$;

DO $do$
BEGIN
    EXECUTE $archival_stmt$
-- PostgreSQL COPY on Windows needs forward slashes (C:/Test/file.csv).
CREATE OR REPLACE FUNCTION pg_normalize_file_path(p_path TEXT)
RETURNS TEXT
LANGUAGE sql
IMMUTABLE
AS $$
    SELECT replace(trim(p_path), E'\\', '/');
$$;
$archival_stmt$;
END;
$do$;

DO $do$
BEGIN
    EXECUTE $archival_stmt$
CREATE OR REPLACE FUNCTION sp_new_job(
    p_process TEXT,
    p_start_month TEXT DEFAULT NULL,
    p_end_month TEXT DEFAULT NULL
)
RETURNS NUMERIC
LANGUAGE plpgsql
AS $$
DECLARE
    v_job_id NUMERIC(38, 0);
    v_month TEXT := to_char(current_date, 'YYYYMM');
    v_has_restore_cfg BOOLEAN := FALSE;
BEGIN
    SELECT COALESCE(MAX(job_id), 0) + 1
    INTO v_job_id
    FROM (
        SELECT job_id FROM archival_summary
        UNION ALL
        SELECT job_id FROM archival_retrieve_summary
    ) jobs;

    IF UPPER(p_process) = 'ARCHIVAL' THEN
        INSERT INTO archival_summary (job_id, domain_name, data_retention_months, archival_date, status)
        SELECT v_job_id, domain_name, data_retention_months, now(), 'WAITING'
        FROM archival_configuration;

        RAISE NOTICE '[DEBUG sp_new_job] ARCHIVAL job_id=% created; domains from archival_configuration=%',
            v_job_id,
            (SELECT COUNT(*) FROM archival_summary WHERE job_id = v_job_id);
    ELSIF UPPER(p_process) = 'RESTORE' THEN
        SELECT EXISTS (SELECT 1 FROM retrival_configuration) INTO v_has_restore_cfg;

        INSERT INTO archival_retrieve_summary (job_id, domain_name, start_month, end_month, retrieve_date, status)
        SELECT v_job_id,
               rc.domain_name,
               COALESCE(NULLIF(rc.start_month, ''), p_start_month, v_month),
               COALESCE(NULLIF(rc.end_month, ''), p_end_month, v_month),
               now(),
               'WAITING'
        FROM retrival_configuration rc
        WHERE v_has_restore_cfg = TRUE;

        IF NOT v_has_restore_cfg THEN
            INSERT INTO archival_retrieve_summary (job_id, domain_name, start_month, end_month, retrieve_date, status)
            SELECT v_job_id,
                   ac.domain_name,
                   COALESCE(p_start_month, v_month),
                   COALESCE(p_end_month, v_month),
                   now(),
                   'WAITING'
            FROM archival_configuration ac;
        END IF;
    ELSE
        RAISE EXCEPTION 'Invalid process value: %, expected ARCHIVAL or RESTORE', p_process;
    END IF;

    RETURN v_job_id;
END;
$$;
$archival_stmt$;
END;
$do$;

DO $do$
BEGIN
    EXECUTE $archival_stmt$
CREATE OR REPLACE PROCEDURE set_archival_last_run(
    p_step_name TEXT,
    p_table_name TEXT,
    p_action TEXT,
    p_start_time TIMESTAMP DEFAULT NULL,
    p_error_msg TEXT DEFAULT NULL,
    p_job_id NUMERIC DEFAULT NULL,
    p_domain_name TEXT DEFAULT NULL,
    p_data_month TEXT DEFAULT NULL,
    p_num_row NUMERIC DEFAULT NULL,
    p_file_name TEXT DEFAULT NULL
)
LANGUAGE plpgsql
AS $$
DECLARE
    v_job_id NUMERIC(38, 0);
    v_domain_name TEXT;
BEGIN
    v_domain_name := p_domain_name;
    IF v_domain_name IS NULL THEN
        SELECT domain_name
        INTO v_domain_name
        FROM archival_config
        WHERE UPPER(TRIM(table_name)) = UPPER(TRIM(p_table_name))
        LIMIT 1;
    END IF;

    v_domain_name := COALESCE(v_domain_name, 'Metadata');
    v_job_id := p_job_id;

    IF v_job_id IS NULL THEN
        SELECT COALESCE(MAX(job_id), 1)
        INTO v_job_id
        FROM archival_last_run;
    END IF;

    IF UPPER(p_action) = 'STARTED' THEN
        INSERT INTO archival_last_run (
            job_id,
            archival_last_run_id,
            domain_name,
            step_name,
            table_name,
            file_name,
            start_time,
            status,
            num_rows,
            data_month
        )
        VALUES (
            v_job_id,
            nextval('seq_archival_last_run'),
            v_domain_name,
            p_step_name,
            p_table_name,
            p_file_name,
            COALESCE(p_start_time, now()),
            'STARTED',
            p_num_row,
            p_data_month
        );
    ELSIF UPPER(p_action) = 'COMPLETED' THEN
        UPDATE archival_last_run
        SET end_time = now(),
            status = 'COMPLETED',
            error_message = NULL,
            num_rows = COALESCE(p_num_row, num_rows),
            data_month = COALESCE(p_data_month, data_month),
            file_name = COALESCE(p_file_name, file_name)
        WHERE archival_last_run_id = (
            SELECT MAX(archival_last_run_id)
            FROM archival_last_run
            WHERE table_name = p_table_name
              AND step_name = p_step_name
              AND job_id = v_job_id
              AND status = 'STARTED'
              AND end_time IS NULL
        );
    ELSIF UPPER(p_action) = 'ERROR' THEN
        UPDATE archival_last_run
        SET end_time = now(),
            status = 'ERROR',
            error_message = LEFT(COALESCE(p_error_msg, 'Unknown error'), 2000)
        WHERE archival_last_run_id = (
            SELECT MAX(archival_last_run_id)
            FROM archival_last_run
            WHERE table_name = p_table_name
              AND step_name = p_step_name
              AND job_id = v_job_id
              AND status = 'STARTED'
              AND end_time IS NULL
        );
    END IF;
END;
$$;
$archival_stmt$;
END;
$do$;

DO $do$
BEGIN
    EXECUTE $archival_stmt$
CREATE OR REPLACE PROCEDURE sp_generate_archival_file_list(
    p_job_id NUMERIC,
    p_default_retention_months INT DEFAULT 12,
    p_file_dir_nm TEXT DEFAULT NULL,
    p_file_extension TEXT DEFAULT 'csv',
    p_date_dim_table TEXT DEFAULT 'd_date',
    p_date_dim_pk TEXT DEFAULT 'date_id',
    p_date_dim_value TEXT DEFAULT 'full_date'
)
LANGUAGE plpgsql
AS $$
DECLARE
    rec RECORD;
    v_table_exists BOOLEAN;
    v_dim_table_exists BOOLEAN;
    v_dim_pk_exists BOOLEAN;
    v_dim_value_exists BOOLEAN;
    v_date_col_type TEXT;
    v_sql TEXT;
    v_min_month DATE;
    v_max_month DATE;
    v_month_start DATE;
    v_month_end DATE;
    v_cutoff_month DATE;
    v_file_no NUMERIC;
    v_data_month TEXT;
    v_date_ind NUMERIC(1, 0);
    v_where_clause TEXT;
    v_condition TEXT;
    v_is_time_col BOOLEAN;
    v_is_date_key_col BOOLEAN;
    v_ext TEXT;
    v_cfg_table_count INTEGER := 0;
    v_debug_ctx TEXT;
    v_table_ident TEXT;
    v_date_col_ident TEXT;
    v_dim_table_ident TEXT;
    v_dim_pk_ident TEXT;
    v_dim_value_ident TEXT;
    v_row_count BIGINT;
BEGIN
    v_ext := replace(COALESCE(NULLIF(trim(p_file_extension), ''), 'csv'), '.', '');
    v_dim_table_ident := pg_sql_ident(p_date_dim_table);
    v_dim_pk_ident := pg_sql_ident(p_date_dim_pk);
    v_dim_value_ident := pg_sql_ident(p_date_dim_value);

    RAISE NOTICE '[DEBUG sp_generate_archival_file_list] START job_id=%, schema=%, user=%, search_path=%, file_dir=%, ext=%',
        p_job_id, current_schema(), current_user, current_setting('search_path'),
        COALESCE(p_file_dir_nm, '<NULL>'), v_ext;

    CALL set_archival_last_run('SP_GENERATE_ARCHIVAL_FILE_LIST', 'SP', 'STARTED', NULL, NULL, p_job_id);

    DELETE FROM archival_file_list WHERE job_id = p_job_id;

    /*
      Guardrail (Oracle parity behavior): if no tables are eligible for this job,
      do NOT silently "complete"—raise an error with enough context to diagnose
      schema/search_path/user mismatches.
    */
    SELECT COUNT(*)
    INTO v_cfg_table_count
    FROM archival_config ac
    JOIN archival_summary s
      ON s.job_id = p_job_id
     AND regexp_replace(UPPER(TRIM(s.domain_name)), E'\\s+', ' ', 'g')
         = regexp_replace(UPPER(TRIM(ac.domain_name)), E'\\s+', ' ', 'g')
    WHERE UPPER(TRIM(ac.table_type)) = 'F';

    RAISE NOTICE '[DEBUG sp_generate_archival_file_list] eligible fact tables (archival_config JOIN archival_summary)=%',
        COALESCE(v_cfg_table_count, 0);

    IF COALESCE(v_cfg_table_count, 0) = 0 THEN
        v_debug_ctx := 'No eligible tables for job_id=' || p_job_id
            || '; current_schema=' || current_schema()
            || '; current_user=' || current_user
            || '; search_path=' || current_setting('search_path');

        RAISE NOTICE '[DEBUG sp_generate_archival_file_list] ERROR: %', v_debug_ctx;
        RAISE NOTICE '[DEBUG sp_generate_archival_file_list] archival_summary domains for job: %',
            (SELECT string_agg(domain_name || '(' || COALESCE(data_retention_months::TEXT, 'NULL') || ')', ', ')
             FROM archival_summary WHERE job_id = p_job_id);
        RAISE NOTICE '[DEBUG sp_generate_archival_file_list] archival_config fact domains: %',
            (SELECT string_agg(DISTINCT domain_name, ', ')
             FROM archival_config WHERE UPPER(TRIM(table_type)) = 'F');

        CALL set_archival_last_run('SP_GENERATE_ARCHIVAL_FILE_LIST', 'SP', 'ERROR', NULL, v_debug_ctx, p_job_id);
        RAISE EXCEPTION '%', v_debug_ctx;
    END IF;

    FOR rec IN
        /*
          Oracle/SQLServer drive archival scope from the current job's per-domain configuration.
          Do the same in PostgreSQL: select fact tables whose DOMAIN_NAME is present in ARCHIVAL_SUMMARY for this JOB.
          DISTINCT ON: fresh install loads archival_config twice (core_data + archival_config_core_data).
        */
        SELECT DISTINCT ON (UPPER(TRIM(ac.table_name)))
               ac.table_name,
               ac.date_column_name,
               COALESCE(NULLIF(s.data_retention_months, 0), NULLIF(ac.data_retention_months, 0), p_default_retention_months) AS retention_months,
               COALESCE(ac.archival_condition_1, '') || COALESCE(ac.archival_condition_2, '') AS combined_condition
        FROM archival_config ac
        JOIN archival_summary s
          ON s.job_id = p_job_id
         AND regexp_replace(UPPER(TRIM(s.domain_name)), E'\\s+', ' ', 'g')
             = regexp_replace(UPPER(TRIM(ac.domain_name)), E'\\s+', ' ', 'g')
        WHERE UPPER(TRIM(ac.table_type)) = 'F'
        ORDER BY UPPER(TRIM(ac.table_name)), ac.table_name
    LOOP
        BEGIN
            v_table_ident := pg_sql_ident(rec.table_name);
            v_date_col_ident := pg_sql_ident(rec.date_column_name);

            RAISE NOTICE '[DEBUG sp_generate_archival_file_list] Processing table=% (pg_ident=%) date_col=% (pg_ident=%) retention_months=% condition=[%]',
                rec.table_name, v_table_ident, rec.date_column_name, v_date_col_ident, rec.retention_months, rec.combined_condition;

            CALL set_archival_last_run('SP_GENERATE_ARCHIVAL_FILE_LIST', rec.table_name, 'STARTED', NULL, NULL, p_job_id);

            SELECT EXISTS (
                SELECT 1
                FROM information_schema.tables t
                WHERE t.table_schema = current_schema()
                  AND UPPER(t.table_name) = UPPER(rec.table_name)
            )
            INTO v_table_exists;

            IF NOT v_table_exists THEN
                RAISE NOTICE '[DEBUG sp_generate_archival_file_list] SKIP table=% - not found in schema=%',
                    rec.table_name, current_schema();
                CALL set_archival_last_run(
                    'SP_GENERATE_ARCHIVAL_FILE_LIST',
                    rec.table_name,
                    'ERROR',
                    NULL,
                    format('Table %s does not exist in schema %s', rec.table_name, current_schema()),
                    p_job_id
                );
                CONTINUE;
            END IF;

            v_cutoff_month := (date_trunc('month', current_date) - make_interval(months => rec.retention_months::INT))::date;
            v_condition := rec.combined_condition;

            RAISE NOTICE '[DEBUG sp_generate_archival_file_list] table=% cutoff_month=% (retention=% months)',
                rec.table_name, v_cutoff_month, rec.retention_months;

            SELECT data_type
            INTO v_date_col_type
            FROM information_schema.columns c
            WHERE c.table_schema = current_schema()
              AND UPPER(c.table_name) = UPPER(rec.table_name)
              AND UPPER(c.column_name) = UPPER(rec.date_column_name)
            LIMIT 1;

            v_is_time_col := v_date_col_type IN ('date', 'timestamp without time zone', 'timestamp with time zone');
            v_is_date_key_col := v_date_col_type IN ('smallint', 'integer', 'bigint', 'numeric', 'decimal');

            v_min_month := NULL;
            v_max_month := NULL;
            v_date_ind := 2;

            IF rec.date_column_name IS NULL THEN
                v_is_time_col := FALSE;
                v_is_date_key_col := FALSE;
            END IF;

            RAISE NOTICE '[DEBUG sp_generate_archival_file_list] table=% date_col_type=% is_time_col=% is_date_key_col=% date_ind=%',
                rec.table_name, COALESCE(v_date_col_type, '<NULL>'), v_is_time_col, v_is_date_key_col, v_date_ind;

            IF v_is_time_col THEN
                v_sql := format(
                    'SELECT date_trunc(''month'', min(%1$I)::timestamp)::date, date_trunc(''month'', max(%1$I)::timestamp)::date
                     FROM %2$I
                     WHERE %1$I < %3$L::timestamp %4$s',
                    v_date_col_ident,
                    v_table_ident,
                    v_cutoff_month,
                    v_condition
                );
                RAISE NOTICE '[DEBUG sp_generate_archival_file_list] table=% min/max SQL: %', rec.table_name, v_sql;
                EXECUTE v_sql INTO v_min_month, v_max_month;
                v_date_ind := 1;
            ELSIF v_is_date_key_col THEN
                SELECT EXISTS (
                           SELECT 1 FROM information_schema.tables t
                           WHERE t.table_schema = current_schema()
                             AND UPPER(t.table_name) = UPPER(p_date_dim_table)
                       ),
                       EXISTS (
                           SELECT 1 FROM information_schema.columns c
                           WHERE c.table_schema = current_schema()
                             AND UPPER(c.table_name) = UPPER(p_date_dim_table)
                             AND UPPER(c.column_name) = UPPER(p_date_dim_pk)
                       ),
                       EXISTS (
                           SELECT 1 FROM information_schema.columns c
                           WHERE c.table_schema = current_schema()
                             AND UPPER(c.table_name) = UPPER(p_date_dim_table)
                             AND UPPER(c.column_name) = UPPER(p_date_dim_value)
                       )
                INTO v_dim_table_exists, v_dim_pk_exists, v_dim_value_exists;

                RAISE NOTICE '[DEBUG sp_generate_archival_file_list] table=% dim_table=% exists=% pk=% value_col=%',
                    rec.table_name, p_date_dim_table, v_dim_table_exists, v_dim_pk_exists, v_dim_value_exists;

                IF v_dim_table_exists AND v_dim_pk_exists AND v_dim_value_exists THEN
                    v_sql := format(
                        'SELECT date_trunc(''month'', min(d.%1$I)::timestamp)::date, date_trunc(''month'', max(d.%1$I)::timestamp)::date
                         FROM %2$I t
                         JOIN %3$I d ON t.%4$I = d.%5$I
                         WHERE d.%1$I < %6$L::date %7$s',
                        v_dim_value_ident,
                        v_table_ident,
                        v_dim_table_ident,
                        v_date_col_ident,
                        v_dim_pk_ident,
                        v_cutoff_month,
                        v_condition
                    );
                    RAISE NOTICE '[DEBUG sp_generate_archival_file_list] table=% min/max SQL (date key): %', rec.table_name, v_sql;
                    EXECUTE v_sql INTO v_min_month, v_max_month;
                    v_date_ind := 0;
                END IF;
            END IF;

            RAISE NOTICE '[DEBUG sp_generate_archival_file_list] table=% min_month=% max_month=% date_ind=%',
                rec.table_name, v_min_month, v_max_month, v_date_ind;

            IF v_min_month IS NULL OR v_max_month IS NULL THEN
                RAISE NOTICE '[DEBUG sp_generate_archival_file_list] table=% SKIP - no rows before cutoff_month=% (retention=% months)',
                    rec.table_name, v_cutoff_month, rec.retention_months;
            ELSE
                RAISE NOTICE '[DEBUG sp_generate_archival_file_list] table=% generating monthly files from % to % (cutoff=%)',
                    rec.table_name, v_min_month, v_max_month, v_cutoff_month;
                v_month_start := v_min_month;
                WHILE v_month_start <= v_max_month LOOP
                    v_month_end := (v_month_start + INTERVAL '1 month')::date;
                    v_data_month := to_char(v_month_start, 'YYYYMM');

                    IF v_date_ind = 1 THEN
                        v_where_clause := format(
                            'WHERE %1$I >= %2$L::timestamp AND %1$I < %3$L::timestamp %4$s',
                            v_date_col_ident,
                            v_month_start,
                            v_month_end,
                            v_condition
                        );
                    ELSIF v_date_ind = 0 THEN
                        v_where_clause := format(
                            'WHERE %1$I IN (SELECT %2$I FROM %3$I WHERE %4$I >= %5$L::date AND %4$I < %6$L::date) %7$s',
                            v_date_col_ident,
                            v_dim_pk_ident,
                            v_dim_table_ident,
                            v_dim_value_ident,
                            v_month_start,
                            v_month_end,
                            v_condition
                        );
                    ELSE
                        v_where_clause := format(
                            'WHERE %1$I < %2$L::timestamp %3$s',
                            v_date_col_ident,
                            v_cutoff_month,
                            v_condition
                        );
                    END IF;

                    v_sql := format('SELECT count(1) FROM %I %s', v_table_ident, v_where_clause);
                    EXECUTE v_sql INTO v_row_count;

                    IF COALESCE(v_row_count, 0) = 0 THEN
                        RAISE NOTICE '[DEBUG sp_generate_archival_file_list] table=% SKIP month=% - zero rows for where=[%]',
                            rec.table_name, v_data_month, v_where_clause;
                        v_month_start := v_month_end;
                        CONTINUE;
                    END IF;

                    -- Number files per archive job (not globally across all prior jobs).
                    -- Using MAX from archival_file_details caused _202602_2.csv metadata when only _202602_1.csv exists on disk.
                    SELECT COALESCE(MAX(file_no), 0) + 1
                    INTO v_file_no
                    FROM archival_file_list
                    WHERE job_id = p_job_id
                      AND UPPER(table_name) = UPPER(rec.table_name)
                      AND data_month = v_data_month;

                    RAISE NOTICE '[DEBUG sp_generate_archival_file_list] table=% INSERT file data_month=% file_no=% rows=% where=[%]',
                        rec.table_name, v_data_month, v_file_no, v_row_count, v_where_clause;

                    INSERT INTO archival_file_list (
                        job_id, table_name, data_month, file_no, file_name, start_date, end_date,
                        exp_ind, dw_delete_ind, date_ind, where_clause, file_dir_nm, num_rows
                    )
                    VALUES (
                        p_job_id,
                        rec.table_name,
                        v_data_month,
                        v_file_no,
                        lower(rec.table_name) || '_' || v_data_month || '_' || v_file_no || '.' || v_ext,
                        v_month_start::timestamp,
                        (v_month_end::timestamp - INTERVAL '1 second'),
                        0,
                        0,
                        v_date_ind,
                        v_where_clause,
                        LEFT(COALESCE(p_file_dir_nm, ''), 50),
                        v_row_count
                    );

                    v_month_start := v_month_end;
                END LOOP;
            END IF;

            RAISE NOTICE '[DEBUG sp_generate_archival_file_list] table=% COMPLETED; file_list rows for job=%',
                rec.table_name,
                (SELECT COUNT(*) FROM archival_file_list WHERE job_id = p_job_id AND UPPER(table_name) = UPPER(rec.table_name));

            CALL set_archival_last_run('SP_GENERATE_ARCHIVAL_FILE_LIST', rec.table_name, 'COMPLETED', NULL, NULL, p_job_id);
        EXCEPTION
            WHEN OTHERS THEN
                RAISE NOTICE '[DEBUG sp_generate_archival_file_list] table=% ERROR: %', rec.table_name, SQLERRM;
                CALL set_archival_last_run(
                    'SP_GENERATE_ARCHIVAL_FILE_LIST',
                    rec.table_name,
                    'ERROR',
                    NULL,
                    SQLERRM,
                    p_job_id
                );
        END;
    END LOOP;

    RAISE NOTICE '[DEBUG sp_generate_archival_file_list] END job_id=% total archival_file_list rows=%',
        p_job_id,
        (SELECT COUNT(*) FROM archival_file_list WHERE job_id = p_job_id);

    CALL set_archival_last_run('SP_GENERATE_ARCHIVAL_FILE_LIST', 'SP', 'COMPLETED', NULL, NULL, p_job_id);
END;
$$;
$archival_stmt$;
END;
$do$;

DO $do$
BEGIN
    EXECUTE $archival_stmt$
CREATE OR REPLACE PROCEDURE sp_export_data(
    p_job_id NUMERIC,
    p_export_directory TEXT,
    p_delete_exported_rows BOOLEAN DEFAULT FALSE
)
LANGUAGE plpgsql
AS $$
DECLARE
    rec RECORD;
    v_file_path TEXT;
    v_export_sql TEXT;
    v_count_sql TEXT;
    v_delete_sql TEXT;
    v_num_rows NUMERIC(38, 0);
    v_pending_count INTEGER := 0;
    v_table_ident TEXT;
BEGIN
    SELECT COUNT(*)
    INTO v_pending_count
    FROM archival_file_list
    WHERE job_id = p_job_id
      AND COALESCE(exp_ind, 0) = 0;

    RAISE NOTICE '[DEBUG sp_export_data] START job_id=% export_directory=% pending_files=%',
        p_job_id, p_export_directory, v_pending_count;

    CALL set_archival_last_run('SP_EXPORT_DATA', 'SP', 'STARTED', NULL, NULL, p_job_id);

    FOR rec IN
        SELECT job_id, table_name, data_month, file_no, file_name, where_clause, file_dir_nm
        FROM archival_file_list
        WHERE job_id = p_job_id
          AND COALESCE(exp_ind, 0) = 0
        ORDER BY table_name, data_month, file_no
    LOOP
        BEGIN
            v_table_ident := pg_sql_ident(rec.table_name);

            v_file_path := pg_normalize_file_path(
                CONCAT(
                    COALESCE(NULLIF(rec.file_dir_nm, ''), p_export_directory),
                    '/',
                    rec.file_name
                )
            );

            RAISE NOTICE '[DEBUG sp_export_data] EXPORT table=% (pg_ident=%) file=% path=% where=[%]',
                rec.table_name, v_table_ident, rec.file_name, v_file_path, COALESCE(rec.where_clause, '');

            v_count_sql := format('SELECT count(1) FROM %I %s', v_table_ident, COALESCE(rec.where_clause, ''));
            EXECUTE v_count_sql INTO v_num_rows;

            RAISE NOTICE '[DEBUG sp_export_data] table=% file=% row_count=% count_sql=%',
                rec.table_name, rec.file_name, v_num_rows, v_count_sql;

            CALL set_archival_last_run('EXPORT', rec.table_name, 'STARTED', NULL, NULL, p_job_id, NULL, rec.data_month, v_num_rows, rec.file_name);

            IF COALESCE(v_num_rows, 0) = 0 THEN
                RAISE NOTICE '[DEBUG sp_export_data] SKIP table=% file=% - zero rows, no file created',
                    rec.table_name, rec.file_name;

                UPDATE archival_file_list
                SET exp_ind = 1,
                    num_rows = 0,
                    error_message = NULL
                WHERE job_id = rec.job_id
                  AND table_name = rec.table_name
                  AND file_name = rec.file_name;

                CALL set_archival_last_run('EXPORT', rec.table_name, 'COMPLETED', NULL, NULL, p_job_id, NULL, rec.data_month, v_num_rows, rec.file_name);
                CONTINUE;
            END IF;

            v_export_sql := format(
                'COPY (SELECT * FROM %I %s) TO %L WITH (FORMAT CSV, HEADER TRUE)',
                v_table_ident,
                COALESCE(rec.where_clause, ''),
                v_file_path
            );
            RAISE NOTICE '[DEBUG sp_export_data] COPY SQL: %', v_export_sql;
            EXECUTE v_export_sql;

            RAISE NOTICE '[DEBUG sp_export_data] SUCCESS table=% file=% rows_exported=%',
                rec.table_name, rec.file_name, v_num_rows;

            IF p_delete_exported_rows THEN
                v_delete_sql := format('DELETE FROM %I %s', v_table_ident, COALESCE(rec.where_clause, ''));
                EXECUTE v_delete_sql;
            END IF;

            INSERT INTO archival_file_details (
                job_id, table_name, data_month, file_no, file_name, archival_date, retrieve_ind, is_reloaded,
                num_rows_archived, file_dir_nm
            )
            VALUES (
                p_job_id, rec.table_name, rec.data_month, rec.file_no, rec.file_name, now(), 0, 0, v_num_rows,
                LEFT(COALESCE(NULLIF(rec.file_dir_nm, ''), p_export_directory), 50)
            );

            UPDATE archival_file_list
            SET exp_ind = 1,
                dw_delete_ind = CASE WHEN p_delete_exported_rows THEN 1 ELSE dw_delete_ind END,
                num_rows = v_num_rows,
                error_message = NULL
            WHERE job_id = rec.job_id
              AND table_name = rec.table_name
              AND file_name = rec.file_name;

            CALL set_archival_last_run('EXPORT', rec.table_name, 'COMPLETED', NULL, NULL, p_job_id, NULL, rec.data_month, v_num_rows, rec.file_name);
        EXCEPTION
            WHEN OTHERS THEN
                RAISE NOTICE '[DEBUG sp_export_data] ERROR table=% file=%: %', rec.table_name, rec.file_name, SQLERRM;
                UPDATE archival_file_list
                SET error_message = LEFT(SQLERRM, 2000)
                WHERE job_id = rec.job_id
                  AND table_name = rec.table_name
                  AND file_name = rec.file_name;

                CALL set_archival_last_run('EXPORT', rec.table_name, 'ERROR', NULL, SQLERRM, p_job_id, NULL, rec.data_month, v_num_rows, rec.file_name);
        END;
    END LOOP;

    RAISE NOTICE '[DEBUG sp_export_data] END job_id=% exported_files=% remaining_pending=%',
        p_job_id,
        (SELECT COUNT(*) FROM archival_file_list WHERE job_id = p_job_id AND COALESCE(exp_ind, 0) = 1),
        (SELECT COUNT(*) FROM archival_file_list WHERE job_id = p_job_id AND COALESCE(exp_ind, 0) = 0);

    CALL set_archival_last_run('SP_EXPORT_DATA', 'SP', 'COMPLETED', NULL, NULL, p_job_id);
END;
$$;
$archival_stmt$;
END;
$do$;

DO $do$
BEGIN
    EXECUTE $archival_stmt$
CREATE OR REPLACE PROCEDURE sp_prepare_retrieve_file_list(
    p_job_id NUMERIC
)
LANGUAGE plpgsql
AS $$
BEGIN
    CALL set_archival_last_run('SP_PREPARE_RETRIEVE_FILE_LIST', 'SP', 'STARTED', NULL, NULL, p_job_id);

    UPDATE archival_retrieve_summary
    SET start_month = CASE WHEN start_month > end_month THEN end_month ELSE start_month END,
        end_month = CASE WHEN start_month > end_month THEN start_month ELSE end_month END
    WHERE job_id = p_job_id;

    -- Clear stale retrieve_ind so prior restore attempts do not pull in out-of-range months.
    UPDATE archival_file_details afd
    SET retrieve_ind = 0
    WHERE COALESCE(afd.is_reloaded, 0) = 0
      AND EXISTS (
          SELECT 1
          FROM archival_retrieve_summary rs
          JOIN (
              SELECT DISTINCT ON (UPPER(TRIM(table_name)))
                     table_name, domain_name
              FROM archival_config
              ORDER BY UPPER(TRIM(table_name)), table_name
          ) ac ON UPPER(ac.domain_name) = UPPER(rs.domain_name)
          WHERE rs.job_id = p_job_id
            AND UPPER(ac.table_name) = UPPER(afd.table_name)
      );

    UPDATE archival_file_details afd
    SET retrieve_ind = 1
    WHERE COALESCE(afd.is_reloaded, 0) = 0
      AND EXISTS (
          SELECT 1
          FROM archival_retrieve_summary rs
          JOIN (
              SELECT DISTINCT ON (UPPER(TRIM(table_name)))
                     table_name, domain_name
              FROM archival_config
              ORDER BY UPPER(TRIM(table_name)), table_name
          ) ac ON UPPER(ac.domain_name) = UPPER(rs.domain_name)
          WHERE rs.job_id = p_job_id
            AND UPPER(ac.table_name) = UPPER(afd.table_name)
            AND afd.data_month BETWEEN rs.start_month AND rs.end_month
      );

    DELETE FROM archival_retrieve_file_list WHERE job_id = p_job_id;

    INSERT INTO archival_retrieve_file_list (
        job_id, file_load_id, table_name, file_name, load_ind, file_remove_ind, file_dir_nm
    )
    SELECT p_job_id,
           row_number() OVER (ORDER BY ac.domain_name, afd.table_name, afd.data_month, afd.file_no),
           afd.table_name,
           afd.file_name,
           0,
           0,
           afd.file_dir_nm
    FROM (
        SELECT DISTINCT ON (UPPER(TRIM(table_name)), file_name)
               table_name,
               file_name,
               data_month,
               file_no,
               file_dir_nm,
               job_id
        FROM archival_file_details
        WHERE retrieve_ind = 1
          AND COALESCE(is_reloaded, 0) = 0
          AND COALESCE(num_rows_archived, 0) > 0
          AND EXISTS (
              SELECT 1
              FROM archival_file_list afl
              WHERE afl.job_id = archival_file_details.job_id
                AND afl.file_name = archival_file_details.file_name
                AND UPPER(afl.table_name) = UPPER(archival_file_details.table_name)
                AND COALESCE(afl.exp_ind, 0) = 1
                AND COALESCE(afl.num_rows, 0) > 0
          )
        ORDER BY UPPER(TRIM(table_name)), file_name, job_id DESC
    ) afd
    JOIN (
        SELECT DISTINCT ON (UPPER(TRIM(table_name)))
               table_name, domain_name
        FROM archival_config
        ORDER BY UPPER(TRIM(table_name)), table_name
    ) ac ON UPPER(ac.table_name) = UPPER(afd.table_name)
    JOIN archival_retrieve_summary rs
      ON UPPER(rs.domain_name) = UPPER(ac.domain_name)
    WHERE rs.job_id = p_job_id
      AND afd.data_month BETWEEN rs.start_month AND rs.end_month;

    CALL set_archival_last_run('SP_PREPARE_RETRIEVE_FILE_LIST', 'SP', 'COMPLETED', NULL, NULL, p_job_id);
END;
$$;
$archival_stmt$;
END;
$do$;

DO $do$
BEGIN
    EXECUTE $archival_stmt$
CREATE OR REPLACE PROCEDURE sp_restore_data(
    p_job_id NUMERIC,
    p_import_directory TEXT
)
LANGUAGE plpgsql
AS $$
DECLARE
    rec RECORD;
    v_file_path TEXT;
    v_copy_sql TEXT;
    v_table_ident TEXT;
BEGIN
    CALL set_archival_last_run('SP_RESTORE_DATA', 'SP', 'STARTED', NULL, NULL, p_job_id);

    FOR rec IN
        SELECT DISTINCT ON (UPPER(TRIM(r.table_name)), r.file_name)
               r.job_id,
               r.file_load_id,
               r.table_name,
               r.file_name,
               COALESCE(NULLIF(r.file_dir_nm, ''), p_import_directory) AS base_dir,
               d.data_month,
               d.file_no,
               d.num_rows_archived
        FROM archival_retrieve_file_list r
        LEFT JOIN LATERAL (
            SELECT fd.data_month, fd.file_no, fd.num_rows_archived
            FROM archival_file_details fd
            WHERE UPPER(TRIM(fd.table_name)) = UPPER(TRIM(r.table_name))
              AND fd.file_name = r.file_name
            ORDER BY fd.job_id DESC
            LIMIT 1
        ) d ON TRUE
        WHERE r.job_id = p_job_id
          AND COALESCE(r.load_ind, 0) = 0
        ORDER BY UPPER(TRIM(r.table_name)), r.file_name, r.file_load_id
    LOOP
        BEGIN
            CALL set_archival_last_run('IMPORT', rec.table_name, 'STARTED', NULL, NULL, p_job_id, NULL, rec.data_month, rec.num_rows_archived, rec.file_name);

            v_file_path := pg_normalize_file_path(CONCAT(rec.base_dir, '/', rec.file_name));
            v_table_ident := pg_sql_ident(rec.table_name);
            v_copy_sql := format('COPY %I FROM %L WITH (FORMAT CSV, HEADER TRUE)', v_table_ident, v_file_path);
            EXECUTE v_copy_sql;

            UPDATE archival_retrieve_file_list
            SET load_ind = 1,
                error_message = NULL
            WHERE job_id = rec.job_id
              AND UPPER(TRIM(table_name)) = UPPER(TRIM(rec.table_name))
              AND file_name = rec.file_name;

            UPDATE archival_file_details
            SET is_reloaded = 1,
                reloade_date = now()
            WHERE UPPER(table_name) = UPPER(rec.table_name)
              AND file_name = rec.file_name
              AND COALESCE(is_reloaded, 0) = 0;

            CALL set_archival_last_run('IMPORT', rec.table_name, 'COMPLETED', NULL, NULL, p_job_id);
        EXCEPTION
            WHEN OTHERS THEN
                UPDATE archival_retrieve_file_list
                SET error_message = LEFT(SQLERRM, 2000)
                WHERE job_id = rec.job_id
                  AND UPPER(TRIM(table_name)) = UPPER(TRIM(rec.table_name))
                  AND file_name = rec.file_name;

                CALL set_archival_last_run('IMPORT', rec.table_name, 'ERROR', NULL, SQLERRM, p_job_id);
        END;
    END LOOP;

    CALL set_archival_last_run('SP_RESTORE_DATA', 'SP', 'COMPLETED', NULL, NULL, p_job_id);
END;
$$;
$archival_stmt$;
END;
$do$;

DO $do$
BEGIN
    EXECUTE $archival_stmt$
DROP PROCEDURE IF EXISTS sp_archival(TEXT, INT, BOOLEAN, TEXT);
$archival_stmt$;
END;
$do$;

DO $do$
BEGIN
    EXECUTE $archival_stmt$
DROP PROCEDURE IF EXISTS sp_archival(TEXT, INT, BOOLEAN, TEXT, BOOLEAN);
$archival_stmt$;
END;
$do$;

DO $do$
BEGIN
    EXECUTE $archival_stmt$
CREATE OR REPLACE PROCEDURE sp_archival(
    p_export_directory TEXT,
    p_default_retention_months INT DEFAULT 12,
    p_delete_exported_rows BOOLEAN DEFAULT FALSE,
    p_file_extension TEXT DEFAULT 'csv',
    p_purge_after_export BOOLEAN DEFAULT TRUE
)
LANGUAGE plpgsql
AS $$
DECLARE
    v_job_id NUMERIC(38, 0);
    v_file_list_count INTEGER;
BEGIN
    v_job_id := sp_new_job('ARCHIVAL');

    RAISE NOTICE '[DEBUG sp_archival] START job_id=% export_directory=% retention_months=% delete_rows=% purge_after_export=% ext=%',
        v_job_id, p_export_directory, p_default_retention_months, p_delete_exported_rows, p_purge_after_export, p_file_extension;

    UPDATE archival_summary
    SET status = 'STARTED',
        start_time = now()
    WHERE job_id = v_job_id;

    CALL set_archival_last_run('ARCHIVAL_PROCESS', 'ARCHIVAL_PROCESS', 'STARTED', NULL, NULL, v_job_id, 'Metadata');

    CALL sp_generate_archival_file_list(v_job_id, p_default_retention_months, p_export_directory, p_file_extension);

    SELECT COUNT(*) INTO v_file_list_count FROM archival_file_list WHERE job_id = v_job_id;
    RAISE NOTICE '[DEBUG sp_archival] after file_list generation: job_id=% archival_file_list rows=%',
        v_job_id, v_file_list_count;

    IF v_file_list_count = 0 THEN
        RAISE NOTICE '[DEBUG sp_archival] WARNING: archival_file_list is EMPTY for job_id=% - no export will run', v_job_id;
    END IF;

    CALL sp_export_data(v_job_id, p_export_directory, p_delete_exported_rows);

    IF p_purge_after_export AND NOT p_delete_exported_rows THEN
        CALL sp_purge_archived_data(v_job_id);
    ELSIF p_purge_after_export AND p_delete_exported_rows THEN
        RAISE NOTICE '[DEBUG sp_archival] Purge skipped - rows already deleted during export (delete_exported_rows=true)';
    ELSE
        RAISE NOTICE '[DEBUG sp_archival] Purge skipped - p_purge_after_export=false';
    END IF;

    UPDATE archival_summary s
    SET archival_date = now(),
        end_time = now(),
        status = CASE
                     WHEN EXISTS (
                         SELECT 1
                         FROM archival_file_list fl
                         JOIN archival_config ac ON UPPER(ac.table_name) = UPPER(fl.table_name)
                         WHERE fl.job_id = s.job_id
                           AND UPPER(ac.domain_name) = UPPER(s.domain_name)
                           AND fl.error_message IS NOT NULL
                     ) THEN 'ERROR'
                     ELSE 'COMPLETED'
                 END,
        error_message = (
            SELECT MAX(fl.error_message)
            FROM archival_file_list fl
            JOIN archival_config ac ON UPPER(ac.table_name) = UPPER(fl.table_name)
            WHERE fl.job_id = s.job_id
              AND UPPER(ac.domain_name) = UPPER(s.domain_name)
              AND fl.error_message IS NOT NULL
        )
    WHERE s.job_id = v_job_id;

    RAISE NOTICE '[DEBUG sp_archival] COMPLETED job_id=% summary_status=% file_details_count=%',
        v_job_id,
        (SELECT string_agg(domain_name || ':' || status, ', ') FROM archival_summary WHERE job_id = v_job_id),
        (SELECT COUNT(*) FROM archival_file_details WHERE job_id = v_job_id);

    CALL set_archival_last_run('ARCHIVAL_PROCESS', 'ARCHIVAL_PROCESS', 'COMPLETED', NULL, NULL, v_job_id, 'Metadata');
EXCEPTION
    WHEN OTHERS THEN
        RAISE NOTICE '[DEBUG sp_archival] FATAL ERROR job_id=%: %', v_job_id, SQLERRM;
        IF v_job_id IS NOT NULL THEN
            UPDATE archival_summary
            SET end_time = now(),
                archival_date = now(),
                status = 'ERROR',
                error_message = LEFT(SQLERRM, 2000)
            WHERE job_id = v_job_id
              AND status <> 'COMPLETED';

            CALL set_archival_last_run('ARCHIVAL_PROCESS', 'ARCHIVAL_PROCESS', 'ERROR', NULL, SQLERRM, v_job_id, 'Metadata');
        END IF;
        RAISE;
END;
$$;
$archival_stmt$;
END;
$do$;

DO $do$
BEGIN
    EXECUTE $archival_stmt$
CREATE OR REPLACE PROCEDURE sp_restore(
    p_import_directory TEXT,
    p_start_month TEXT,
    p_end_month TEXT,
    p_domain_name TEXT DEFAULT NULL
)
LANGUAGE plpgsql
AS $$
DECLARE
    v_job_id NUMERIC(38, 0);
BEGIN
    v_job_id := sp_new_job('RESTORE', p_start_month, p_end_month);

    IF p_domain_name IS NOT NULL THEN
        DELETE FROM archival_retrieve_summary
        WHERE job_id = v_job_id
          AND UPPER(domain_name) <> UPPER(p_domain_name);
    END IF;

    UPDATE archival_retrieve_summary
    SET status = 'STARTED',
        start_time = now()
    WHERE job_id = v_job_id;

    CALL set_archival_last_run('RETRIEVE_PROCESS', 'RETRIEVE_PROCESS', 'STARTED', NULL, NULL, v_job_id, 'Metadata');

    CALL sp_prepare_retrieve_file_list(v_job_id);
    CALL sp_restore_data(v_job_id, p_import_directory);

    UPDATE archival_retrieve_summary rs
    SET retrieve_date = now(),
        end_time = now(),
        status = CASE
                     WHEN EXISTS (
                         SELECT 1
                         FROM archival_retrieve_file_list r
                         JOIN archival_config ac ON UPPER(ac.table_name) = UPPER(r.table_name)
                         WHERE r.job_id = rs.job_id
                           AND UPPER(ac.domain_name) = UPPER(rs.domain_name)
                           AND r.error_message IS NOT NULL
                     ) THEN 'ERROR'
                     ELSE 'COMPLETED'
                 END,
        error_message = (
            SELECT MAX(r.error_message)
            FROM archival_retrieve_file_list r
            JOIN archival_config ac ON UPPER(ac.table_name) = UPPER(r.table_name)
            WHERE r.job_id = rs.job_id
              AND UPPER(ac.domain_name) = UPPER(rs.domain_name)
              AND r.error_message IS NOT NULL
        )
    WHERE rs.job_id = v_job_id;

    CALL set_archival_last_run('RETRIEVE_PROCESS', 'RETRIEVE_PROCESS', 'COMPLETED', NULL, NULL, v_job_id, 'Metadata');
EXCEPTION
    WHEN OTHERS THEN
        IF v_job_id IS NOT NULL THEN
            UPDATE archival_retrieve_summary
            SET retrieve_date = now(),
                end_time = now(),
                status = 'ERROR',
                error_message = LEFT(SQLERRM, 2000)
            WHERE job_id = v_job_id
              AND status <> 'COMPLETED';

            CALL set_archival_last_run('RETRIEVE_PROCESS', 'RETRIEVE_PROCESS', 'ERROR', NULL, SQLERRM, v_job_id, 'Metadata');
        END IF;
        RAISE;
END;
$$;
$archival_stmt$;
END;
$do$;

DO $do$
BEGIN
    EXECUTE $archival_stmt$
-- Wrapper function to match the interface expected by Java code
CREATE OR REPLACE FUNCTION pkg_archival_restore_manager(
    p_utility VARCHAR(10),
    p_password VARCHAR(255) DEFAULT NULL
) RETURNS void 
LANGUAGE plpgsql 
AS $$
DECLARE
    v_dir_nm VARCHAR(50);
    v_default_export_dir TEXT := '/tmp/archival_export';
    v_default_import_dir TEXT := '/tmp/archival_import';
BEGIN
    RAISE NOTICE '[DEBUG pkg_archival_restore_manager] START utility=% password_provided=%',
        p_utility, (p_password IS NOT NULL);

    -- Get archive directory from system properties
    BEGIN
        SELECT description INTO v_dir_nm 
        FROM system_property 
        WHERE upper(name) = 'ARCHIVAL_FILE_DIRECTORY_NAME';
        RAISE NOTICE '[DEBUG pkg_archival_restore_manager] ARCHIVAL_FILE_DIRECTORY_NAME from system_property=%', v_dir_nm;
    EXCEPTION
        WHEN NO_DATA_FOUND THEN
            v_dir_nm := CASE WHEN UPPER(p_utility) = 'ARCHIVAL' THEN v_default_export_dir ELSE v_default_import_dir END;
            RAISE NOTICE '[DEBUG pkg_archival_restore_manager] system_property not found; using default dir=%', v_dir_nm;
    END;
    
    -- Handle ARCHIVAL process
    IF UPPER(p_utility) = 'ARCHIVAL' THEN
        RAISE NOTICE '[DEBUG pkg_archival_restore_manager] Calling sp_archival with dir=%', COALESCE(v_dir_nm, v_default_export_dir);
        CALL sp_archival(
            COALESCE(v_dir_nm, v_default_export_dir)::TEXT,
            12::INT,
            FALSE::BOOLEAN,
            'csv'::TEXT,
            TRUE::BOOLEAN
        );
    
    -- Handle RESTORE process  
    ELSIF UPPER(p_utility) = 'RESTORE' THEN
        CALL sp_restore(
            COALESCE(v_dir_nm, v_default_import_dir),
            to_char(current_date - interval '12 months', 'YYYYMM'),
            to_char(current_date, 'YYYYMM'),
            NULL
        );
    
    ELSE
        RAISE EXCEPTION 'Invalid utility parameter: %. Expected ARCHIVAL or RESTORE', p_utility;
    END IF;

    RAISE NOTICE '[DEBUG pkg_archival_restore_manager] FINISHED utility=%', p_utility;

EXCEPTION
    WHEN OTHERS THEN
        RAISE NOTICE '[DEBUG pkg_archival_restore_manager] ERROR: %', SQLERRM;
        RAISE EXCEPTION 'Error in pkg_archival_restore_manager: %', SQLERRM;
END;
$$;
$archival_stmt$;
END;
$do$;

DO $do$
BEGIN
    EXECUTE $archival_stmt$
-- Additional required functions for compatibility

-- Validate that an archival directory exists and is writable on the PostgreSQL server.
-- Uses COPY TO (same mechanism as sp_export_data) instead of pg_ls_dir, which requires
-- separate EXECUTE grants that the warehouse user typically does not have.
DROP PROCEDURE IF EXISTS pg_validate_archival_directory(TEXT);
$archival_stmt$;
END;
$do$;

DO $do$
BEGIN
    EXECUTE $archival_stmt$
CREATE OR REPLACE PROCEDURE pg_validate_archival_directory(p_path_name TEXT)
LANGUAGE plpgsql
AS $$
DECLARE
    v_path TEXT;
    v_probe_file TEXT;
BEGIN
    IF p_path_name IS NULL OR LENGTH(TRIM(p_path_name)) = 0 THEN
        RAISE EXCEPTION 'Please check Archive Directory Exists and have required permissions: Path cannot be empty'
        USING ERRCODE = 'P0001';
    END IF;

    v_path := rtrim(pg_normalize_file_path(p_path_name), '/');
    v_probe_file := v_path || '/.bds_archival_dir_check.tmp';

    BEGIN
        EXECUTE format('COPY (SELECT 1) TO %L', v_probe_file);
    EXCEPTION
        WHEN OTHERS THEN
            RAISE EXCEPTION 'Please check Archive Directory Exists and have required permissions %', p_path_name
            USING ERRCODE = 'P0001';
    END;
END;
$$;
$archival_stmt$;
END;
$do$;

DO $do$
BEGIN
    EXECUTE $archival_stmt$
-- Function to set archival directory
DROP PROCEDURE IF EXISTS set_archival_dir(TEXT);
$archival_stmt$;
END;
$do$;

DO $do$
BEGIN
    EXECUTE $archival_stmt$
CREATE OR REPLACE PROCEDURE set_archival_dir(p_directory_path TEXT)
LANGUAGE plpgsql
AS $$
BEGIN
    CALL pg_validate_archival_directory(p_directory_path);

    UPDATE system_property 
    SET value = p_directory_path,
        description = p_directory_path
    WHERE upper(name) = 'ARCHIVAL_FILE_DIRECTORY_NAME';
    
    IF NOT FOUND THEN
        INSERT INTO system_property (name, value, description)
        VALUES ('ARCHIVAL_FILE_DIRECTORY_NAME', p_directory_path, p_directory_path);
    END IF;
    
EXCEPTION
    WHEN OTHERS THEN
        IF SQLERRM LIKE 'Please check Archive Directory Exists%' THEN
            RAISE;
        END IF;
        RAISE EXCEPTION 'Procedure set_archival_dir Failed with error message %', SQLERRM;
END;
$$;
$archival_stmt$;
END;
$do$;

DO $do$
BEGIN
    EXECUTE $archival_stmt$
-- Procedure to get archival status runs - implements the core archival file list generation
CREATE OR REPLACE PROCEDURE sp_archival_file_list(p_job_id NUMERIC)
LANGUAGE plpgsql
AS $$
BEGIN
    -- Call the comprehensive file list generation procedure
    CALL sp_generate_archival_file_list(
        p_job_id,
        12,  -- default retention months
        NULL, -- use system directory setting
        'csv',
        'd_date',
        'date_id',
        'full_date'
    );
    
    -- Log completion
    CALL set_archival_last_run(
        'SP_ARCHIVAL_FILE_LIST', 
        'SYSTEM', 
        'COMPLETED', 
        NULL, 
        NULL, 
        p_job_id
    );
END;
$$;
$archival_stmt$;
END;
$do$;

DO $do$
BEGIN
    EXECUTE $archival_stmt$
-- Procedure for BCP operations - PostgreSQL implementation using COPY
CREATE OR REPLACE PROCEDURE sp_tbl_bcp_remove_data(p_password TEXT DEFAULT NULL)
LANGUAGE plpgsql
AS $$
DECLARE
    v_job_id NUMERIC;
    v_export_dir TEXT;
BEGIN
    -- Get current job ID
    SELECT COALESCE(MAX(job_id), 0) INTO v_job_id FROM archival_summary;
    
    -- Get export directory
    SELECT description INTO v_export_dir 
    FROM system_property 
    WHERE upper(name) = 'ARCHIVAL_FILE_DIRECTORY_NAME';
    
    -- Call the main export procedure
    CALL sp_export_data(v_job_id, COALESCE(v_export_dir, '/tmp/archival_export'), false);
    
    -- Log the operation
    CALL set_archival_last_run(
        'SP_TBL_BCP_REMOVE_DATA', 'SYSTEM', 'COMPLETED', 
        NULL, NULL, v_job_id
    );
END;
$$;
$archival_stmt$;
END;
$do$;

DO $do$
BEGIN
    EXECUTE $archival_stmt$
-- Procedure to remove table data after successful archival
DROP PROCEDURE IF EXISTS sp_tbl_remove_data(TEXT);
$archival_stmt$;
END;
$do$;

DO $do$
BEGIN
    EXECUTE $archival_stmt$
DROP PROCEDURE IF EXISTS sp_tbl_remove_data(TEXT, NUMERIC);
$archival_stmt$;
END;
$do$;

DO $do$
BEGIN
    EXECUTE $archival_stmt$
CREATE OR REPLACE PROCEDURE sp_tbl_remove_data(
    p_table_name TEXT,
    p_job_id NUMERIC DEFAULT NULL
)
LANGUAGE plpgsql
AS $$
DECLARE
    v_job_id NUMERIC;
    v_delete_count INTEGER := 0;
    v_total_deleted BIGINT := 0;
    v_where_clause TEXT;
    v_sql TEXT;
    rec RECORD;
BEGIN
    v_job_id := p_job_id;

    IF v_job_id IS NULL THEN
        SELECT COALESCE(MAX(job_id), 0) INTO v_job_id FROM archival_summary;
    END IF;

    IF v_job_id = 0 THEN
        RAISE NOTICE '[DEBUG sp_tbl_remove_data] SKIP table=% - no archival job found', p_table_name;
        RETURN;
    END IF;
    
    CALL set_archival_last_run('SP_TBL_REMOVE_DATA', p_table_name, 'STARTED', NULL, NULL, v_job_id);
    
    -- Delete only rows that were successfully exported in this job
    FOR rec IN
        SELECT where_clause, data_month, file_name, num_rows
        FROM archival_file_list 
        WHERE job_id = v_job_id 
          AND UPPER(table_name) = UPPER(p_table_name)
          AND COALESCE(exp_ind, 0) = 1 
          AND COALESCE(dw_delete_ind, 0) = 0
          AND COALESCE(num_rows, 0) > 0
    LOOP
        BEGIN
            v_sql := format('DELETE FROM %I %s', pg_sql_ident(p_table_name), COALESCE(rec.where_clause, ''));
            RAISE NOTICE '[DEBUG sp_tbl_remove_data] job_id=% table=% file=% DELETE SQL: %',
                v_job_id, p_table_name, rec.file_name, v_sql;

            EXECUTE v_sql;
            
            GET DIAGNOSTICS v_delete_count = ROW_COUNT;
            v_total_deleted := v_total_deleted + v_delete_count;

            RAISE NOTICE '[DEBUG sp_tbl_remove_data] job_id=% table=% file=% deleted_rows=%',
                v_job_id, p_table_name, rec.file_name, v_delete_count;
            
            UPDATE archival_file_list 
            SET dw_delete_ind = 1,
                num_rows = COALESCE(num_rows, v_delete_count)
            WHERE job_id = v_job_id 
              AND UPPER(table_name) = UPPER(p_table_name)
              AND data_month = rec.data_month
              AND file_name = rec.file_name;
              
        EXCEPTION
            WHEN OTHERS THEN
                RAISE NOTICE '[DEBUG sp_tbl_remove_data] ERROR job_id=% table=% file=%: %',
                    v_job_id, p_table_name, rec.file_name, SQLERRM;
                UPDATE archival_file_list 
                SET error_message = LEFT(SQLERRM, 2000)
                WHERE job_id = v_job_id 
                  AND UPPER(table_name) = UPPER(p_table_name)
                  AND data_month = rec.data_month
                  AND file_name = rec.file_name;
        END;
    END LOOP;

    RAISE NOTICE '[DEBUG sp_tbl_remove_data] job_id=% table=% total_deleted=%',
        v_job_id, p_table_name, v_total_deleted;
    
    CALL set_archival_last_run('SP_TBL_REMOVE_DATA', p_table_name, 'COMPLETED', NULL, NULL, v_job_id);
    
EXCEPTION
    WHEN OTHERS THEN
        CALL set_archival_last_run('SP_TBL_REMOVE_DATA', p_table_name, 'ERROR', NULL, SQLERRM, v_job_id);
        RAISE;
END;
$$;
$archival_stmt$;
END;
$do$;

DO $do$
BEGIN
    EXECUTE $archival_stmt$
DROP PROCEDURE IF EXISTS sp_purge_archived_data(NUMERIC);
$archival_stmt$;
END;
$do$;

DO $do$
BEGIN
    EXECUTE $archival_stmt$
CREATE OR REPLACE PROCEDURE sp_purge_archived_data(p_job_id NUMERIC)
LANGUAGE plpgsql
AS $$
DECLARE
    rec RECORD;
BEGIN
    RAISE NOTICE '[DEBUG sp_purge_archived_data] START job_id=%', p_job_id;

    FOR rec IN
        SELECT DISTINCT table_name
        FROM archival_file_list
        WHERE job_id = p_job_id
          AND COALESCE(exp_ind, 0) = 1
          AND COALESCE(dw_delete_ind, 0) = 0
          AND COALESCE(num_rows, 0) > 0
        ORDER BY table_name
    LOOP
        CALL sp_tbl_remove_data(rec.table_name, p_job_id);
    END LOOP;

    RAISE NOTICE '[DEBUG sp_purge_archived_data] END job_id=%', p_job_id;
END;
$$;
$archival_stmt$;
END;
$do$;

DO $do$
BEGIN
    EXECUTE $archival_stmt$
-- Procedure to reset archival config data
CREATE OR REPLACE PROCEDURE sp_reset_archival_config_data()
LANGUAGE plpgsql
AS $$
DECLARE
    v_job_id NUMERIC(38,0);
    v_count INTEGER := 0;
BEGIN
    CALL set_archival_last_run('SP_RESET_ARCHIVAL_CONFIG_DATA', 'SP', 'STARTED', NULL, NULL, NULL);

    SELECT COALESCE(MAX(job_id), 0) INTO v_job_id FROM archival_summary;

    IF v_job_id > 0 THEN
        CALL sp_update_archival_summary(v_job_id);
        SELECT COUNT(1) INTO v_count FROM archival_summary WHERE job_id = v_job_id AND status = 'ERROR';
    END IF;

    IF v_count = 0 THEN
        UPDATE archival_config
        SET archival_ind = 0,
            purge_ind = 0;

        UPDATE archival_last_run
        SET end_time = now(),
            status = 'COMPLETED'
        WHERE (end_time IS NULL OR status = 'STARTED');

        TRUNCATE TABLE archival_file_list;
        TRUNCATE TABLE archival_process_tbl;
    ELSE
        UPDATE archival_last_run
        SET end_time = now(),
            status = 'ERROR'
        WHERE (end_time IS NULL OR status = 'STARTED');
    END IF;

    CALL set_archival_last_run('SP_RESET_ARCHIVAL_CONFIG_DATA', 'SP', 'COMPLETED', NULL, NULL, v_job_id);
END;
$$;
$archival_stmt$;
END;
$do$;

DO $do$
BEGIN
    EXECUTE $archival_stmt$
-- Procedure to update archival summary
CREATE OR REPLACE PROCEDURE sp_update_archival_summary(p_job_id NUMERIC)
LANGUAGE plpgsql
AS $$
BEGIN
    UPDATE archival_summary a
    SET end_time = (
        SELECT max(end_time)
        FROM archival_last_run b
        WHERE a.job_id = b.job_id
          AND a.domain_name = b.domain_name
          AND a.job_id = p_job_id
    )
    WHERE a.job_id = p_job_id
      AND (a.start_time IS NOT NULL AND a.end_time IS NULL);

    UPDATE archival_summary a
    SET start_time = t.min_start_time,
        end_time = t.max_end_time
    FROM (
        SELECT domain_name, min(start_time) AS min_start_time, max(end_time) AS max_end_time
        FROM archival_last_run
        WHERE job_id = p_job_id
        GROUP BY domain_name
    ) t
    WHERE a.job_id = p_job_id
      AND a.domain_name = t.domain_name
      AND (a.start_time IS NULL AND a.end_time IS NULL);

    UPDATE archival_summary a
    SET archival_date = now(),
        error_message = (
            SELECT alr.error_message
            FROM archival_last_run alr
            WHERE alr.error_message IS NOT NULL
              AND alr.job_id = a.job_id
              AND NOT EXISTS (
                  SELECT 1
                  FROM archival_last_run b
                  WHERE alr.job_id = b.job_id
                    AND b.step_name = alr.step_name
                    AND b.table_name = alr.table_name
                    AND b.archival_last_run_id > alr.archival_last_run_id
                    AND b.status = 'COMPLETED'
              )
            ORDER BY alr.archival_last_run_id DESC
            LIMIT 1
        )
    WHERE a.job_id = p_job_id
      AND a.status <> 'COMPLETED';

    UPDATE archival_summary
    SET status = CASE WHEN error_message IS NULL THEN 'COMPLETED' ELSE 'ERROR' END
    WHERE job_id = p_job_id
      AND status <> 'COMPLETED';
END;
$$;
$archival_stmt$;
END;
$do$;

DO $do$
BEGIN
    EXECUTE $archival_stmt$
-- Procedure to stop archival process
CREATE OR REPLACE PROCEDURE sp_stop_archival(p_job_id NUMERIC)
LANGUAGE plpgsql
AS $$
BEGIN
    CALL set_archival_last_run('SP_STOP_ARCHIVAL', 'SP', 'STARTED', NULL, NULL, p_job_id);

    UPDATE archival_summary a
    SET archival_date = now(),
        error_message = 'Archival stopped by user externally',
        status = 'ERROR'
    WHERE a.job_id = p_job_id
      AND a.status NOT IN ('COMPLETED','WAITING');

    UPDATE archival_config SET archival_ind = 0, purge_ind = 0;

    UPDATE archival_last_run
    SET end_time = now(),
        status = 'ERROR',
        error_message = 'Archival stopped by user externally'
    WHERE (end_time IS NULL OR status = 'STARTED');

    TRUNCATE TABLE archival_file_list;
    TRUNCATE TABLE archival_process_tbl;

    CALL set_archival_last_run('SP_STOP_ARCHIVAL', 'SP', 'COMPLETED', NULL, NULL, p_job_id);
END;
$$;
$archival_stmt$;
END;
$do$;

DO $do$
BEGIN
    EXECUTE $archival_stmt$
-- Retrieval related procedures
CREATE OR REPLACE PROCEDURE sp_retrieve_file_list(p_job_id NUMERIC)
LANGUAGE plpgsql
AS $$
BEGIN
    CALL set_archival_last_run('SP_RETRIEVE_FILE_LIST', 'SP', 'STARTED', NULL, NULL, p_job_id);
    
    CALL sp_prepare_retrieve_file_list(p_job_id);

    CALL set_archival_last_run('SP_RETRIEVE_FILE_LIST', 'SP', 'COMPLETED', NULL, NULL, p_job_id);
END;
$$;
$archival_stmt$;
END;
$do$;

DO $do$
BEGIN
    EXECUTE $archival_stmt$
CREATE OR REPLACE PROCEDURE sp_retrieve_file_data(p_password TEXT DEFAULT NULL)
LANGUAGE plpgsql
AS $$
DECLARE
    v_job_id NUMERIC(38,0);
    v_dir_nm TEXT;
    v_default_import_dir TEXT := '/tmp/archival_import';
BEGIN
    SELECT COALESCE(MAX(job_id), 0) INTO v_job_id FROM archival_retrieve_summary;

    CALL set_archival_last_run('SP_RETRIEVE_FILE_DATA', 'SP', 'STARTED', NULL, NULL, v_job_id);

    BEGIN
        SELECT description INTO v_dir_nm
        FROM system_property
        WHERE upper(name) = 'ARCHIVAL_FILE_DIRECTORY_NAME';
    EXCEPTION
        WHEN OTHERS THEN
            v_dir_nm := v_default_import_dir;
    END;

    CALL sp_restore_data(v_job_id, COALESCE(v_dir_nm, v_default_import_dir));
    
    CALL set_archival_last_run('SP_RETRIEVE_FILE_DATA', 'SP', 'COMPLETED', NULL, NULL, v_job_id);
END;
$$;
$archival_stmt$;
END;
$do$;

DO $do$
BEGIN
    EXECUTE $archival_stmt$
CREATE OR REPLACE PROCEDURE sp_reset_retrival_config_data()
LANGUAGE plpgsql
AS $$
DECLARE
    v_job_id NUMERIC(38,0);
    v_count INTEGER := 0;
BEGIN
    CALL set_archival_last_run('SP_RESET_RETRIVAL_CONFIG_DATA', 'SP', 'STARTED', NULL, NULL, NULL);

    SELECT COALESCE(MAX(job_id), 0) INTO v_job_id FROM archival_retrieve_summary;

    IF v_job_id > 0 THEN
        CALL sp_update_retrieve_summary(v_job_id);
        SELECT COUNT(1) INTO v_count FROM archival_retrieve_summary WHERE job_id = v_job_id AND status = 'ERROR';
    END IF;

    IF v_count = 0 THEN
        UPDATE archival_last_run
        SET end_time = now(),
            status = 'COMPLETED'
        WHERE (end_time IS NULL OR status = 'STARTED');

        TRUNCATE TABLE archival_retrieve_file_list;
        TRUNCATE TABLE archival_retrieve_process_tbl;

        DELETE FROM retrival_configuration;
    ELSE
        UPDATE archival_last_run
        SET end_time = now(),
            status = 'ERROR'
        WHERE (end_time IS NULL OR status = 'STARTED');
    END IF;

    CALL set_archival_last_run('SP_RESET_RETRIVAL_CONFIG_DATA', 'SP', 'COMPLETED', NULL, NULL, v_job_id);
END;
$$;
$archival_stmt$;
END;
$do$;

DO $do$
BEGIN
    EXECUTE $archival_stmt$
CREATE OR REPLACE PROCEDURE sp_update_retrieve_summary(p_job_id NUMERIC)
LANGUAGE plpgsql
AS $$
BEGIN
    UPDATE archival_retrieve_summary a
    SET start_time = t.min_start_time,
        end_time = t.max_end_time
    FROM (
        SELECT domain_name, min(start_time) AS min_start_time, max(end_time) AS max_end_time
        FROM archival_last_run
        WHERE job_id = p_job_id
        GROUP BY domain_name
    ) t
    WHERE a.job_id = p_job_id
      AND a.domain_name = t.domain_name
      AND (a.start_time IS NOT NULL AND a.end_time IS NULL);

    UPDATE archival_retrieve_summary a
    SET start_time = t.min_start_time,
        end_time = t.max_end_time
    FROM (
        SELECT domain_name, min(start_time) AS min_start_time, max(end_time) AS max_end_time
        FROM archival_last_run
        WHERE job_id = p_job_id
        GROUP BY domain_name
    ) t
    WHERE a.job_id = p_job_id
      AND a.domain_name = t.domain_name
      AND (a.start_time IS NULL AND a.end_time IS NULL);

    UPDATE archival_retrieve_summary a
    SET retrieve_date = now(),
        error_message = (
            SELECT alr.error_message
            FROM archival_last_run alr
            WHERE alr.error_message IS NOT NULL
              AND alr.job_id = a.job_id
              AND NOT EXISTS (
                  SELECT 1
                  FROM archival_last_run b
                  WHERE alr.job_id = b.job_id
                    AND b.step_name = alr.step_name
                    AND b.table_name = alr.table_name
                    AND b.archival_last_run_id > alr.archival_last_run_id
                    AND b.status = 'COMPLETED'
              )
            ORDER BY alr.archival_last_run_id DESC
            LIMIT 1
        )
    WHERE a.job_id = p_job_id
      AND a.status <> 'COMPLETED';

    UPDATE archival_retrieve_summary
    SET status = CASE WHEN error_message IS NULL THEN 'COMPLETED' ELSE 'ERROR' END
    WHERE job_id = p_job_id
      AND status <> 'COMPLETED';
END;
$$;
$archival_stmt$;
END;
$do$;

DO $do$
BEGIN
    EXECUTE $archival_stmt$
CREATE OR REPLACE PROCEDURE sp_stop_retrival(p_job_id NUMERIC)
LANGUAGE plpgsql
AS $$
BEGIN
    CALL set_archival_last_run('SP_STOP_RETRIVAL', 'SP', 'STARTED', NULL, NULL, p_job_id);

    UPDATE archival_retrieve_summary a
    SET retrieve_date = now(),
        error_message = 'Restoration stopped by user externally',
        status = 'ERROR'
    WHERE a.job_id = p_job_id
      AND a.status NOT IN ('COMPLETED','WAITING');

    UPDATE archival_last_run
    SET end_time = now(),
        status = 'ERROR',
        error_message = 'Restoration stopped by user externally'
    WHERE (end_time IS NULL OR status = 'STARTED')
      AND step_name <> 'SP_STOP_RETRIVAL';

    TRUNCATE TABLE archival_retrieve_file_list;
    TRUNCATE TABLE archival_retrieve_process_tbl;

    CALL set_archival_last_run('SP_STOP_RETRIVAL', 'SP', 'COMPLETED', NULL, NULL, p_job_id);
END;
$$;
$archival_stmt$;
END;
$do$;

DO $do$
BEGIN
    EXECUTE $archival_stmt$
-- Directory validation procedure for PostgreSQL
DROP PROCEDURE IF EXISTS dir_check(TEXT);
$archival_stmt$;
END;
$do$;

DO $do$
BEGIN
    EXECUTE $archival_stmt$
CREATE OR REPLACE PROCEDURE dir_check(p_path_name TEXT)
LANGUAGE plpgsql
AS $$
BEGIN
    CALL pg_validate_archival_directory(p_path_name);
END;
$$;
$archival_stmt$;
END;
$do$;

DO $do$
BEGIN
    EXECUTE $archival_stmt$
-- Procedure for archive/restore rerun functionality
CREATE OR REPLACE PROCEDURE sp_archival_restore_rerun(
    p_new_job_id NUMERIC,
    p_failed_job_id NUMERIC,
    p_process_type TEXT -- 'ARCH' for archival, 'REST' for restore
)
LANGUAGE plpgsql
AS $$
DECLARE
    v_count INTEGER := 0;
BEGIN
    IF UPPER(p_process_type) = 'ARCH' THEN
        -- Copy configuration from failed archival job
        INSERT INTO archival_summary (job_id, domain_name, data_retention_months, archival_date, status)
        SELECT p_new_job_id, domain_name, data_retention_months, now(), 'WAITING'
        FROM archival_summary
        WHERE job_id = p_failed_job_id;
        
        GET DIAGNOSTICS v_count = ROW_COUNT;
        
    ELSIF UPPER(p_process_type) = 'REST' THEN
        -- Copy configuration from failed restore job
        INSERT INTO archival_retrieve_summary (job_id, domain_name, start_month, end_month, retrieve_date, status)
        SELECT p_new_job_id, domain_name, start_month, end_month, now(), 'WAITING'
        FROM archival_retrieve_summary
        WHERE job_id = p_failed_job_id;
        
        GET DIAGNOSTICS v_count = ROW_COUNT;
    END IF;
    
    IF v_count = 0 THEN
        RAISE EXCEPTION 'No configuration found for failed job ID: %', p_failed_job_id;
    END IF;
    
    RAISE NOTICE 'Rerun setup completed for job % based on failed job %', p_new_job_id, p_failed_job_id;
END;
$$;
$archival_stmt$;
END;
$do$;
