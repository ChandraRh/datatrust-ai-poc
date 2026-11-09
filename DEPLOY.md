# Deploying the DataTrust AI web app (for the DevOps team)

For Azure-specific resource setup and deployment steps, see the shareable [Azure deployment guide](./AZURE_DEPLOYMENT_GUIDE.md).

The website and ADLS ETL worker are separate containers/services. The website reads PostgreSQL; the
worker watches ADLS for complete batches, runs the PySpark ETL, and writes results and run logs to
the same PostgreSQL database. Kafka streaming is a separate optional workload.

```
 Users ──HTTPS──► datatrust-web container :8000 ────────────────┐
                                                                 ├──► PostgreSQL 17 (managed)
 ADLS batch ──► datatrust-pipeline worker ───────────────────────┘
```

## 1. Database
* Managed PostgreSQL 17 (e.g. Azure Database for PostgreSQL Flexible Server).
* Load the data once: restore `datatrust_backup.backup` with pg_restore (or run the pipeline against it).
* The app creates schema `portal` itself on first start.
* Network: allow the container's outbound IP / VNet only; require SSL.

## 2. Containers
```bash
docker build -t datatrust-web .
docker build -f Dockerfile.pipeline -t datatrust-pipeline .
```
The web image uses `requirements-web.txt`; the ETL image uses `requirements-pipeline.txt` and Java 17.
Neither image contains `.env`, source data, or credentials.

Run the local ADLS worker with `python adls_pipeline_worker.py` and the website with `python web/server.py`
in separate terminals. Batch status is stored in PostgreSQL and shown in the website's Pipeline log, where
an administrator can retry failed batches. See [AZURE_DEPLOYMENT_GUIDE.md](./AZURE_DEPLOYMENT_GUIDE.md)
for ADLS batch layout, `_READY` markers, and Azure Container Apps deployment.

Try the web app with PostgreSQL on one machine. Set a non-empty `PG_PASSWORD` in `.env` first:
`docker compose -f docker-compose.web.yml up -d --build`. The database container is empty; load the
project schema and data before using the dashboard.

## 3. Settings (environment variables; secrets from Key Vault / secret store)
| Variable | Value in the cloud | Secret? |
|---|---|---|
| PG_HOST, PG_PORT, PG_DATABASE, PG_USER | managed PostgreSQL | |
| PG_PASSWORD | database password | yes |
| WEB_HOST | 0.0.0.0 (already set in the image) | |
| PORT or WEB_PORT | port the platform expects (default 8000) | |
| PORTAL_ADMIN_USER / PORTAL_ADMIN_PASSWORD | first portal admin (used only when no portal user exists) | password: yes |
| PORTAL_SECRET | long random string; signs login cookies (keep the same across restarts and instances) | yes |
| PORTAL_COOKIE_SECURE | true (site served over HTTPS) | |
| PORTAL_COMPANY_DOMAIN | optional: Connect accepts only these e-mail domains, e.g. `isteer.com` | |
| GOOGLE_CHAT_WEBHOOK_URL | optional: Google Chat space webhook for incident alerts | yes |
| DATATRUST_PUBLIC_URL | the public https address of the app (links in alert cards) | |
| ALERT_MIN_SEVERITY / ALERT_INTERVAL_SECONDS | default MEDIUM / 60 | |
| CONN_<NAME>_PASSWORD | one per extra source added in the Data Estate portal | yes |
| LLM_PROVIDER (+ ANTHROPIC_API_KEY, OLLAMA_URL, or GEMINI_API_KEY) | none / anthropic / ollama / gemini | key: yes |
| DATATRUST_AS_OF | data (default) or today | |

## 4. Platform checklist
* HTTPS only (TLS at the load balancer); redirect HTTP.
* Health probe: `GET /health` → 200 `{"status":"ok"}`, 503 if the database is unreachable.
* Scaling: stateless; sessions are signed cookies, so several instances work if they share PORTAL_SECRET.
* Logs: stdout (set WEB_LOG_LEVEL=info for request logs).
* Access: put the site behind company SSO / IP allow-list for the demo if possible; the portal login is a POC login.
* The Data Estate portal can change permissions on connected databases (after admin approval):
  give its source logins only the rights you want it to manage.


## 5. Optional: SQL Server sources from the cloud
The default image talks to PostgreSQL only. To connect SQL Server / Azure SQL sources in the Data Estate portal,
build the image with Microsoft's ODBC driver (adds about 60 MB):

    docker build --build-arg WITH_MSSQL=true -t datatrust-web .

The SQL Server must be reachable from the cloud (firewall / private network) and use a SQL login
(Windows login only works on a Windows laptop). Put its password in a secret named as on the Connections page.

## 6. Connect page (Google Workspace)
No setup needed: Chat, Meet, Calendar and Gmail open in the user's own browser, signed in with their company account.
The people directory lives in table portal.people (created on first start). Set PORTAL_COMPANY_DOMAIN to allow only
company e-mail addresses.


## 7. Auto-fix, schema-change guard, alerts
* **Auto-fix** (Impact page) writes to silver only after a portal admin approves; every change is kept in
  `audit.dq_corrections` (created on first use) and the silver layer re-applies it on later runs.
  The database login of the app therefore needs INSERT/UPDATE/DELETE on silver and audit (the POC login has it).
* **Schema-change guard** reads `dt_audit.ddl_log`: enable tracking for the pipeline database once on the
  Connections page. This installs PostgreSQL event triggers, which need a superuser; some managed PostgreSQL
  services do not allow event triggers. Without tracking the guard is simply silent; everything else works.
* **Alerts**: the web app itself posts to Google Chat (outbound HTTPS to chat.googleapis.com must be allowed).
  With several instances each alert is still sent once (claimed in `audit.alert_log`).


## 8. Data quality: Rejected records + DQ Rule Studio
* Tables are created by the web app on first start (CREATE privilege on schema audit): `audit.dq_rules` (seeded with
  the 17 rules that were in silver_layer.py, unchanged), `audit.dq_rule_changes`, `audit.rejected_history`,
  `audit.dq_reprocess`, `audit.rejected_review`. The silver layer also creates `audit.rejected_history` if missing.
* The silver layer reads `audit.dq_rules` at the start of every run. No table / no rows for a table -> the rules in
  the code are used, so the pipeline works with or without the web app.
* Every rejected row is kept in `audit.rejected_history` (silver.rejected_records only holds the latest load).
* Re-insert: a portal user requests it, an admin approves; SCD1 tables are loaded at once, SCD2 tables (customers,
  products) at the next silver run. The fix is kept in `audit.dq_corrections`, so later full re-runs give the same result.
* Before testing with NEW sample data, clear the old demo fixes (they are matched by record key):
  `UPDATE audit.dq_corrections SET active = FALSE;`
