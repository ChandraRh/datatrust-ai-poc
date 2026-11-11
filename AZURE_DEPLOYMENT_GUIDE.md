# DataTrust AI: Azure deployment guide

This guide deploys the website and the ADLS pipeline worker as two separate Azure services. The worker watches ADLS for a ready batch, runs the existing PySpark ETL, and writes the results to PostgreSQL. The website reads those results.

## Quick overview

The deployment uses:

1. **Azure Container Registry (ACR)** to store its Docker image.
2. **Azure App Service (Linux, custom container)** to run the web app.
3. **Azure Container Apps** to run the always-on pipeline worker.
4. **Azure Database for PostgreSQL Flexible Server** to store source data, results, portal data, and ETL logs.
5. **Azure Storage with ADLS Gen2** for the incoming CSV batches.
6. A resource group and subscription where you can create or use these resources.

Both services use the same PostgreSQL database. The database must be set up before the first pipeline run.

```text
Browser --> Azure App Service (website) ------------------> Azure PostgreSQL
                    ^                                             ^
                    |                                             |
              ACR web image                Azure Container Apps (pipeline worker)
                                                  ^         |
                                                  |         +--> Azure PostgreSQL
                                             ADLS Gen2 CSV batches
```

These are separate services: App Service serves the website, while the Container App worker runs ETL. The worker checks ADLS every 30 seconds by default. Upload all CSVs for a batch first, then upload an empty `_READY` marker; the worker starts the batch after it sees that marker. Batch lifecycle and errors are stored in PostgreSQL and shown in the website's Pipeline log. This uses polling, not an Event Grid trigger.

## Azure resources: what is needed?

| Resource | Required? | Simple explanation |
|---|---|---|
| Azure Container Registry (ACR) | Yes | Holds the web and pipeline-worker Docker images. |
| Azure App Service, Linux custom container | Yes | Runs the website. |
| Azure Container Apps environment and app | Yes | Runs the ADLS pipeline worker continuously. Keep it at one replica. |
| Azure Database for PostgreSQL Flexible Server | Yes | Stores DataTrust data, portal accounts, pipeline status, and errors. |
| Azure Storage with ADLS Gen2 | Yes | Stores CSV batches for the pipeline worker. |
| Azure subscription and resource group | Yes | Azure account and project container used to create resources. |
| Azure Key Vault | Recommended | Securely stores passwords, API keys, and signing secrets. |
| Azure Log Analytics workspace | Optional/recommended | Central place to collect and search Azure app logs. Configure App Service diagnostic settings to send logs there. |
| pgvector extension | No | The current app does not use vector embeddings or similarity search. |
| Azure OpenAI Service | No, not in the current app | The current app does not have an Azure OpenAI integration. Its optional AI providers are configured in Step 5. |
| Azure Service Bus | No | The ADLS worker does not use Service Bus. |

You will also need:

- Network connectivity from both services to PostgreSQL. Prefer private networking for production.
- Permission for both services to pull images from ACR.
- Storage Blob Data Reader for the worker identity on the ADLS container (batch status is stored in PostgreSQL).
- Storage Blob Data Contributor for the identity or user that runs `upload_to_adls.py` or uploads CSVs manually; the pipeline worker does not need write access to ADLS.
- Key Vault access for identities that use Key Vault references.
- A DNS and TLS plan if using a custom domain. App Service provides HTTPS for its default hostname.

Choose resource sizes based on expected usage and budget. Enable PostgreSQL backups and set an appropriate retention period.

## Before you start

Ask your Azure administrator to provide:

- Azure subscription and resource group names.
- An approved Azure region.
- A PostgreSQL server, database, and dedicated application login, or permission to create them.
- An ACR and permission to push images.
- An App Service plan and permission to create a Linux custom-container app.
- An Azure Container Apps environment and permission to create a Container App.
- Secure network paths from both services to PostgreSQL.
- Permission to grant the worker's managed identity access to ADLS.
- Key Vault access if you will store secrets there.

Install Azure CLI. Install Docker too if you plan to build the image on your computer. The commands below use PowerShell.

## Step 1: Create and populate PostgreSQL

1. Create an Azure Database for PostgreSQL Flexible Server.
2. Select a PostgreSQL version supported by Azure and compatible with the project's data/backup. Check compatibility before restoring; the project deployment notes have targeted PostgreSQL 17.
3. Create a database, for example `datatrust_poc`.
4. Create dedicated logins for the website and pipeline worker. Do not use the PostgreSQL server administrator as an application login.
5. Run `python run_pipeline.py --setup` using a deployment/migration login before starting the services. Grant each application login only the required database/schema permissions:
   - Website: read access to `bronze`, `silver`, `gold`, and `audit`; write access to the portal and approval tables; and `SELECT`/`UPDATE` on `audit.adls_batch_runs`. The web app initializes portal and feature tables at startup, so pre-create the `portal` schema and grant the web login `USAGE` and `CREATE` on `portal` and `audit` (or use an approved migration process that supplies these tables).
   - Pipeline worker: permission to create/update `bronze`, `silver`, `gold`, and `audit` objects and write pipeline status, quality, error, and batch lifecycle records.
6. Load the project's data by restoring an available database backup or running the separate data pipeline.

The repository mentions a `datatrust_backup.backup` file. Confirm that your team has the file before planning to restore it.

Restrict PostgreSQL network access to both Azure services or their VNet. Do not open it to the whole internet. Both services use `PGSSLMODE=require` for encrypted database connections.

## Step 2: Build and push the web image to ACR

Sign in to Azure and select the correct subscription:

```powershell
az login
az account set --subscription "<subscription-id-or-name>"
```

Create a resource group and ACR if your team has not already created them. Resource names and region must follow your organization's rules. The ACR name must be globally unique.

```powershell
az group create --name "<resource-group>" --location "<azure-region>"
az acr create --resource-group "<resource-group>" --name "<unique-acr-name>" --sku Basic
```

From the repository root, build the image in ACR (Docker is not required on your computer):

```powershell
az acr build --registry "<unique-acr-name>" --image datatrust-web:v1 .
```

Build a separate image for the PySpark pipeline worker:

```powershell
az acr build --registry "<unique-acr-name>" --file Dockerfile.pipeline --image datatrust-pipeline:v1 .
```

Or, if Docker is installed locally, build and push the image yourself:

```powershell
az acr login --name "<unique-acr-name>"
docker build -t "<unique-acr-name>.azurecr.io/datatrust-web:v1" .
docker push "<unique-acr-name>.azurecr.io/datatrust-web:v1"
```

```powershell
docker build -f Dockerfile.pipeline -t "<unique-acr-name>.azurecr.io/datatrust-pipeline:v1" .
docker push "<unique-acr-name>.azurecr.io/datatrust-pipeline:v1"
```

Use a new image tag (for example, `v2`) when publishing a later version. Keep the prior tag available in case you need to roll back.

The web image contains only the web app. The separate pipeline image contains PySpark, Java, and the ETL code. Neither image contains `.env`, source data, or credentials.

**Optional SQL Server/Azure SQL connector:** the default image does not include Microsoft ODBC drivers. If you need this connector, build a separate image:

```powershell
az acr build --registry "<unique-acr-name>" --image datatrust-web:v1-mssql --build-arg WITH_MSSQL=true .
```

The SQL Server source must also be reachable from Azure. This connector is separate from the primary PostgreSQL database.

## Step 3: Create the App Service

1. In Azure, create an **App Service** using **Linux** and a **custom container**.
2. Select the image you pushed to ACR, such as `datatrust-web:v1`.
3. Enable the App Service managed identity.
4. Give that identity permission to pull images from ACR (commonly the `AcrPull` role).
5. Configure networking so App Service can connect to PostgreSQL.
6. If using Key Vault references, give the App Service identity permission to read secrets. The common role is **Key Vault Secrets User**.
7. Add the App Service settings from Step 5, then save and restart the app.

Azure portal wording can change. Look under **App Service > Settings > Environment variables** (sometimes shown as **Configuration**) and add each setting as a separate name/value pair.

## Step 4: Understand `.env` and Azure settings

- [`.env.example`](./.env.example) is a template listing settings used by the project.
- `.env` is for running the project on your computer.
- Azure App Service does **not** automatically read your computer's `.env` file. Do not add `.env` to the Docker image or source control.
- In Azure, create App Service environment variables only for settings the **web app** needs. Use `.env.example` to find setting names, but enter Azure values—not local values such as `localhost`.
- Put passwords, API keys, and signing secrets in Key Vault and reference them from App Service.
- Pipeline settings such as ADLS, Spark, and Kafka are configured on the separate pipeline/worker, not on this web App Service. See Step 7.

## Step 5: Add web app settings in App Service

Open **App Service > Settings > Environment variables** and add the settings below. Use the `+ Add` option (or equivalent), enter one name/value pair, and repeat. Add the database values for your Azure PostgreSQL server; do not copy local database values from `.env`.

### Required: web server and database

| Setting name | Value to enter |
|---|---|
| `WEBSITES_PORT` | `8000` |
| `WEB_HOST` | `0.0.0.0` |
| `WEB_PORT` | `8000` |
| `PG_HOST` | Azure PostgreSQL server hostname only (no `https://`) |
| `PG_PORT` | `5432` |
| `PG_DATABASE` | Database name created in Step 1 |
| `PG_USER` | Dedicated DataTrust database login |
| `PG_PASSWORD` | Database password; preferably a Key Vault reference |
| `PGSSLMODE` | `require` |

### Required: Data Estate login

These settings keep sign-in enabled for the Data Estate sections (Connections, Catalog, Activity, Access, and their APIs). Set the bootstrap password to a unique, strong value before the first start; store it as a Key Vault secret.

| Setting name | Value to enter |
|---|---|
| `PORTAL_AUTH_ENABLED` | `true` |
| `PORTAL_ADMIN_USER` | `admin` for the current POC setup |
| `PORTAL_ADMIN_EMAIL` | The admin's real email address |
| `PORTAL_ADMIN_PASSWORD` | A unique, strong bootstrap password; store as a Key Vault secret |
| `PORTAL_SECRET` | A long random secret stored in Key Vault; keep it unchanged across restarts and instances |
| `PORTAL_COOKIE_SECURE` | `true` because the Azure site uses HTTPS |

For a **new/empty portal user table**, the first admin account is created from these settings. The configured admin email is marked verified when the account is first created. If no bootstrap password is configured, the app generates one and prints it once to its startup logs; configure the Key Vault secret first to avoid putting a credential in logs.

The account is created only when the portal has no users. If the Azure database already contains portal accounts, changing `PORTAL_ADMIN_PASSWORD` will **not** reset an existing password. Use the existing account or its password-reset process.

To generate a random value for `PORTAL_SECRET`, run locally:

```powershell
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

Save the output in Key Vault; do not paste it in chat, tickets, or source code.

### Optional: email sign-up and password recovery

Add these only if users need email verification or password-reset emails:

| Setting name | Example/value |
|---|---|
| `DATATRUST_PUBLIC_URL` | `https://<app-name>.azurewebsites.net` |
| `SMTP_HOST` | `smtp.gmail.com` for Gmail |
| `SMTP_PORT` | `587` |
| `SMTP_USER` | Gmail sender account |
| `SMTP_PASSWORD` | Gmail App Password in Key Vault (requires 2-Step Verification) |
| `SMTP_FROM` | Sender address authorized for that Gmail account |

Without SMTP, the app can start and the bootstrap account can sign in, but email sign-up/verification and password recovery cannot send messages.

### Optional: Google sign-in

Google sign-in requires a Google OAuth 2.0 **Web application** client. Add the Azure URL as an authorized redirect URI:

```text
https://<app-name>.azurewebsites.net/api/auth/google/callback
```

Then add these settings:

| Setting name | Value |
|---|---|
| `GOOGLE_CLIENT_ID` | Google OAuth client ID |
| `GOOGLE_CLIENT_SECRET` | Google OAuth client secret; preferably a Key Vault reference |
| `PORTAL_ADMIN_EMAILS` | Optional comma-separated list of email addresses that should receive the admin role |

If using a custom domain, register its callback URL instead. Google sign-in will not be available unless the client ID and secret are configured.

### Other optional web app settings

Only add these if you use the corresponding feature:

| Setting name | When to use it |
|---|---|
| `LLM_PROVIDER` | `none` (default), `gemini`, `anthropic`, or `ollama`. Set only the provider you intend to use. |
| `GEMINI_API_KEY`, `GEMINI_MODEL` | Gemini provider; keep the API key in Key Vault. Model default: `gemini-2.5-flash`. |
| `ANTHROPIC_API_KEY` | Anthropic provider; keep the key in Key Vault. |
| `OLLAMA_URL` | Ollama provider only. It must be reachable from Azure; `localhost` means the App Service container, not your computer. |
| `GOOGLE_CHAT_WEBHOOK_URL` | Google Chat alerts; keep the webhook URL in Key Vault. |
| `ALERT_MIN_SEVERITY` | Optional alert threshold; default is `MEDIUM`. |
| `ALERT_INTERVAL_SECONDS` | Optional alert polling interval; default is `60`. |
| `DATATRUST_PUBLIC_URL` | Public HTTPS URL used in email and alert links. |
| `CONN_<NAME>_PASSWORD` | Password for an extra source configured in the Data Estate portal; keep it in Key Vault. |
| `DATATRUST_AS_OF` | Optional date behavior override; see project configuration. |
| `WEB_LOG_LEVEL` | Set to `info` when you need more request logs. |

For email and alert links, set `DATATRUST_PUBLIC_URL` to the public HTTPS address. Allow outbound HTTPS to Google Chat if using its alerts.

## Step 6: Protect the website

The main dashboard and dashboard APIs currently open without an application sign-in. `PORTAL_AUTH_ENABLED=true` protects only the Data Estate sections and their APIs; it does not protect the whole site.

Before sharing the site publicly, use an approved Azure-level access control, such as:

- App Service Authentication with Microsoft Entra ID, configured to require sign-in; or
- App Service access restrictions or private networking so only approved users/network locations can reach it.

The Data Estate login is a POC login, not a replacement for company-wide single sign-on. Use a unique bootstrap password and protect the whole site with Azure-level access control.

## Step 7: Create the ADLS pipeline worker

The pipeline worker is a separate, continuously running Azure Container App. It watches the ADLS container every 30 seconds, by default. It runs ETL only after it finds a `_READY` marker. This keeps incomplete uploads from being processed.

1. Create an **Azure Container Apps environment** in the approved region.
2. Create a Container App from the image `datatrust-pipeline:v1`.
3. Disable ingress; the worker does not serve web traffic.
4. Enable its system-assigned managed identity.
5. Set the minimum and maximum replica counts to `1`. Do not scale this worker to multiple replicas; two workers could process the same batch simultaneously.
6. Configure its environment variables:

| Variable | Value |
|---|---|
| `SOURCE_MODE` | `adls` |
| `ADLS_ACCOUNT_NAME` | Azure Storage account name |
| `ADLS_CONTAINER` | ADLS Gen2 container/filesystem name |
| `ADLS_FOLDER` | `batches` (the root folder the worker watches) |
| `ADLS_ACCOUNT_KEY` | Leave empty when using managed identity |
| `ADLS_CONNECTION_STRING` | Leave empty when using managed identity |
| `ADLS_WORKER_POLL_SECONDS` | `30`, or another interval in seconds |
| `PG_HOST` | Azure PostgreSQL server hostname |
| `PG_PORT` | `5432` |
| `PG_DATABASE` | DataTrust database name |
| `PG_USER` | Dedicated pipeline database login |
| `PG_PASSWORD` | Pipeline database password; use a Key Vault reference |
| `PGSSLMODE` | `require` |
| `WRITE_MODE` | `python` (project default) |

7. Grant the Container App identity **Storage Blob Data Reader** on the ADLS container. The worker reads source CSVs and `_READY`; it stores processing status and errors in PostgreSQL. Separately, grant **Storage Blob Data Contributor** to the user or identity that uploads CSVs.
8. Grant the worker identity **AcrPull** on ACR. If using Key Vault references, grant it **Key Vault Secrets User** on the vault.
9. Allow the worker network access to PostgreSQL and ADLS. Store database passwords in Key Vault.

For private ACR access, set the Container App's registry authentication to use its managed identity. If the image cannot be pulled during initial creation, first create a user-assigned managed identity, grant it `AcrPull` and the ADLS role, then select that identity for both image-pull and runtime access when creating the Container App.

The worker image has Java 17 and the ETL dependencies. It runs `adls_pipeline_worker.py`; do not use the website image for this service.

### Prepare PostgreSQL once

Before the worker's first data batch, create the Bronze/Silver/Gold and audit structures. From a machine with the project requirements installed and network access to Azure PostgreSQL, set the Azure `PG_*` environment values and run:

```powershell
python run_pipeline.py --setup
```

Run this once for a new database, and rerun it after updates that add SQL structures. It is safe to rerun and creates `audit.adls_batch_runs` for the batch-status page. The web app's own startup setup does not create all ETL schemas and views.

### ADLS folder and batch format

The worker expects all configured source CSVs in a batch folder. The required filenames are listed in `config.py`.

```text
<container>/
  batches/
    full/
      payment_orders.csv
      payments.csv
      refunds.csv
      settlements.csv
      customers.csv
      products.csv
      categories.csv
      _READY
    2026-11-09/
      payment_orders.csv
      payments.csv
      refunds.csv
      settlements.csv
      customers.csv
      products.csv
      categories.csv
      _READY
```

- Use `batches/full` for the first load containing all historical data. The worker runs `python run_pipeline.py --full`.
- Use one folder named with the business date (`YYYY-MM-DD`) for each later daily batch. The worker runs the pipeline for that date.
- Upload all seven CSVs first. Upload an empty `_READY` blob last. The worker checks for every expected CSV before it starts.
- `_READY` is the only batch marker. The worker does not write `_PROCESSED` or `_FAILED`; it records `READY`, `RUNNING`, `SUCCEEDED`, or `FAILED` in `audit.adls_batch_runs`.
- View batch statuses and failure details under **Pipeline log** in the website. An administrator can retry a failed batch there; the worker reuses the existing files and `_READY` marker.
- If you need to replace files for a failed batch, run `upload_to_adls.py` with the same batch name from a machine that has the project CSVs, ADLS access, and PostgreSQL access. The helper uploads the files and queues the retry. Do not replace files while a batch is `READY` or `RUNNING`.
- Pipeline status updates are stored in PostgreSQL. Azure Container App logs are still needed if the worker cannot connect to PostgreSQL before it can record the failure.
- Process batches in order. Complete the initial `full` load before uploading daily batches.

For local development, set `SOURCE_MODE=adls`, `ADLS_FOLDER=batches`, and the ADLS/PostgreSQL values in `.env`, then run:

```powershell
pip install -r requirements.txt
python run_pipeline.py --setup
python adls_pipeline_worker.py
```

The worker stays running and checks ADLS every 30 seconds. Upload a test batch using the helper below, or create the same folder structure in ADLS Storage Explorer. In a separate terminal, start the website with `python web\server.py`.

To upload local `data/source_csv` files and create the ready marker:

```powershell
python upload_to_adls.py full
python upload_to_adls.py 2026-11-09
```

Use the first command for an initial full-history batch and the second for a dated batch. The helper uploads `_READY` only after all required CSVs are uploaded.

## Optional: Kafka or Azure Event Hubs

Kafka streaming is a different, optional input path. It is not needed for ADLS batches. See [streaming/README.md](./streaming/README.md) if you separately deploy the Kafka consumer and producer.

## Step 8: Start and check the app

1. Save the App Service settings and restart the app.
2. Confirm the pipeline worker is running and its logs show that it is watching the configured ADLS container.
3. Open `https://<app-name>.azurewebsites.net/health`.
4. Expect HTTP `200` when the app can query PostgreSQL. HTTP `503` means the database is unavailable to the app.
5. Upload a complete test batch to ADLS, then upload `_READY` last. Within the polling interval, worker logs should show the batch starting.
6. Confirm the website's Pipeline log shows the batch status and PostgreSQL has the run details.
7. Open the dashboard and check that expected data appears. The website checks for updates about every 15 seconds.
8. Open a Data Estate section and sign in with the bootstrap credentials configured in App Service.
9. If something fails, check both App Service and worker logs, plus PostgreSQL connectivity and permissions.

The Docker image has a container health check. App Service's Health check feature is configured separately; if you use it, set its path to `/health` and confirm your App Service plan supports it.

## Step 9: Deploy an update

1. Build and push a new version of the web image and/or pipeline image, such as `v2`.
2. Update the matching service to use its new image tag.
3. Wait for the website to pass `/health` and for the worker logs to show it is watching ADLS.
4. Keep the previous image tags so you can roll back if needed.
5. Keep PostgreSQL backups separately. Deploying an app image does not back up the database.

## Security and operations checklist

- Use HTTPS and set `PORTAL_COOKIE_SECURE=true`.
- Keep PostgreSQL private or restricted to approved networks and require TLS.
- Store passwords, API keys, webhooks, and signing secrets in Key Vault or an approved secret store.
- Use managed identities for ACR, Key Vault, and ADLS access where supported.
- Keep the pipeline worker at one replica so two workers do not process the same ready batch at once.
- Keep database permissions limited to what each workload needs.
- Enable PostgreSQL backups and test restoring them.
- Send App Service logs to an approved monitoring destination such as Log Analytics if required.
- Set alerts for app restarts, failed health checks, database problems, and resource exhaustion.
- Protect the whole site with Azure-level access controls before broad or sensitive use.
- Review privacy, access, retention, and audit requirements before using this POC with regulated data or multiple customers.

## Current limits

- The app is designed around this project's PostgreSQL medallion pipeline.
- Spark and Kafka are separate workloads; App Service does not start them.
- The app is not a general Azure Data Factory, Snowflake, or Databricks telemetry collector.
- The portal login is a POC login, not enterprise SSO.
- Some schema-change guard features may depend on PostgreSQL capabilities restricted by managed database services.
- PostgreSQL and SQL Server/Azure SQL catalog capabilities vary. See [PRODUCT_ARCHITECTURE.md](./PRODUCT_ARCHITECTURE.md).

## Related files

- [`.env.example`](./.env.example) — list of project environment settings; includes local-development examples.
- [DEPLOY.md](./DEPLOY.md) — additional deployment and feature notes.
- [Dockerfile](./Dockerfile) — web container build configuration.
- [docker-compose.web.yml](./docker-compose.web.yml) — local web-plus-PostgreSQL setup; not the production Azure topology.
- [PRODUCT_ARCHITECTURE.md](./PRODUCT_ARCHITECTURE.md) — current capabilities and product limits.
