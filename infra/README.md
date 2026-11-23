# Deploy to Azure: two separate services

DataTrust AI and the POC data pipeline are different systems, so they are deployed separately: each has its own
resource group, container registry, Key Vault, managed identity, database and login. Either one can be updated or
deleted without touching the other.

What this subscription allows shapes two shared pieces:

* **One database server.** Azure no longer creates new PostgreSQL servers for this subscription (B1ms and B2s were refused
  in Southeast Asia and Central India), so both databases live on the existing Sales DB server `pg-sales-hv`, next to
  `salesdb`: `datatrust_poc` owned by login `poc_pipeline`, and `datatrust_app` owned by login `datatrust_owner`. Each
  database accepts connections only from its own login (and the server admin), so neither service can read the other's.
* **One Container Apps environment.** The subscription allows only one, so DataTrust's web app runs on the POC service's
  environment `dtpoc-env` (and in its region, Southeast Asia); its container logs go to that environment's workspace.

`-NewServer` and `-OwnEnvironment` create separate ones instead, once the subscription allows it.

**Deployed (23 Nov 2026):** https://datatrust-web.gentlehill-adc60b94.southeastasia.azurecontainerapps.io

```
 rg-datatrust-poc   POC data pipeline (the "customer" system)            rg-datatrust   DataTrust AI (the product)
 ─────────────────────────────────────────────                          ─────────────────────────────────────────
 ADLS dtpoclake… landing/raw/<batch>/_READY                              Browser ──HTTPS──► Container App datatrust-web
        │ Event Grid ──► queue adls-ready                                                    (1 replica: web + ADF collector
        ▼                                                                                     + checks runner + alerts)
 Container Apps Job dtpoc-pipeline  (starts on upload, PySpark ETL)                                 │
        │                                                                                           ▼
        ▼                                                                pg-sales-hv ─ datatrust_app (own login)
 pg-sales-hv ─ datatrust_poc (+ salesdb) ◄────────── monitored source ───   (logins, connections, rules, incidents)
   bronze / silver / gold / audit                    (reads; fixes only        │
                                                      after admin approval)    └─ also monitors Sales DB, ADF, …
```

| | POC data pipeline | DataTrust AI |
|---|---|---|
| Script | `infra/poc/deploy.ps1` | `infra/datatrust/deploy.ps1` |
| Resource group | `rg-datatrust-poc` | `rg-datatrust` |
| Database | `datatrust_poc` on `pg-sales-hv`, login `poc_pipeline` | `datatrust_app` on `pg-sales-hv`, login `datatrust_owner` |
| Landing files | its own ADLS Gen2 account `dtpoclake…`, container `landing` | none |
| Compute | Container Apps **Job** that starts when a batch is uploaded, billed only while it runs | Container App, always on, exactly 1 replica, on the POC's environment |
| Image | `datatrust-pipeline` (`Dockerfile.pipeline`) | `datatrust-web` (`Dockerfile`) |
| Knows about the other | nothing | the POC database's host, login and password, like any monitored source |

`infra/common.ps1` (helpers) and `infra/copy-database.ps1` (one-off data copy) are shared tooling only.

## Before you run them

| You need | Check |
|---|---|
| Azure CLI | `az version` |
| Signed in to the right subscription | `az login`, then `az account show` |
| Rights to create resource groups and assign roles in them | Owner, or Contributor + User Access Administrator |
| PostgreSQL 17 client tools, for the one-off data copy | `C:\Program Files\PostgreSQL\17\bin\pg_dump.exe` |
| The local PostgreSQL running, with `.env` pointing at it | the local site works |
| For the pipeline job: ADLS settings in `.env` | `ADLS_CONTAINER`, `ADLS_FOLDER`; and `ADLS_ACCOUNT_NAME` with `ADLS_ACCOUNT_KEY` for the one-off copy of the landing files (or the account itself, with `-UseEnvAdls`) |

Docker is not needed when Azure allows cloud builds (`az acr build`). This subscription does not, so the scripts fall back to
the local Docker engine: start Docker Desktop or Rancher Desktop before running them.

## Run, in this order

From the repository root, in PowerShell:

```powershell
az login
.\infra\poc\deploy.ps1          # 1. the POC data pipeline (about 15 minutes)
.\infra\datatrust\deploy.ps1    # 2. DataTrust AI, which then monitors the POC database (about 15 minutes)
```

Each script shows the subscription and what it will create, and asks before creating anything.
DataTrust goes second because it reads the POC database's address and password from the POC service
(the deployment outputs and the POC Key Vault) and keeps its own copy of that password in its own Key Vault.

Later updates, data untouched:

```powershell
.\infra\poc\deploy.ps1 -SkipDataCopy          # new pipeline code
.\infra\datatrust\deploy.ps1 -SkipDataCopy    # new DataTrust code or settings
```

### POC data pipeline options

| Option | Default | What it does |
|---|---|---|
| `-ResourceGroup` | `rg-datatrust-poc` | where the POC service goes |
| `-Location` | `southeastasia` | Azure region, next to the Sales DB |
| `-SharedServer`, `-SharedServerResourceGroup` | `pg-sales-hv`, `datatrust_ai` | the existing server that gets `datatrust_poc` |
| `-SharedServerAdminPasswordEnv` | `CONN_SALESDB_PASSWORD` | the `.env` name holding that server's admin password, used once to create the database and login (or `$env:SHARED_PG_ADMIN_PASSWORD`) |
| `-PocLogin` | `poc_pipeline` | the POC's own login; it owns `datatrust_poc` |
| `-NewServer` | off | create a separate PostgreSQL server for the POC instead (size `-PgSku`, default `Standard_B2s`) |
| `-Trigger` | `event` | `event`: the job starts within about a minute of a `_READY` upload. `schedule`: it runs every `-Schedule` |
| `-SafetyNet` | `0 2 * * *` | event mode: a daily catch-up run (job `dtpoc-pipeline-daily`) in case an upload event was lost; `""` = none |
| `-Schedule` | `*/15 * * * *` | schedule mode: how often the job looks for new batches (cron, UTC). Each run costs about 35 s of 2 vCPU, even with nothing to do |
| `-UseEnvAdls` | off | read the ADLS account in `.env` instead of creating one (no upload trigger, so schedule mode) |
| `-SkipLandingCopy` | off | don't copy the landing files from the `.env` account into the new one |
| `-NoPipelineJob` | off | only the database, no ADLS job |
| `-SkipDataCopy` | off | don't copy the local `datatrust_poc` |
| `-Yes` | off | don't ask for confirmation |

### DataTrust AI options

| Option | Default | What it does |
|---|---|---|
| `-ResourceGroup` | `rg-datatrust` | where the DataTrust service goes |
| `-Location` | the shared environment's region | Azure region; with the shared environment it is that environment's (Southeast Asia) |
| `-SharedEnvironment` | `dtpoc-env` | the Container Apps environment to run on; `-OwnEnvironment` creates one instead |
| `-SharedServer`, `-AppLogin` | `pg-sales-hv`, `datatrust_owner` | where `datatrust_app` goes and its login; `-NewServer` creates a server instead (size `-PgSku`) |
| `-PocResourceGroup` | `rg-datatrust-poc` | where to find the POC database to monitor |
| `-PocHost`, `-PocUser`, `-PocDatabase` | from the POC service | a POC database somewhere else (password in `$env:POC_PG_PASSWORD`) |
| `-EnableChatAlerts` | off | also post Google Chat alerts and the daily briefing from Azure. Off by default so the cloud copy and your local server don't both post to the same space |
| `-WithSqlServerDriver` | off | build the image with Microsoft's ODBC driver, for SQL Server sources |
| `-SkipDataCopy` | off | don't copy the local `datatrust_app` |
| `-Yes` | off | don't ask for confirmation |

## What each creates

| Resource | POC data pipeline | DataTrust AI |
|---|---|---|
| PostgreSQL | database `datatrust_poc` and login `poc_pipeline` on the existing `pg-sales-hv` (plus a firewall rule `datatrust-poc-deployer` for the data copy from your PC) | database `datatrust_app` and login `datatrust_owner` on `pg-sales-hv` (firewall rule `datatrust-app-deployer` for the data copy) |
| Container Registry | Basic | Basic |
| Storage | ADLS Gen2 `dtpoclake…` (Standard LRS): container `landing`, queue `adls-ready`, Event Grid topic `dtpoc-lake-events` sending `_READY` uploads to the queue | none |
| Key Vault (RBAC) | `PG-PASSWORD`, `ADLS-ACCOUNT-KEY`, `ADLS-QUEUE-CONNECTION` (written by the template, straight from the storage account) | `APP-PG-PASSWORD`, `PG-PASSWORD` (the POC database), `PORTAL-SECRET`, API keys, webhooks |
| Managed identity | pulls the image, reads its Key Vault (and ADLS, when no key is given) | pulls the image, reads its Key Vault |
| Container Apps environment + Log Analytics (30 days) | yes | yes |
| Compute | jobs `dtpoc-pipeline` (on upload) and `dtpoc-pipeline-daily` (catch-up): 2 vCPU, 4 GB, only while running | app `datatrust-web`: 1 vCPU, 2 GB, always on |

Rough cost while both exist: about USD 55–75 a month: the always-on web app (1 vCPU, 2 GB), two Basic registries, Key Vaults and logs. The pipeline job costs about a third of a US cent per run and runs only when a batch arrives (plus once a day); the landing storage, queue and Event Grid cost cents a month. The Sales DB server was already paid for.
Remove a service with `az group delete -n rg-datatrust` or `az group delete -n rg-datatrust-poc`.

## Data copy (first run only)

* **POC:** the local `datatrust_poc` is copied **without** DataTrust's own tables that an older version kept there
  (`portal`, `telemetry`, `app`, `monitor`, `audit.remediation_log`, `audit.alert_log`). What arrives is the pipeline's data:
  `bronze`, `silver`, `gold`, `audit`, and `dt_audit` (DataTrust's change log on the monitored database).
* **DataTrust:** the local `datatrust_app` (logins, connections, rules, ADF history, incidents, decisions) is copied, and its
  POC connection is pointed at the POC service's server.

## Settings and secrets

**DataTrust** reads your `.env`:

* **Copied as plain settings:** everything that is not a secret, such as `LLM_PROVIDER`, `GEMINI_MODEL`, `ALERT_MIN_SEVERITY` and `DATATRUST_AS_OF`.
* **Copied into its Key Vault:** names ending in `PASSWORD`, `SECRET`, `_KEY`, `WEBHOOK_URL`, `TOKEN` or `CONNECTION_STRING`,
  for example `GEMINI_API_KEY`, `ADF_CLIENT_SECRET` and `CONN_SALESDB_PASSWORD`. Values go to Key Vault from a temporary file,
  never on a command line or into the deployment template.
* **Set for Azure instead:** `APP_PG_*` (its own server), `PG_*` (the monitored POC database), `PGSSLMODE=require`,
  `PORTAL_COOKIE_SECURE=true`, `PORTAL_AUTH_ENABLED=true` and `DATATRUST_PUBLIC_URL`. `PORTAL_SECRET` is reused from `.env`,
  or generated once and kept.
* **Not copied:** the pipeline's settings (`ADLS_*`, `SOURCE_MODE`, `WRITE_MODE`), local-only values (Kafka, `OLLAMA_URL`,
  ports), and the Google Chat webhook unless you pass `-EnableChatAlerts`.

**POC pipeline** takes only `ADLS_CONTAINER`, `ADLS_FOLDER` and `WRITE_MODE`; its landing account, key and queue come from
its own storage account. The `.env` account (`ADLS_ACCOUNT_NAME` + key) is only read once, to copy the landing files across
(`infra/copy-landing.py`; files already there are skipped, `_READY` markers go last). Batches the pipeline already ran are
not run again.

With `-UseEnvAdls` it takes `ADLS_ACCOUNT_NAME` and `ADLS_ACCOUNT_KEY` or `ADLS_CONNECTION_STRING` (into its Key Vault)
instead. With neither key nor connection string, the job signs in to ADLS with its managed identity; the script grants it
*Storage Blob Data Contributor* on the account when it can, and says so when it can't.

**Retry from DataTrust:** when the POC job starts on uploads, DataTrust's deploy script sets `PIPELINE_TRIGGER_QUEUE_URL` and
gives DataTrust's identity *Storage Queue Data Message Sender* on the POC queue `adls-ready` only (no key). Pressing
**Retry failed batch** then adds a message there and the job starts within about a minute. Deploy the POC first, so
DataTrust finds the queue.

**Sending a batch to Azure:** upload the CSV files to `<landing account>/landing/raw/<full or YYYY-MM-DD>/`, then an empty
`_READY` file last. The job starts within about a minute. Uploading into a folder that already ran does nothing; use a new
folder name.

## After deploying

1. **Open the address DataTrust printed.** Sign in with your existing account: the user table was copied.
2. **Run the pipeline once** to check it: `az containerapp job start -n dtpoc-pipeline -g rg-datatrust-poc`, then
   `az containerapp job execution list -n dtpoc-pipeline -g rg-datatrust-poc -o table`.
3. **Other monitored databases:** allow DataTrust's printed outbound IPs in their firewalls (for example the Sales DB server).
   The POC server already accepts Azure services.
4. **SQL Server on this PC** (`DESKTOP-JN0VBK5`) can't be reached from Azure. That connection shows an error in the cloud
   copy until the server is reachable (and the image is built with `-WithSqlServerDriver`).
5. **Google sign-in, if used:** add `<address>/api/auth/google/callback` as a redirect URI in Google Cloud.
6. **The Gemini free tier** (about 20 requests a day) is shared by every copy that uses the same key.
7. **The schema-change guard** needs event triggers, which Azure's managed PostgreSQL may refuse. The POC data copy
   reports them if they were skipped. Everything else works without them.

## Limits of this setup

This setup is for a demo and a pilot. It is not production-hardened:

* Both PostgreSQL servers accept connections from any Azure service (firewall rule `0.0.0.0`) and from your IP.
  For production, use private networking, and let the monitored servers accept only DataTrust's outbound IPs.
* DataTrust connects to the POC database with the POC's own login (`poc_pipeline`, which owns `datatrust_poc`), because it
  also applies approved fixes there. For production, create a dedicated DataTrust login with only the rights it needs.
  Approved access requests that create new logins need CREATEROLE, which `poc_pipeline` does not have.
* The site's own sign-in protects the Data Estate pages. To protect every page, also turn on Container Apps
  authentication (Microsoft Entra ID) or IP restrictions.

## Troubleshooting

| Symptom | Look at |
|---|---|
| `/health` never answers | `az containerapp logs show -n datatrust-web -g rg-datatrust --follow` |
| A pipeline run failed | `az containerapp job execution list -n dtpoc-pipeline -g rg-datatrust-poc -o table`, then its logs in Log Analytics `dtpoc-logs` |
| "No POC service found" from the DataTrust script | deploy the POC first, or pass `-PocHost` / `-PocUser` with `$env:POC_PG_PASSWORD` |
| Data copy blocked | your IP changed: run again, or add it under PostgreSQL > Networking |
| `Could not write ... to Key Vault` | the role assignment can take a few minutes; run again |
| Image build fails | the `az acr build` log above the error; the web build imports every module, so a missing package shows there |
