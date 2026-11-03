# Community / blog draft — Postgres Data Migration Utility

Copy into Helix Community (News / Blog). Adjust names, product versions, and “how to get it” to match your internal process. Remove sections you do not want public.

---

## Suggested title options

1. **Introducing a Postgres Data Migration Utility for TrueSight Server Automation (TSSA) and TSSA-DW**
2. **Oracle and SQL Server to PostgreSQL: A Practical Migration Utility for TSSA Environments**
3. **Simplifying TSSA Database Migration to PostgreSQL**

**Recommended:** Option 1

---

## Suggested summary / teaser (1–2 lines)

A lightweight utility to migrate TrueSight Server Automation (and TSSA-DW) data from Oracle or SQL Server into PostgreSQL—with table discovery, bulk load, sequence/IDENTITY sync, and clear migration reports.

---

## Full post body

### Introducing a Postgres Data Migration Utility for TrueSight Server Automation (TSSA) and TSSA-DW

Migrating TrueSight Server Automation databases to PostgreSQL is a common step for customers modernizing their data platform. Between schema differences, large table counts, identity/sequence handling, and operational cutover needs, a repeatable, auditable approach matters as much as raw throughput.

We have built a **Postgres Data Migration Utility** that moves data from **Oracle or Microsoft SQL Server** into **PostgreSQL** for **TSSA** (and **TSSA-DW** warehouse) environments. It is designed to be run by database and platform teams during migration projects, dry-runs, and validation cycles.

---

### Why we built it

Typical TSSA migrations involve:

- Hundreds of tables with foreign keys, triggers, and existing seed/core data in PostgreSQL  
- Different source models: **Oracle named sequences** vs **SQL Server IDENTITY** columns  
- Occasional column-name differences between source and PostgreSQL  
- Partition-related columns on OLTP schemas that do not always exist the same way on SQL Server or DW  
- A need for clear success/failure reporting after long-running loads  

This utility focuses on **data movement and cutover helpers** (truncate, load, sequence sync, validation), assuming PostgreSQL objects are already created (for example from a supported TSSA PostgreSQL schema).

---

### What it supports

| Capability | Description |
|------------|-------------|
| **Source databases** | Oracle or SQL Server (`source.type` in config) |
| **Target** | PostgreSQL (schema-aware) |
| **Scope** | TSSA OLTP and TSSA-DW (separate configs recommended) |
| **Table discovery** | Migrates tables that exist in **both** source and PostgreSQL (same name, case-insensitive) |
| **Exclude list** | `exclude_tables` skips tables for **data migration and sequence/IDENTITY sync** |
| **Bulk load** | Export to CSV, load with PostgreSQL `COPY` |
| **Safety options** | Truncate (including cascade), disable triggers/FK checks during load, analyze afterward |
| **Column mapping** | Optional YAML for renamed columns and special expressions |
| **Sequence sync** | Oracle: align PG sequences from `ALL_SEQUENCES`. SQL Server: map IDENTITY to PG sequences (`seq_<table>` and column-owned sequences) |
| **Validation** | Optional constraint validation after migration |
| **Reporting** | Per-run log, summary, tables CSV, sequences CSV, validation CSV |

---

### How a typical run works

1. Configure connections and schema names in YAML (`config.yaml` for TSSA; a separate file for TSSA-DW is recommended).  
2. Preview scope:  
   `python -m oracle_pg_migrate -c config.yaml --list-tables`  
3. Run a single table (smoke test) or full migration:  
   `python -m oracle_pg_migrate -c config.yaml -v`  
4. Sync sequences after load (or rely on post-migration sync):  
   `python -m oracle_pg_migrate -c config.yaml --sync-sequences-only -v`  
5. Optionally validate constraints:  
   `python -m oracle_pg_migrate -c config.yaml --validate-only`  
6. Review `logs/` (or DW log directory): summary, failed tables, sequence results.

**Full migration tip:** For FK-heavy TSSA schemas, prefer a full run with truncate-all upfront rather than many isolated `--table` runs, which can hit CASCADE / FK side effects.

---

### Oracle vs SQL Server — important differences

**Oracle → PostgreSQL**  
Named sequences (for example `SEQ_JOB_RUN` → `seq_job_run`) usually align by name. Partition helper columns such as `LIB_PARTITION_*` are often present when Oracle partitioning scripts were applied.

**SQL Server → PostgreSQL**  
TSSA commonly uses **IDENTITY** columns rather than standalone sequences. The utility reads IDENTITY next values and sets the matching PostgreSQL sequence using `max(identity_next, max(column)+1)`. Tables listed in `exclude_tables` are skipped for IDENTITY sync as well.

**TSSA-DW**  
Warehouse schemas typically do not use the same partition-column patterns as OLTP. Use a dedicated DW config (and disable partition-column handling when appropriate) so DW runs stay simple and focused.

---

### Reporting you can take to stakeholders

Every run produces artifacts such as:

- Detailed log  
- Text summary (including failed tables)  
- Tables CSV (exported vs imported row counts)  
- Sequences CSV (source vs PostgreSQL next values)  
- Validation CSV (when validation is enabled)  

That makes dry-runs and production cutovers easier to review and sign off.

---

### Who should use it

- TSSA / Automation administrators preparing a PostgreSQL cutover  
- DBAs running Oracle or SQL Server → PostgreSQL data loads for TSSA or TSSA-DW  
- Project teams that need repeatable migrations with clear logs for audit and support  

---

### Prerequisites (high level)

- Python environment with the utility dependencies (Oracle client / ODBC Driver for SQL Server / PostgreSQL connectivity as applicable)  
- Network access from the migration host to source and PostgreSQL  
- PostgreSQL schema objects already deployed for the target TSSA or TSSA-DW version  
- Appropriate privileges (including truncate / session settings used during load, as documented for your environment)  

---

### Getting started

1. Copy the example config and set source type, schemas, and credentials.  
2. Optionally set `exclude_tables` for objects you do not want migrated or sequence-synced.  
3. Use a separate column-mapping file only when names differ (start empty for Oracle when names already match).  
4. Run `--list-tables`, then a single-table test, then full migration.  
5. Sync sequences and review the summary report.

*(Add internal link / package location / support contact here.)*

---

### Feedback

We welcome feedback from teams running TSSA and TSSA-DW migrations to PostgreSQL—especially around large-table performance, edge-case column mappings, and cutover checklists. Please comment on this post or reach out through your usual BMC / project channels.

---

### Disclaimer (recommended)

This utility is intended to assist with **data migration** into an existing PostgreSQL schema. Always validate in a non-production environment first. Follow your organization’s change-control and backup procedures before production cutover. Feature availability and supportability for PostgreSQL with TrueSight Server Automation should be confirmed against official BMC documentation for your product version.

---

## Optional shorter LinkedIn / internal Slack version

**Postgres Data Migration Utility for TSSA / TSSA-DW**

We’ve published a utility to migrate TrueSight Server Automation (and DW) data from **Oracle or SQL Server** to **PostgreSQL**.

Highlights:
- Auto-discovers matching tables  
- Bulk CSV + COPY load with truncate / FK-safe options  
- Oracle sequence sync and SQL Server IDENTITY → PG sequence sync  
- `exclude_tables` applies to data **and** sequence sync  
- Per-run reports for tables, sequences, and failures  

Ideal for dry-runs and cutover weekends. Details in the Helix Community post: [link].

---

## Suggested tags / topics

`TrueSight Server Automation` · `TSSA` · `PostgreSQL` · `Oracle` · `SQL Server` · `Migration` · `Database` · `TSSA-DW`

---

## Checklist before you publish

- [ ] Confirm whether this is internal-only or Helix Community–public  
- [ ] Add “how to obtain” (Git, share path, engagement team)  
- [ ] Add supported TSSA / PostgreSQL versions you have tested  
- [ ] Add author name and contact  
- [ ] Attach a simple architecture diagram (optional): Source DB → Utility → PostgreSQL + reports  
- [ ] Remove or soften any customer-specific hostnames from examples  
