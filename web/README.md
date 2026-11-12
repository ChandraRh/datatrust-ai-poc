# DataTrust AI web app

A web page on top of your PostgreSQL gold views and audit tables. Every number is a live query;
when the pipeline runs again, the page detects it (checks every 15 s) and refreshes itself.

The website and the ADLS ETL worker are separate processes. The website reads PostgreSQL; the
worker reads complete CSV batches from ADLS, processes them, and writes the results to PostgreSQL.

## Run
```powershell
.venv\Scripts\activate
pip install -r requirements.txt
python run_pipeline.py --setup           # once per new database
```

Configure `.env` with Azure PostgreSQL and ADLS settings, then use two terminals:

```powershell
# Terminal 1: ADLS pipeline worker; keeps watching for a ready batch
python adls_pipeline_worker.py

# Terminal 2: website; open http://localhost:8000
python web\server.py
```

Upload all required CSV files under `ADLS_FOLDER/<batch>/`, then upload an empty `_READY` file
last. Use `full` for the first historical load or a `YYYY-MM-DD` folder for a daily batch. The worker
stores `READY`, `RUNNING`, `SUCCEEDED`, or `FAILED` in PostgreSQL; the Pipeline log lets an admin retry
failed batches using the files currently stored in ADLS. To upload local sample CSVs and create `_READY`, run
`python upload_to_adls.py full` or `python upload_to_adls.py YYYY-MM-DD`.

## Sign-in setup

The dashboard opens without a sign-in prompt. The Data Estate sections (Connections, Catalog, Activity, Access, and related tools) ask you to sign in when opened; their APIs remain protected. `/login` is not shown on initial website entry. `PORTAL_AUTH_ENABLED=true` keeps Data Estate sign-in enabled. Setting it to `false` disables sign-in for those sections and opens their APIs/admin actions, so only use that setting for a trusted local instance.

With authentication enabled, copy `.env.example` to `.env`, then configure:

- `PORTAL_ADMIN_USER` and `PORTAL_ADMIN_PASSWORD` for the bootstrap administrator. These are used to create an account only when the portal has no users.
- The local development database and `.env` are set to the requested `admin` / `admin` Data Estate credentials. Change them before exposing the app or using it beyond a trusted local machine.
- `PORTAL_SECRET` with a stable random value, and `PORTAL_COOKIE_SECURE=true` when using HTTPS.
- Gmail SMTP settings (`SMTP_USER`, `SMTP_PASSWORD`, `SMTP_FROM`) for verification and password-reset links. Use a Google App Password, not your Gmail password.
- Optional `GOOGLE_CLIENT_ID` and `GOOGLE_CLIENT_SECRET`; register `http://localhost:8000/api/auth/google/callback` as a development redirect URI.

Set `DATATRUST_PUBLIC_URL` for deployed environments so emailed links use the public HTTPS address. Email/password accounts must verify their email before signing in. New accounts are viewers; admin access is assigned through the bootstrap account or `PORTAL_ADMIN_EMAILS`. Existing users without a linked verified email need an administrator email configured before they can sign in. See the [Azure deployment guide](../AZURE_DEPLOYMENT_GUIDE.md) for cloud settings and Google/Gmail setup.

## Azure Data Factory connector
Connect a factory on **Connect ADF** (read-only Azure access: see [docs/ADF_CONNECTOR.md](../docs/ADF_CONNECTOR.md)). The
server reads it every `ADF_POLL_SECONDS` and the **ADF pipelines / ADF issues / ADF runs** pages show every run, step, error,
diagnosed cause and the checks DataTrust runs on the ADLS source files and PostgreSQL target tables. Run the collector as its own
process with `ADF_COLLECTOR_IN_WEB=false` and `python web/adf/collector.py`.

## Pages
| Page | What it shows | Where the data comes from |
|---|---|---|
| 1. Observe | Trust score, trend per business date, KPIs, quality bars, revenue chart, layer status | gold.*, audit.batch_control, audit.step_log |
| Ask DataTrust AI | Chat: revenue for any period / by day, method, region, segment; missing data; rejects; duplicates; reconciliation; pipeline; trust score; customers | agent.py tools |
| 2. Detect | Open incidents, most severe first | gold.settlement_reconciliation, silver.rejected_records, audit.step_log |
| 3. Investigate | Root cause, evidence checks (each names its source), evidence chain, affected rows | same, plus bronze / source file counts |
| 4. Explain | Source file → bronze → silver → gold for every entity, with rows held back or lost | source CSV, bronze, audit.step_log, information_schema.view_table_usage |
| 5. Impact & decide | Gold views affected, revenue at risk (exact gap), actions; Approve | audit.remediation_log (created automatically) |
| 6. Verify | Approved incidents become RESOLVED only when the next run no longer shows them | audit.remediation_log + live detection |
| Pipeline log | Steps, batches, errors and warnings | audit.* |

## Trust score (explainable, shown on page 1)
Starts at 100: −15 per business date where settlement ≠ silver (max −45), −up to 10 for rows lost between
source and silver, −up to 10 for rejected rows, −up to 5 for duplicates, −30 if the latest run has a
failed/skipped layer, −10 if no successful run in 24 h. ≥ 90 Healthy, ≥ 75 Needs attention, below At risk.
The rules are constants at the top of data_service.py.

## AI agent modes (.env LLM_PROVIDER)
* `none` (default): built-in agent, free and offline. Understands the questions listed on the Ask page.
* `ollama`: free local model. Install Ollama, `ollama pull qwen2.5:7b`, set LLM_PROVIDER=ollama.
* `anthropic`: Claude via API key (paid). Set LLM_PROVIDER=anthropic and ANTHROPIC_API_KEY.
* `gemini`: Google Gemini via API key (paid). Set LLM_PROVIDER=gemini and GEMINI_API_KEY; optionally set GEMINI_MODEL.

In the model modes the model can only call fixed read-only tools plus a SELECT-only query on gold.* views
(read-only transaction, 10 s timeout, 200 rows). If the model is unavailable, the built-in agent answers.

## Dates
'Last month', 'last 7 days', 'yesterday' count back from the latest business date in gold
(DATATRUST_AS_OF=data). With the sample data (2026-10-01 to 10-08), 'last month' = September → the agent
says no data is loaded and shows the available range; it fills in automatically once those dates are loaded.

## Product scope
This is a proof of concept centered on its own PySpark/PostgreSQL pipeline. The Data Estate can browse
PostgreSQL and SQL Server; Snowflake and SAP HANA are connection-testable only. It is not yet a general
collector for arbitrary Azure, Snowflake, or other orchestrator runs. See the project-root
[`PRODUCT_ARCHITECTURE.md`](../PRODUCT_ARCHITECTURE.md) and
[`PRODUCT_CHANGE_TRACKER.md`](../PRODUCT_CHANGE_TRACKER.md) for the target direction and planned work.
