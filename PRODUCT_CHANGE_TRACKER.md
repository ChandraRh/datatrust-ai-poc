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
| P1 | First customer integration | Choose the first customer-priority platform and implement read-only run/error/asset collection end-to-end. Azure Data Factory is one candidate, not an implemented connector. |
| P1 | Shared observability model | Persist tenants, sources, pipelines, runs, tasks, assets, lineage edges, quality results, and incidents with migrations and retention policies. |
| P1 | Incident operations | Add configurable severity, ownership, acknowledgement, routing, escalation, and incident audit history. |
| P2 | Enterprise operations | Add SSO, secret-store integration/rotation, retention/deletion, backups, monitoring, and runbooks. |
| P2 | Connector expansion | Validate the integration contract with a second platform before committing to broad platform coverage. |

## Scope note

The event schema and architecture notes are design groundwork. They do **not** add an ingestion API, persistence model, SSO, tenant enforcement, or new Azure/Snowflake/Databricks monitoring. The existing dashboard remains centered on the POC's PySpark/PostgreSQL pipeline.
