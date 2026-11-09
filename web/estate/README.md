# Data Estate module

Connect databases and warehouses to the DataTrust portal and see, in one place:
what is in them (catalog), who changed what, what failed, who has access, and grant access
through request -> approve -> apply. An estate assistant answers questions about any connected source.

PostgreSQL and SQL Server sources have catalog-browsing connectors. Snowflake and SAP HANA can be
connection-tested, but cannot yet be browsed in the portal. Adding a source is not the same as
collecting its pipeline telemetry.

The web app currently monitors its own PySpark/PostgreSQL POC pipeline; it does not yet collect
run events from arbitrary external orchestrators. A proposed vendor-neutral event envelope is
documented in [`../../contracts/pipeline-event-v1.schema.json`](../../contracts/pipeline-event-v1.schema.json).
That schema is not yet accepted by an API or persisted.

## Files
| File | Purpose |
|---|---|
| store.py | Portal tables in schema `portal` (users, connections, access requests, audit log), login, sessions |
| postgres_connector.py | Catalog, change tracking (event triggers), failures, roles, safe GRANT/REVOKE/CREATE USER |
| service.py | Request workflow and the estate assistant |
| routes.py | API under /api/estate (login required; changes need admin) |
| ../static/estate.js | The five pages in the sidebar group "Data estate" |

## First start
`python web\server.py` creates schema `portal` and the first admin. If `PORTAL_ADMIN_PASSWORD` is not set in .env,
a password is generated and printed ONCE in the terminal. The POC database is added as the first source.

## Security
* Passwords of sources are never stored: a source keeps the NAME of a .env variable.
* Browsing runs in read-only sessions (10 s timeout).
* Access changes run only after an admin approves; the exact SQL is shown before; identifiers are quoted;
  every name is checked against the live catalog; each step goes to portal.audit_log.
* New database users get a random 16-character password, shown once to the approving admin.
* Change tracking (Connections page) installs schema dt_audit + 2 event triggers in the chosen database;
  it needs a superuser login on that source.
