# DataTrust product foundation tracker

Plain-language record of architecture and deployment foundation work. Improvements to features that already exist are tracked separately in [`CURRENT_PHASE_CHANGE_TRACKER.md`](./CURRENT_PHASE_CHANGE_TRACKER.md).

**Current phase:** Foundation

**Last updated:** 2026-11-03

## Verification

- The web application's imports pass locally using the same module check added to the Docker build.
- The telemetry schema parses as JSON, and its required fields and conditional rules are present.
- The Docker image itself was not built: Docker is not installed in this environment.

## Completed in this change

| Section | Change | Status |
|---|---|---|
| Container dependencies | Added a web-only requirements file so Docker has the file it already expects and does not install Spark. | Done |
| Container contents | Included `dq_rules.py`, which the web app imports but the image previously did not copy. | Done |
| Container validation | Added an image-build import check so missing application modules or web dependencies fail during build; equivalent local import check passed. | Done (image build pending) |
| Local compose credentials | Removed the weak `change-me` database-password fallback; Compose now requires `PG_PASSWORD`. | Done |
| Telemetry contract | Added a versioned JSON Schema for portable pipeline, task, asset, quality, and failure events. | Done (contract only) |
| Product architecture | Documented current capabilities, target concepts, connector rules, enterprise requirements, and phased delivery. | Done |

## Next work (not implemented yet)

| Priority | Section | Next change |
|---|---|---|
| P0 | Build and test automation | Run Docker builds in CI and add automated API, database migration, and connector contract tests. |
| P0 | Secure tenant identity | Implement and test tenant-scoped authentication/authorization before exposing ingestion to customers. |
| P1 | Telemetry ingestion | Add an authenticated, durable, idempotent ingestion path that validates the event contract and records provenance. |
| P1 | First customer integration | **Azure Data Factory connector built** (`web/adf`, pages under "Azure Data Factory", [`docs/ADF_CONNECTOR.md`](./docs/ADF_CONNECTOR.md)). Tested with recorded-shape API payloads, a real PostgreSQL database and headless-browser screenshots; **still to do: validate against the live ADF pipelines** (ADLS/Blob → PostgreSQL). |
| P1 | Own database (phase 1 of the generic product) | **Done:** DataTrust keeps its own data in a separate database (`APP_PG_*`, default `datatrust_app`): `portal` (logins, connections, people, access requests), `telemetry` (connector data) and `app` (approvals, alerts sent). Created on first start; existing rows move from the POC database once. The POC database is now only a monitored source. Snowflake parked. |
| P1 | AI features (phase 4) | **Done:** `web/llm.py` (provider switch, evidence-only grounding, unverified-number flags, cache by evidence), `web/ai.py` (incident root cause + fix plan on Investigate / Impact, suggested checks from a table profile on Add check, per-source briefing on Observe with optional daily Google Chat post), `web/ask.py` (Ask across sources with read-only tools). Tested live with Gemini on the POC, the ADF factory and the Azure database; the Gemini key is on the free tier (20 requests a day), which limits real use. |
| P1 | Pages per source (phase 3) | **Done:** a Source picker in the top bar; Observe → Verify, Explain, the trust score and alerts are computed per connection or factory (`web/scopes.py`), never blended. POC: its pipeline rules + its other checks; other databases: their data checks; ADF: its pipeline scores. Incidents get a lifecycle (first seen, acknowledge, resolved by data) in `app.incident_state`. Tested: unit tests + live on the POC, the ADF factory and an unchecked Azure database. |
| P1 | Generic data checks (phase 2) | **Done:** `web/checks` + pages "Data checks" / "Add check". Seven check types (freshness, volume vs learned usual, empty values, unique key, schema change with accept, custom read-only SQL, reconciliation between two queries on any connections), per-check schedule, result history with evidence and the SQL that ran, in `monitor.*`. Dialect layer for PostgreSQL and SQL Server (Snowflake = one more dialect). The hard-wired gateway-vs-silver reconciliation is now a configured starter check and reproduces the same two mismatched dates. Tested: unit tests for both dialects and every check type; live on the POC PostgreSQL database. SQL Server path not yet run live (the configured server was unreachable). |
| P1 | Shared observability model | Persist tenants, sources, pipelines, runs, tasks, assets, lineage edges, quality results, and incidents with migrations and retention policies. |
| P1 | Incident operations | Add configurable severity, ownership, acknowledgement, routing, escalation, and incident audit history. |
| P2 | Enterprise operations | Add SSO, secret-store integration/rotation, retention/deletion, backups, monitoring, and runbooks. |
| P2 | Connector expansion | Validate the integration contract with a second platform before committing to broad platform coverage. |

## Scope note

The event schema and architecture notes are design groundwork. They do **not** add an ingestion API, persistence model, SSO, tenant enforcement, or new Azure/Snowflake/Databricks monitoring. The existing dashboard remains centered on the POC's PySpark/PostgreSQL pipeline.
