# Azure Data Factory connector

DataTrust reads your Azure Data Factory (ADF) pipelines and shows their runs, steps, errors and likely causes in the website. It also checks the source files and target tables those pipelines use.

The connector **only reads**. It never changes pipelines, triggers, linked services or data.

## What you get

| Page | What it shows |
|---|---|
| **ADF pipelines** | One row per pipeline: health, trust score (0–100), last run, duration and rows compared with usual, 30-day success rate, the last 20 runs, schedule, and open issues. Above the table: key figures and a chart of runs per day. |
| **ADF issues** | Open problems, most severe first. Each one has its cause, the system at fault (source storage, target PostgreSQL, data, Data Factory, network, credentials), the evidence, and numbered steps to fix it. Issues close by themselves when the next run no longer shows them. |
| **ADF runs** | Every run, filterable by pipeline, status and period, and searchable by run ID, error text or parameter value. Opening a run shows the details listed below. |
| **Connect ADF** | Add, test, edit, pause or remove a factory. It also shows sync health and history, and can set up instant updates. |

Opening a run shows:

- **Why it failed**: the root cause, followed through Execute Pipeline steps into child pipelines and into ForEach, If and Switch containers. You get the path (for example PL_Master › Run sales load › PL_Load_Sales › Copy customers), a plain-language explanation, the raw Azure error, the names it mentions (path, table, column, constraint, SQLSTATE), fix steps, and whether a re-run can help.
- **Steps over time**: a timeline of every step, including time spent queued.
- **Per step**: rows read, written and skipped, bytes, duration, data integration units (DIU), parallel copies, throughput, and error.
- **Source and target checks**, run by DataTrust itself (see [below](#source-and-target-checks)).
- **Data moved**: for each Copy step, the storage path it read and the PostgreSQL table it wrote, with parameters resolved for that run.
- Parameters, trigger, parent and child runs, re-runs, and a link to the run in ADF Monitor.

### Issue types

| Issue | When it is raised |
|---|---|
| RUN_FAILED | The newest finished run failed. It is CRITICAL after 3 failures in a row. |
| RUN_CANCELLED | The newest finished run was cancelled. |
| STALE | There has been no successful run for twice the trigger's interval plus 30 minutes. For a pipeline without a trigger, the limit is the factory's "max hours between successful runs" setting. |
| TRIGGER_STOPPED | A trigger for the pipeline exists but is stopped. |
| TRIGGER_FAILED | A trigger failed to start a run in the last 24 hours. |
| LONG_RUNNING | A run has been in progress for 3 times its usual duration, or 2 hours if there is no history. |
| ROWS_SKIPPED | The newest successful run skipped incompatible rows. When ADF logged them, DataTrust reads the first rows and reasons from that log. |
| VOLUME | The newest successful run loaded 0 rows, or less than 50% of the usual (median) number of rows. |
| SLOW | The newest successful run took more than twice the usual time, and at least 5 minutes longer. |
| CHECK_FAILED | A source or target check failed, or found a difference. |
| CONNECTOR | DataTrust cannot read the factory, for example because access was removed or a secret expired. |

The causes it recognises come from ADF error codes, connector codes such as `PathNotFound`, `UserErrorFileNotFound` or `DelimitedTextMoreColumnsThanDefined`, and PostgreSQL SQLSTATEs such as 23505, 23502, 22P02, 42P01, 42703 and 28P01. They include:

- a missing source file
- storage permission or firewall
- malformed CSV
- a type conversion problem
- a value too long for its column
- a missing column (schema drift)
- a missing table
- a duplicate key
- a NOT NULL or foreign key violation
- a PostgreSQL firewall, login, permission, SSL, capacity or timeout problem
- an integration runtime that is offline
- an expression error
- Key Vault access
- an activity timeout
- throttling
- a network error
- a data flow error
- a run that was cancelled, or failed through a Fail activity

The rules are data, in `web/adf/diagnose.py`.

## Setup

### 1. Choose an identity for DataTrust

You have two options.

**Option A: App registration (service principal)**

Use this for local development and most other cases.

1. In Entra ID, go to **App registrations → New registration**. Use any name, for example `datatrust-reader`.
2. In **Certificates & secrets**, create a new client secret.
3. Put the secret in `.env`:

   ```
   ADF_CLIENT_SECRET=<the secret value>
   ```

   In Azure, add it as an app setting or a Key Vault reference instead. DataTrust stores only the **name** of this variable, never the secret.

**Option B: Managed identity**

Use this when DataTrust runs in Azure (App Service or Container Apps). Turn on the system-assigned identity, then choose "Managed identity / az login" when you connect the factory. On a laptop, the same option uses your own `az login`.

### 2. Give the identity read access

**On the factory (required).** Use the built-in **Data Factory Contributor** role. It can also change the factory, but DataTrust never does. For least privilege, create this custom role instead and assign it at the factory scope:

```json
{
  "Name": "DataTrust ADF Reader",
  "Description": "Read ADF definitions and run history (DataTrust connector).",
  "Actions": [
    "Microsoft.DataFactory/factories/read",
    "Microsoft.DataFactory/factories/pipelines/read",
    "Microsoft.DataFactory/factories/datasets/read",
    "Microsoft.DataFactory/factories/linkedservices/read",
    "Microsoft.DataFactory/factories/triggers/read",
    "Microsoft.DataFactory/factories/pipelineruns/read",
    "Microsoft.DataFactory/factories/querypipelineruns/action",
    "Microsoft.DataFactory/factories/pipelineruns/queryactivityruns/action",
    "Microsoft.DataFactory/factories/querytriggerruns/action"
  ],
  "AssignableScopes": ["/subscriptions/<subscription-id>"]
}
```

```bash
az role definition create --role-definition datatrust-adf-reader.json
az role assignment create --assignee <client-id or managed identity principal id> \
  --role "DataTrust ADF Reader" \
  --scope /subscriptions/<sub>/resourceGroups/<rg>/providers/Microsoft.DataFactory/factories/<factory>
```

**On the source storage account (for file and row checks).** Assign the **Storage Blob Data Reader** role to the same identity.

There is a different option if the storage account uses a key: put a connection string in `.env`, for example `ADF_STORAGE_CONN=...`, and enter that variable name on the Connect ADF page. If the account is the one already configured in `ADLS_*`, those settings are used automatically.

**On the target PostgreSQL (for table and row checks).** Create a read-only login:

```sql
CREATE ROLE datatrust_reader LOGIN PASSWORD '...';
GRANT CONNECT ON DATABASE salesdb TO datatrust_reader;
GRANT USAGE ON SCHEMA public TO datatrust_reader;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO datatrust_reader;
```

Then add the database under **Data estate → Connections**. Use the **same host name** as in the ADF linked service, so the connector matches it automatically. You can also pick the connection explicitly on the Connect ADF page.

### 3. Connect the factory

Open **Connect ADF** and fill in:

- subscription ID
- resource group
- factory name
- tenant ID and client ID
- the name of the secret variable

Press **Test connection**. It checks each step in turn: sign-in, factory, pipelines, run history and activity runs. If a step fails, it says why.

Press **Connect**. The first sync reads the last 30 days of history (ADF keeps 45 days).

Alternatively, set these variables in `.env`. The factory is then added when the server starts:

```
ADF_SUBSCRIPTION_ID
ADF_RESOURCE_GROUP
ADF_FACTORY_NAME
ADF_TENANT_ID
ADF_CLIENT_ID
ADF_CLIENT_SECRET
```

### 4. Optional: instant updates

By default, each factory is read every `ADF_POLL_SECONDS` (300 seconds). For near-instant updates:

1. On the Connect ADF page, press **Instant updates…**. This creates a token and an ADF **Web activity** definition.
2. Add that Web activity as the last step of each pipeline, connected on **Completion**.

The Web activity only *asks* DataTrust to sync; the data itself is always read from Azure. Your DataTrust site must be reachable from Azure.

## How it works

```
Azure management API (read-only)            DataTrust
  pipelines, datasets, linked services ─┐
  queryPipelineRuns (incremental)       ├─► collector ──► PostgreSQL telemetry.* ──► /api/estate/adf/* ──► pages
  queryActivityRuns, queryTriggerRuns  ─┘      │
  ADLS / Blob (files, CSV rows, skip logs) ◄───┤ checks for the Copy steps of the newest runs
  target PostgreSQL (table, rows, columns) ◄───┘
```

**Collector** (`web/adf/collector.py`)

- Runs as a thread inside the web server. To run it as its own process instead, set `ADF_COLLECTOR_IN_WEB=false` and run `python web/adf/collector.py`.
- Each sync reads runs updated since the last sync, with 15 minutes of overlap so late updates are not missed.
- It re-reads runs that are still in progress.
- Every write is an upsert keyed on ADF's own IDs, so repeating a sync changes nothing.
- A PostgreSQL advisory lock ensures each factory has only one sync at a time, even across processes.

**Storage** (`telemetry` schema)

- Tables: `sources`, `definitions`, `pipeline_runs`, `task_runs`, `trigger_runs`, `checks` and `sync_log`.
- The tables are platform-neutral, so the next connector (Airflow, Databricks, …) can reuse them.

**Parameters**

Datasets and linked services that use expressions are resolved with the values of each run. Supported:

- `@pipeline().parameters.x`, `@dataset().x` and `@{...}` interpolation
- `concat`, `formatDateTime`, `utcNow`, `addDays`, `toUpper`, `replace` and similar functions

Values that are only known inside the run, such as `@item()` or `@activity(...)`, are shown as "depends on runtime values" rather than guessed.

**Secrets**

- The database stores only environment-variable **names**.
- ADF masks linked-service secrets, and DataTrust never needs them.
- The webhook token is stored as a SHA-256 hash and shown only once.

## Source and target checks

| Check | What it compares |
|---|---|
| Source files | The files the Copy step read are still present: count, size and last modified. A file changed after the run started is flagged, because the target may not hold its latest version. |
| Source row count | Data rows in the CSV files (up to `ADF_PROBE_MAX_MB`) against the `rowsRead` that ADF reported. |
| Skipped rows | The rows ADF skipped, plus the first entries of its skipped-rows log. |
| Target table | The table exists, and how many columns it has. |
| Target row count | Rows in the table now against the rows the newest successful load wrote. **Fewer** means rows were deleted, truncated or replaced after the load. |
| File vs table columns | The file header, or the explicit column mapping, against the table's columns. It reports columns that would be dropped, and required columns that are missing. |

## Limits (current version)

- **Supported activities.** Source and target checks cover ADLS Gen2 or Blob storage sources and PostgreSQL targets: Azure Database for PostgreSQL and the `PostgreSql` / `PostgreSqlV2` connectors. Other activity types (Data Flow, Lookup, Script, Stored Procedure, Web, ForEach, …) are collected with their status, errors and metrics, but have no source or target checks.
- **Data flows.** Row metrics come from the run output. DataTrust does not look inside the transformations.
- **Run history.** ADF keeps 45 days. DataTrust keeps what it collected, so history grows beyond 45 days from the day you connect.
- **Server name.** If a linked service stores its whole connection string as a secret, DataTrust cannot read the server name from it. Choose the target connection on the Connect ADF page instead.
- **Instant updates** need a public URL that Azure can reach. Polling works without one.
