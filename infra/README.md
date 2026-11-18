# Deploy DataTrust AI to Azure with one script

`infra/deploy.ps1` creates everything in one resource group and starts the web app on Azure Container Apps.
It copies the two local databases the first time, so the cloud copy opens with your data, rules, connections and accounts.

```
Browser ──HTTPS──► Container App "datatrust-web" (1 replica) ──► Azure PostgreSQL 17
                     │  web app + ADF collector + checks runner     ├─ datatrust_poc  (POC data)
                     │  + alerts, all in one process                └─ datatrust_app  (DataTrust's own data)
                     ├─ image from Azure Container Registry
                     ├─ secrets from Key Vault (managed identity)
                     └─ logs to Log Analytics
   (optional) Container App "datatrust-pipeline": ADLS batches → PySpark ETL → datatrust_poc
```

## Before you run it

| You need | Check |
|---|---|
| Azure CLI | `az version` (install: `winget install -e --id Microsoft.AzureCLI`) |
| Signed in to the right subscription | `az login`, then `az account show` |
| Rights to create a resource group and assign roles in it | Owner, or Contributor + User Access Administrator |
| PostgreSQL 17 client tools, for the one-off data copy | `C:\Program Files\PostgreSQL\17\bin\pg_dump.exe` |
| The local PostgreSQL running, with `.env` pointing at it | the local site works |

Docker is not needed: the image is built in Azure (`az acr build`).

## Run

From the repository root, in PowerShell:

```powershell
az login
.\infra\deploy.ps1
```

It shows the subscription and what it will create, and asks before creating anything. It takes about 15 minutes the first time.
At the end it prints the site's address (`https://datatrust-web.<random>.<region>.azurecontainerapps.io`) and the app's
outbound IP addresses.

To deploy a new version later (new image, same data):

```powershell
.\infra\deploy.ps1 -SkipDataCopy
```

| Option | Default | What it does |
|---|---|---|
| `-ResourceGroup` | `rg-datatrust-demo` | where everything goes |
| `-Location` | `centralindia` | Azure region |
| `-EnableChatAlerts` | off | also post Google Chat alerts and the daily briefing from Azure. Off by default so the cloud copy and your local server don't both post to the same space |
| `-WithPipelineWorker` | off | also run the ADLS pipeline worker (2 vCPU, 4 GB) |
| `-WithSqlServerDriver` | off | build the image with Microsoft's ODBC driver, for SQL Server sources |
| `-SkipDataCopy` | off | don't copy the local databases (use on every run after the first) |
| `-Yes` | off | don't ask for confirmation |

## What it creates

| Resource | Size | Why |
|---|---|---|
| Container Registry | Basic | holds the `datatrust-web` image |
| Key Vault | Standard, RBAC | every password, API key and webhook |
| Managed identity | user-assigned | pulls the image and reads Key Vault; no passwords for either |
| PostgreSQL Flexible Server 17 | Burstable B1ms, 32 GB, 7-day backups | `datatrust_poc` and `datatrust_app` |
| Container Apps environment + `datatrust-web` | 1 vCPU, 2 GB, exactly 1 replica | the web app, with HTTPS on its own address |
| Log Analytics workspace | 30 days | container logs |

The web app stays at **one replica** because the ADF collector, the checks runner and the alert loop run inside it.
Two replicas would run each of them twice.

Remove everything: `az group delete -n rg-datatrust-demo`.

## Settings and secrets

`deploy.ps1` reads your `.env`:

* **Copied as plain settings:** everything that is not a secret, such as `LLM_PROVIDER`, `GEMINI_MODEL`, `ALERT_MIN_SEVERITY` and `DATATRUST_AS_OF`.
* **Copied into Key Vault:** names ending in `PASSWORD`, `SECRET`, `_KEY`, `WEBHOOK_URL`, `TOKEN` or `CONNECTION_STRING`,
  for example `GEMINI_API_KEY`, `ADF_CLIENT_SECRET` and `CONN_SALESDB_PASSWORD`. The container app reads them through its
  identity. Values go to Key Vault from a temporary file, never on a command line or into the deployment template.
* **Set for Azure instead:** `PG_*` (the Azure server and a generated admin password), `PGSSLMODE=require`,
  `PORTAL_COOKIE_SECURE=true`, `PORTAL_AUTH_ENABLED=true` and `DATATRUST_PUBLIC_URL` (the app's address).
  `PORTAL_SECRET` is reused from `.env`, or generated once and kept.
* **Not copied:** local-only values (Kafka, `OLLAMA_URL`, ports, ADLS for the web app), and the Google Chat webhook unless
  you pass `-EnableChatAlerts`.

To change one setting later: change `.env`, then run `.\infra\deploy.ps1 -SkipDataCopy`.

## After the first deployment

1. **Open the address it printed.** Sign in with your existing account: the user table was copied.
2. **Monitored databases:** allow the printed outbound IPs in the firewall of each database DataTrust watches
   (for example the Sales DB server). Without that, its rules show "could not run".
3. **SQL Server on this PC** (`DESKTOP-JN0VBK5`) can't be reached from Azure. That connection shows an error in the cloud
   copy until the server is reachable from Azure (and the image is built with `-WithSqlServerDriver`).
4. **Google sign-in, if used:** add `<address>/api/auth/google/callback` as a redirect URI in Google Cloud.
5. **The Gemini free tier** (about 20 requests a day) is shared by every copy that uses the same key.
6. **The schema-change guard** needs event triggers, which Azure's managed PostgreSQL may refuse. The data copy reports
   them if they were skipped. Everything else works without them.

## Limits of this setup

This setup is for a demo and a pilot. It is not production-hardened:

* PostgreSQL accepts connections from any Azure service (firewall rule `0.0.0.0`) and from your IP. For production, use
  private networking (a VNet-integrated Container Apps environment and a private PostgreSQL endpoint).
* The apps use the PostgreSQL admin login. For production, create separate least-privilege logins.
* The site's own sign-in protects the Data Estate pages. To protect every page, also turn on Container Apps
  authentication (Microsoft Entra ID) or IP restrictions.

## Troubleshooting

| Symptom | Look at |
|---|---|
| `/health` never answers | `az containerapp logs show -n datatrust-web -g rg-datatrust-demo --follow` (needs `az extension add -n containerapp`) |
| Data copy blocked | your IP changed: run again, or add it under PostgreSQL > Networking |
| `Could not write ... to Key Vault` | the role assignment can take a few minutes; run again |
| Image build fails | the `az acr build` log above the error; the build imports every module, so a missing package shows there |
