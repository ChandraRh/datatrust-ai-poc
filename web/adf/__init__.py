"""
Azure Data Factory connector.

Reads an ADF factory through the Azure management REST API (read-only) and stores what it finds in the
`telemetry` schema, so the website can show every pipeline, run, activity (step), error, root cause, and the state
of the source (ADLS / Blob) and target (PostgreSQL) each Copy activity touched.

    client.py      ADF REST API: definitions, pipeline runs, activity runs, trigger runs (Azure AD token, retries)
    store.py       telemetry.* tables (sources, definitions, pipeline_runs, task_runs, trigger_runs, checks, sync_log)
    resolve.py     dataset / linked service -> concrete asset (storage path, PostgreSQL table), parameters resolved
    diagnose.py    error -> cause, affected system, and the steps to fix it; failure chain across child pipelines
    probes.py      checks the source files and the target table each Copy activity used
    collector.py   one sync (definitions -> runs -> activities -> triggers -> checks) and the polling loop
    insights.py    what the pages show: health, issues, run detail with timeline, lineage
    routes.py      /api/adf/*

Run the collector on its own:  python web/adf/collector.py            (polls every ADF_POLL_SECONDS)
                               python web/adf/collector.py --once
The web server also starts the same loop in a background thread unless ADF_COLLECTOR_IN_WEB=false.
"""
