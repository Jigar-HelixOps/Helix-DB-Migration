# Data Migration Utility (Oracle / SQL Server → PostgreSQL)

Standalone utility to migrate data from **Oracle** or **SQL Server** to PostgreSQL in one step. Set `source.type` in config, provide connection details and matching schema names — it discovers tables with the same name in both databases and migrates them automatically.

## Prerequisites

- Python 3.6+ (3.10+ recommended)
- Network access to source database and PostgreSQL
- Tables already created in PostgreSQL with the same names as source (case-insensitive match)
- Source schema and `postgres.schema` configured
- For SQL Server: ODBC Driver 17 (or 18) for SQL Server installed on the migration host

## Setup

```powershell
cd migration

linux - source .venv/bin/activate
windows  - .\.venv\Scripts\Activate.ps1
python -m venv .venv
python -m pip install -r requirements.txt
copy config.example.yaml config.yaml


ModuleNotFoundError: No module named 'yaml'

The virtual environment is missing PyYAML. Install the dependencies again:

cd /home/jigar-mig/migration
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
Then verify:

python -c "import yaml; print('PyYAML OK')"
Run migration again:

python -m oracle_pg_migrate -v
If pip install fails, install packages individually:

python -m pip install oracledb==1.4.2 psycopg2-binary==2.9.5 PyYAML==6.0.1
If the venv looks broken, recreate it:

cd /home/jigar-mig/migration
deactivate 2>/dev/null || true
rm -rf .venv
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip setuptools wheel
python -m pip install -r requirements.txt
python -m oracle_pg_migrate -v

# Full TSSA load
python -m oracle_pg_migrate -c config.yaml -v

# Full DW load
python -m oracle_pg_migrate -c config.tssa_dw.yaml -v

```

Edit `config.yaml` with your source and PostgreSQL connection details.

### Oracle source (default)

```yaml
source:
  type: oracle

oracle:
  user: oracle_user
  password: oracle_password
  dsn: ora19c
  schema: BLADELOGIC
```

### SQL Server source

```yaml
source:
  type: sqlserver

sqlserver:
  host: localhost
  port: 1433
  database: mydb
  user: sa
  password: secret
  schema: dbo
  driver: "ODBC Driver 17 for SQL Server"
  trust_server_certificate: true
```

Environment variable overrides:

- `SOURCE_TYPE` — `oracle` or `sqlserver`
- `ORACLE_USER`, `ORACLE_PASSWORD`, `ORACLE_DSN`, `ORACLE_SCHEMA`
- `MSSQL_HOST`, `MSSQL_PORT`, `MSSQL_DATABASE`, `MSSQL_USER`, `MSSQL_PASSWORD`, `MSSQL_SCHEMA`, `MSSQL_DRIVER`, `MSSQL_CONNECTION_STRING`
- `PGHOST`, `PGPORT`, `PGDATABASE`, `PGUSER`, `PGPASSWORD`, `PGSCHEMA`

## Migrate everything in one go

```powershell
python -m oracle_pg_migrate
```

That will:

1. Find all tables that exist in both source schema and `postgres.schema` with the same name
2. Export each table from Oracle or SQL Server to CSV
3. Import each CSV into the matching PostgreSQL table
4. Optionally sync sequences and validate constraints

## Optional flags

Preview matching tables without migrating:

```powershell
python -m oracle_pg_migrate --list-tables
```

Migrate a single table:

```powershell
python -m oracle_pg_migrate --table employees
```

Truncate PostgreSQL tables before loading:

```powershell
python -m oracle_pg_migrate --truncate
```

Verbose logging:

```powershell
python -m oracle_pg_migrate -v
```

Validate constraints only (no migration):

```powershell
python -m oracle_pg_migrate --validate-only
python -m oracle_pg_migrate --validate-only --table employees
```

Sync sequences only (no data migration):

```powershell
python -m oracle_pg_migrate --sync-sequences-only -v
```

## Post-migration SQL

After data load and sequence sync, the utility runs `post_mig.sql` against PostgreSQL (search_path = `postgres.schema`) to rewrite application server / PXE hostnames.

Configure in `config.yaml`:

```yaml
post_migration:
  enabled: true
  sql_file: ./post_mig.sql
  app_server_host: bcx-pun-ea66409       # new Postgres app server host
  pxe_server_host: bcx-pun-ea66409_pxe   # new PXE server host
  old_app_server_host: p0app2k16         # source Oracle/SQL Server app hostname
```

The `system_property` update (`DatabaseType = Postgres`) stays hardcoded in `post_mig.sql`. Set `post_migration.enabled: false` to skip.

## Sequence sync (Oracle vs SQL Server)

Tables listed in `migrate.exclude_tables` are skipped for **both** data migration and sequence/IDENTITY sync (Oracle named sequences and SQL Server IDENTITY).

**Oracle → PostgreSQL:** Oracle exposes named sequences (`ALL_SEQUENCES`). The utility matches them by name to PostgreSQL sequences and sets each to `max(oracle_next_value, max(column)+1)`.

**SQL Server → PostgreSQL:** TSSA schemas typically use **IDENTITY columns** on tables, not standalone `sys.sequences`. Named sequence sync alone will report 0 updated. After data migration, the utility therefore:

1. Reads SQL Server `sys.identity_columns` for the next identity value per table/column
2. Finds PostgreSQL sequences linked to table columns (`pg_depend`)
3. Maps SQL Server table/column names via `column_mapping.yaml` when they differ from PostgreSQL
4. Sets each linked PG sequence with `setval(seq, max(identity_next, max_column+1), false)`

Check `logs/migrate_*_sequences.csv` for `source_kind=identity` and sources like `tablename.columnname (IDENTITY)`.

If an identity column is not synced, confirm the PG sequence is linked to the column, the SQL Server table has IDENTITY on the mapped source column, and add a `column_mapping.yaml` entry when names differ.

## Partition columns (SQL Server → PostgreSQL)

Oracle TSSA adds `lib_partition_result_date` / `lib_partition_event_date` via `lib_partition.sql`. SQL Server partitions on existing columns (usually `date_created`) and does not add those `lib_*` names.

Resolution order for `lib_partition_result_date` / `lib_partition_event_date`:

1. Same column name on the source (typical **Oracle** after `lib_partition.sql`)
2. Column mapping / `global_columns` in YAML
3. Auto-map from source date/datetime columns (typical **SQL Server**)
4. If still no source — **skip** the PostgreSQL partition column (no error); COPY omits that column and PostgreSQL applies a column default if one exists

Override in `column_mapping.yaml` under `global_columns` if your schema uses a different source column.

**Quoted / mixed-case PostgreSQL columns** (e.g. `"USER"`, `"Select Option"`): export and COPY always use the exact name from the PostgreSQL catalog. Do not rely on `lowercase_columns` for those; map `source: ExactPgName` only when Oracle and PostgreSQL names differ.

## Config options

```yaml
oracle:
  schema: HR          # required

postgres:
  schema: public      # required

migrate:
  tables: []
  exclude_tables: []
  lowercase_columns: true
  truncate_before_import: true
  truncate_all_before_migration: true
  truncate_cascade: true
  restart_identity: true
  disable_triggers: true
  disable_foreign_keys: true
  disable_row_security: true
  analyze_after_migration: true
```

If `migrate.tables` is empty, all matching tables are migrated.

### Migrating into a schema with existing objects

Use these options when PostgreSQL already has tables, constraints, triggers, indexes, views, and core/seed data:

| Option | Purpose |
|--------|---------|
| `truncate_before_import` | Clear existing table data before load |
| `truncate_all_before_migration` | Truncate all matching tables once at the start |
| `truncate_cascade` | Handle foreign keys between tables during truncate |
| `restart_identity` | Reset serial/identity columns after truncate |
| `disable_triggers` | Disable triggers during data load |
| `disable_foreign_keys` | Disable FK checks during data load |
| `disable_row_security` | Disable row-level security during load |
| `analyze_after_migration` | Update optimizer statistics after load |

**Not required to disable:** views, procedures, functions, indexes. These do not block data load. Indexes may slow inserts slightly; `analyze_after_migration` helps afterward.

Triggers and foreign keys are disabled using `SET session_replication_role = replica` and restored to `origin` after migration. If that requires elevated privileges, the utility falls back to `ALTER TABLE ... DISABLE TRIGGER ALL` per table.

-- Run as postgres superuser
GRANT SET ON PARAMETER session_replication_role TO tssa_login_user;

## Example

Oracle schema `HR` has `EMPLOYEES` and `DEPARTMENTS`.  
PostgreSQL schema `public` has `employees` and `departments`.

```powershell
python -m oracle_pg_migrate
```

Output:

```
Migrated 2 table(s), 1500 total rows
  EMPLOYEES: 1200 rows
  DEPARTMENTS: 300 rows
```

## How it works

- Table names are matched case-insensitively (`EMPLOYEES` = `employees`)
- Column headers are lowercased by default for PostgreSQL compatibility
- Data is exported in batches and loaded with PostgreSQL `COPY` for speed
- CSV files are saved under `export_data/` for review

## Logs and summary report

Every run writes these files under `logs/`:

```text
logs/migrate_20260928_233000.log            # full detailed log
logs/migrate_20260928_233000.summary.txt    # text summary report
logs/migrate_20260928_233000_tables.csv     # per-table results (open in Excel)
logs/migrate_20260928_233000_validation.csv # constraint issues (if any)
```

### Tables CSV columns

| Column | Description |
|--------|-------------|
| `table_name` | Table name |
| `oracle_schema` / `oracle_table` | Oracle source |
| `postgres_schema` / `postgres_table` | PostgreSQL target |
| `status` | `SUCCESS` or `FAILED` |
| `rows_exported` | Rows read from Oracle |
| `rows_imported` | Rows loaded into PostgreSQL |
| `rows_match` | `YES` if exported = imported |
| `duration_seconds` | Time taken per table |
| `error_message` | Error details if failed |

The summary report includes:

- start/end time and total duration
- Oracle and PostgreSQL connection details
- per-table status (`SUCCESS` / `FAILED`)
- rows exported and imported per table
- failed tables and error messages

At the end of each run, the utility prints the log and summary file paths.

Configure log directory in `config.yaml`:

```yaml
logging:
  log_dir: ./logs
```

Use `-v` or `-vv` for more console output. The log file always captures detailed `INFO`/`DEBUG` messages.

## Validate constraints only

After migration, or at any time, run constraint validation without reloading data:

```powershell
python -m oracle_pg_migrate --validate-only
```

Validate one table:

```powershell
python -m oracle_pg_migrate --validate-only --table dbm_jobrun_stat
```

Checks performed:

- Foreign key orphan rows
- UNIQUE constraint duplicates
- NOT NULL violations
- `VALIDATE CONSTRAINT` for any NOT VALID constraints

Results are written to the log file and summary report under `logs/`.

## Troubleshooting

- **No matching tables found**: verify schemas and that PostgreSQL tables already exist
- **Column mismatch**: ensure PostgreSQL column names match Oracle columns (lowercase by default)
- **Oracle connection errors**: verify `dsn` format (`host:port/service_name`)
- **Permission errors**: Oracle user needs `SELECT` on tables; PostgreSQL user needs `INSERT`
