# DataTrust product foundation

## What exists today

- The main pipeline is a PySpark medallion POC: local/ADLS files flow through Bronze, Silver, and PostgreSQL Gold views.
- The web dashboard reads that POC's Gold views and audit tables. Its trust score, reconciliation, and some incident logic use the demo's data model.
- The Data Estate can browse PostgreSQL and SQL Server. Snowflake and SAP HANA can be connection-tested, but do not have catalog-browsing connectors.
- Kafka streaming is an optional add-on.
- **Azure Data Factory connector (first external integration):** read-only collection of pipelines, runs, activity runs, trigger runs, errors with diagnosed causes, and checks of ADLS/Blob sources and PostgreSQL targets, stored in the platform-neutral `telemetry` schema. See [`docs/ADF_CONNECTOR.md`](./docs/ADF_CONNECTOR.md). Not yet validated against a live customer factory; tenant isolation is still the portal's single-tenant login.

These are implemented capabilities, not a claim that arbitrary cloud pipelines are currently monitored.

## Product direction

Make DataTrust a vendor-neutral operations and trust layer that observes pipelines customers already run. Start with one target customer/use case and a small set of integrations; prove that signals are reliable before expanding platform coverage.

The shared product concepts should be independent of any one warehouse:

1. **Tenant and source** — the customer boundary, connected system, environment, and integration identity.
2. **Pipeline and run** — the external pipeline identity, each execution, status, timestamps, duration, and retries.
3. **Task and event** — step-level status and time-ordered source events with stable IDs for deduplication.
4. **Asset and lineage edge** — source-qualified datasets and their read/write relationships.
5. **Quality result and incident** — measured evidence, severity, ownership, lifecycle, and affected downstream assets.

The versioned event envelope is in [`contracts/pipeline-event-v1.schema.json`](./contracts/pipeline-event-v1.schema.json). It is a design contract only: no API, durable event store, tenant enforcement, or external connector currently accepts it.

## Integration rules

- Prefer documented source APIs or standard event formats; do not require customers to replace their orchestrator.
- Give each connector a clear health state, last successful sync, permissions, and source-side limitations.
- Use stable source IDs and idempotent event handling; tolerate retries, delayed events, and out-of-order delivery.
- Preserve source timestamps and IDs, record DataTrust ingestion time separately, and show provenance for derived conclusions.
- Request read-only access by default. Keep credentials in an approved secret store and never include them in telemetry or logs.
- Treat provider-specific details as connector metadata; keep the common run/asset/incident model portable.

## Enterprise requirements before production use

- Establish authentication and tenant context before accepting customer telemetry. A `tenant_id` supplied in a payload is not proof of tenant membership.
- Add SSO/OIDC or SAML, tenant-scoped authorization, least-privilege service identities, secret rotation, audit trails, retention/deletion controls, and secure transport.
- Define alert policies, incident ownership and acknowledgement, escalation, maintenance windows, and safe/manual approval for any action that changes customer systems or data.
- Separate collection from the web request process; use durable ingestion, retry/backoff, rate-limit handling, deduplication, metrics, and operational dead-letter/replay controls.
- Add migration discipline, automated tests, integration contract tests, deployment checks, backups, and support/runbook practices.

## Delivery order

1. **Foundation (current phase):** repair and verify container packaging, write the shared telemetry contract, document current-vs-target capability, and keep a change tracker.
2. **First integration:** select the first customer-priority orchestrator/warehouse; implement read-only collection end-to-end, including health, run history, errors, assets, and lineage.
3. **Reliable platform:** add durable ingestion, replay/deduplication, tenant isolation, SSO, retention, alert workflows, and operational metrics.
4. **Expand deliberately:** validate the connector contract with another platform before adding more integrations.

Do not present a schema or roadmap as an implemented integration. Update the tracker when implementation and validation actually land.
