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

## Two databases
DataTrust keeps its own data (logins, connections, ADF telemetry, approvals, alerts sent) in its **own database**,
`APP_PG_*` in `.env` (default: database `datatrust_app` on the `PG_*` server). It is created on first start, and
DataTrust's tables that used to live in the POC database are copied over once. The POC database (`PG_*`) is only a
monitored source. If the database user may not create databases (common on Azure), create `datatrust_app` once by hand.

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

## Data checks
**Data checks** (managed as rules in **DQ Rule Studio** → Add rule; each rule opens its own page with history) watch any table or query in a connected PostgreSQL or SQL Server database (Data estate →
Connections): freshness, row volume against the usual, empty values, unique keys, schema changes, your own read-only
SQL, and reconciliations between two queries (also across databases, e.g. source totals vs target totals). Each check
runs on its own schedule; results and their evidence (duplicate keys, differing rows, the SQL that ran) are kept in
DataTrust's database (`monitor.*`). Checks only read: PostgreSQL in a read-only session, SQL Server inside a transaction
that is always rolled back, and user SQL must be one SELECT. On first start a few starter checks are added for the POC
database, including the payment-gateway reconciliation. Run the scheduler separately with `CHECKS_IN_WEB=false` and
`python web/checks/runner.py`. SQL Server sources need `pip install pyodbc` and Microsoft ODBC Driver 18.

## One source at a time
Pick a **Source** at the top of the page: every connection (PostgreSQL, SQL Server) and every connected Data Factory.
Observe, Detect, Investigate, Explain, Impact, Verify and the trust score then show **only that source**; nothing is
blended across sources.

| Source | Trust score from | Incidents |
|---|---|---|
| The POC database (the `PG_*` connection) | its pipeline rules (below), minus its other failing data checks | pipeline findings + data checks |
| Any other database | its data checks: 100 − 40 / 25 / 10 / 4 per failing CRITICAL / HIGH / MEDIUM / LOW check, − 5 per check that could not run, − 2 per warning | its failing checks |
| An Azure Data Factory | the average of its pipeline scores (ADF connector) | its ADF issues |

A source with nothing measured yet shows "No data yet" rather than a perfect score.
The Data quality pages follow the source too: for the POC they are its own rule engine (Rejected records, DQ Rule
Studio); for a database they list the rows its failing checks found and its checks as rules; for a factory they show
the rows ADF skipped while copying and each Copy step's data settings with DataTrust's checks of every copy (`web/quality.py`). Incidents have a lifecycle in
`app.incident_state`: first seen, acknowledged (button on Detect), and gone, always with a recorded reason (`web/resolution.py`):

| Reason | When | Verify shows it as |
|---|---|---|
| Fixed by DataTrust | auto-fix or re-insert corrected the rows (`audit.dq_corrections`) | verified resolved |
| Fixed by a pipeline rerun | a newer POC batch (or ADF run) succeeded and the same, unchanged rule finds nothing | verified resolved |
| Re-checked: passes | the data check ran again after the incident opened and passed | verified resolved |
| Accepted | a schema change was acknowledged, or a schema check's new columns were accepted | verified resolved |
| Rule changed | the rule was edited, switched off or deleted after the incident opened | not verified |
| Not verified | it disappeared and none of the above was recorded (e.g. data cleared or reloaded) | not verified |
| Still failing in a newer run | the same failure appears under a newer run's id | not resolved: the new incident keeps the original "open since" |

When an incident opens, DataTrust notes where the POC's logs stand (latest batch, last correction id, last rule-change id) and later
compares ids, not clocks, so different time zones in the two databases cannot give a wrong reason. Google Chat and the daily briefing
use the same reasons: only verified fixes are announced as resolved.
Google Chat alerts cover every source and name it on each card. Links: `#observe=db:3`, `#detect=adf:3`.

## AI features (LLM_PROVIDER)
| Where | What the model does | What it sees |
|---|---|---|
| Investigate | Root cause in plain words, confidence, the facts it rests on, what the evidence cannot tell | the incident's evidence pack only |
| Impact | A fix plan and prevention steps; new steps can be ticked into the approval | same |
| Observe | Today's briefing for the source: state, what broke / got fixed, what to watch; optional daily post to Google Chat (`BRIEFING_CHAT_AT`) | the source's score, incidents and trend |
| DQ Rule Studio → Add rule | Suggested rules for a table (also without a model, from the profile) | the table's measured profile (counts, empty %, distinct, ranges) |
| Ask DataTrust AI | Questions about the selected source (or any other by name) | read-only tools: overview, incidents, checks, ADF runs, catalog, one SELECT |

Rules (`web/llm.py`): the model never gets direct access; it answers from evidence DataTrust measured; any number it writes
that is not in the evidence is flagged on the page; answers are cached by evidence (`app.ai_notes`), so pages show a saved
answer and only a button press spends a model call; without a model every feature falls back to its plain version.

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
