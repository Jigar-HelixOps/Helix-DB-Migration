# Oracle → SQL Server Migration Utility

Standalone utility to migrate data from **Oracle** to **SQL Server**. Provide connection details and matching schema names — it discovers tables with the same name in both databases and migrates them automatically.

Same flow as `migration/` (Oracle/SQL Server → PostgreSQL), adapted for SQL Server as the target: CSV export, batched inserts via pyodbc, `IDENTITY_INSERT`, truncate/FK prepare, and Oracle sequence → SQL Server IDENTITY sync.

## Prerequisites

- Python 3.6+ (3.10+ recommended)
- Network access to Oracle and SQL Server
- Tables already created in SQL Server with the same names as Oracle (case-insensitive match)
- ODBC Driver 17 (or 18) for SQL Server installed on the migration host

## Setup

```bash
cd oratosql
python3 -m venv .venv
source .venv/bin/activate   # Windows: .\.venv\Scripts\Activate.ps1
python -m pip install --upgrade "pip<22" "setuptools<59" wheel
python -m pip install -r requirements.txt
cp config.example.yaml config.yaml
```

**Python 3.6 note:** stock pip 9.x cannot install modern `oracledb` wheels and fails building from source (`from __future__ import annotations`). Always upgrade pip/setuptools/wheel first as above. If `oracledb` still fails, install pinned packages individually:

```bash
python -m pip install oracledb==1.4.2 PyYAML==5.4.1 "pyodbc>=4.0.35,<5.0.0"
```

Prefer Python 3.8+ when the host has it (`python3.8 -m venv .venv`).

Edit `config.yaml` with Oracle and SQL Server connection details.

### Config overview

```yaml
oracle:
  user: oracle_user
  password: oracle_password
  dsn: localhost:1521/ORCLPDB1
  schema: BLADELOGIC

sqlserver:
  host: localhost
  port: 1433
  database: TSSA
  user: sa
  password: sqlserver_password
  schema: dbo
  driver: "ODBC Driver 17 for SQL Server"
  trust_server_certificate: true

migrate:
  tables: []
  exclude_tables: []   # skips data migrate + IDENTITY sync
  lowercase_columns: false
  column_mapping_file: ./column_mapping.yaml
  truncate_before_import: true
  truncate_all_before_migration: true
  disable_foreign_keys: true
  disable_triggers: true
  restart_identity: true
  sync_identities_after_migration: true
```

Environment variable overrides:

- `ORACLE_USER`, `ORACLE_PASSWORD`, `ORACLE_DSN`, `ORACLE_SCHEMA`
- `MSSQL_HOST`, `MSSQL_PORT`, `MSSQL_DATABASE`, `MSSQL_USER`, `MSSQL_PASSWORD`, `MSSQL_SCHEMA`, `MSSQL_DRIVER`, `MSSQL_CONNECTION_STRING`

## Migrate everything

```powershell
python -m oracle_mssql_migrate -v
```

That will:

1. Find all tables that exist in both Oracle schema and SQL Server schema with the same name
2. Export each table from Oracle to CSV
3. Import each CSV into the matching SQL Server table (`IDENTITY_INSERT` when needed)
4. Sync SQL Server IDENTITY values from Oracle sequences

## Optional flags

```powershell
# Preview matching tables
python -m oracle_mssql_migrate --list-tables

# Single table
python -m oracle_mssql_migrate --table employees

# Truncate before import
python -m oracle_mssql_migrate --truncate

# Identity sync only (no data migration)
python -m oracle_mssql_migrate --sync-identities-only -v
```

## Identity sync (Oracle sequences → SQL Server IDENTITY)

Tables in `migrate.exclude_tables` are skipped for **both** data migration and IDENTITY sync.

After data load (or with `--sync-identities-only`), the utility:

1. Reads Oracle `ALL_SEQUENCES` (`LAST_NUMBER`)
2. Matches `seq_<table>` names to SQL Server tables with IDENTITY columns
3. Reseeds with `DBCC CHECKIDENT` so the next insert is `max(oracle_next, max(column)+1)`

Check `logs/migrate_*_sequences.csv` for per-sequence results.

## Column mapping

Optional `column_mapping.yaml` (see `column_mapping.example.yaml`):

```yaml
global_columns:
  # oracle_col: sqlserver_col

tables:
  my_table:
    oracle_col: sqlserver_col
```

## Logs and summary

Every run writes under `logs/`:

```text
logs/migrate_YYYYMMDD_HHMMSS.log
logs/migrate_YYYYMMDD_HHMMSS.summary.txt
logs/migrate_YYYYMMDD_HHMMSS_tables.csv
logs/migrate_YYYYMMDD_HHMMSS_sequences.csv
```

CSV exports are saved under `export_data/`.

## How it works

- Table names matched case-insensitively (`EMPLOYEES` = `employees`)
- Target column order comes from the SQL Server catalog
- Batched `INSERT` with `SET IDENTITY_INSERT ON` when the CSV includes the identity column
- Triggers and foreign keys are disabled on **all tables in the SQL Server schema**
  during load (including `exclude_tables`), then restored afterward — batched DDL.
- Clear phase: FK-referenced tables use batched `DELETE`; others use batched
  single-table `TRUNCATE` statements (SQL Server does not support multi-table TRUNCATE).
- Oracle schema/table names are uppercased in quoted SQL (`"P0APPSERVER4LIN"."ACE"`).
- Column mapping format is `oracle_column: sqlserver_column` (see `column_mapping.yaml`).
- Overlong Oracle strings are truncated to the SQL Server column length (logged once).
- SQL Server-only columns (not in Oracle) are skipped; INSERT omits them.
- Oracle LOBs are fetched inline (`fetch_lobs=False`) for much faster large-table export.

### Transaction log full (`LOG_BACKUP` / error 9002)

Bulk inserts under **FULL** recovery fill the log if log backups are not running. Before a large run:

```sql
-- Option A (lab / one-time migration): switch to SIMPLE, then back after
ALTER DATABASE win2k16app1_new SET RECOVERY SIMPLE;
-- ... run migration ...
ALTER DATABASE win2k16app1_new SET RECOVERY FULL;

-- Option B: keep FULL, free log space
BACKUP LOG win2k16app1_new TO DISK = 'C:\backup\win2k16app1_new_log.bak' WITH INIT;
```

## Troubleshooting

- **No matching tables**: verify schemas and that SQL Server tables already exist
- **Column mismatch**: add entries in `column_mapping.yaml`, or confirm names match
- **Oracle connection errors**: verify `dsn` (`host:port/service_name`)
- **ODBC errors**: confirm Driver 17/18 is installed and `sqlserver.driver` matches
- **IDENTITY_INSERT permission**: login needs ability to set IDENTITY_INSERT on target tables
