"""
telemetry.* tables: what the ADF connector collected. Created on first start, never dropped.

The tables are platform-neutral (a source has a `kind`), so the next connector (Airflow, Databricks, ...) reuses
them. ADF specifics stay in jsonb columns (definition, input, output, error).

    sources        one row per connected factory: settings (no secrets, only env-variable NAMES), sync health
    definitions    pipelines, datasets, linked services, triggers as ADF returns them
    pipeline_runs  every pipeline run (status, times, parameters, who/what started it, parent run)
    task_runs      every activity run (= a step): status, error, rows/bytes read and written, input/output
    trigger_runs   every trigger firing (schedule / tumbling window / storage event), including failed ones
    checks         source and target checks done for Copy activities (files present, row counts, table state)
    sync_log       every sync: when, how it was started, what it read, and the error if it failed
"""
from __future__ import annotations

import hashlib
import json
import re
import secrets
import sys
from datetime import datetime, timezone
from pathlib import Path

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

sys.path.insert(0, str(next(f for f in Path(__file__).resolve().parents if (f / "config.py").exists())))
import config  # noqa: E402

KIND = "azure_data_factory"
ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
GUID = re.compile(r"^[0-9a-fA-F-]{36}$")
NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.()\- ]{0,89}$")


def connect():
    """DataTrust's own database (APP_PG_*): telemetry is the product's data, not the monitored source's."""
    import appdb
    return appdb.connect(application_name="datatrust-adf-connector")


DDL = """
CREATE SCHEMA IF NOT EXISTS telemetry;

CREATE TABLE IF NOT EXISTS telemetry.sources (
    source_id            INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    kind                 TEXT NOT NULL,
    name                 TEXT NOT NULL UNIQUE,
    config               JSONB NOT NULL,
    enabled              BOOLEAN NOT NULL DEFAULT true,
    webhook_token_hash   TEXT,
    created_by           TEXT,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    watermark            TIMESTAMPTZ,
    definitions_synced_at TIMESTAMPTZ,
    last_sync_started    TIMESTAMPTZ,
    last_sync_finished   TIMESTAMPTZ,
    last_sync_status     TEXT,
    last_sync_error      TEXT,
    last_success_at      TIMESTAMPTZ,
    sync_requested_at    TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS telemetry.definitions (
    source_id   INTEGER NOT NULL REFERENCES telemetry.sources ON DELETE CASCADE,
    kind        TEXT NOT NULL,                 -- pipeline / dataset / linkedservice / trigger
    name        TEXT NOT NULL,
    type        TEXT,
    body        JSONB NOT NULL,
    removed     BOOLEAN NOT NULL DEFAULT false,
    synced_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (source_id, kind, name)
);

CREATE TABLE IF NOT EXISTS telemetry.pipeline_runs (
    source_id        INTEGER NOT NULL REFERENCES telemetry.sources ON DELETE CASCADE,
    run_id           TEXT PRIMARY KEY,
    pipeline_name    TEXT NOT NULL,
    status           TEXT,
    run_start        TIMESTAMPTZ,
    run_end          TIMESTAMPTZ,
    duration_ms      BIGINT,
    invoked_by_type  TEXT,
    invoked_by_name  TEXT,
    parent_run_id    TEXT,
    run_group_id     TEXT,
    is_latest        BOOLEAN,
    parameters       JSONB,
    message          TEXT,
    last_updated     TIMESTAMPTZ,
    raw              JSONB,
    ingested_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    activities_synced_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS pipeline_runs_by_pipeline ON telemetry.pipeline_runs (source_id, pipeline_name, run_start DESC);

CREATE TABLE IF NOT EXISTS telemetry.task_runs (
    source_id        INTEGER NOT NULL REFERENCES telemetry.sources ON DELETE CASCADE,
    activity_run_id  TEXT PRIMARY KEY,
    run_id           TEXT NOT NULL,
    pipeline_name    TEXT,
    activity_name    TEXT,
    activity_type    TEXT,
    status           TEXT,
    run_start        TIMESTAMPTZ,
    run_end          TIMESTAMPTZ,
    duration_ms      BIGINT,
    error_code       TEXT,
    error_message    TEXT,
    failure_type     TEXT,
    rows_read        BIGINT,
    rows_written     BIGINT,
    rows_skipped     BIGINT,
    data_read        BIGINT,
    data_written     BIGINT,
    files_read       BIGINT,
    files_written    BIGINT,
    throughput_kbps  DOUBLE PRECISION,
    input            JSONB,
    output           JSONB,
    error            JSONB,
    user_properties  JSONB,
    ingested_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS task_runs_by_run ON telemetry.task_runs (run_id, run_start);

CREATE TABLE IF NOT EXISTS telemetry.trigger_runs (
    source_id        INTEGER NOT NULL REFERENCES telemetry.sources ON DELETE CASCADE,
    trigger_run_id   TEXT PRIMARY KEY,
    trigger_name     TEXT,
    trigger_type     TEXT,
    status           TEXT,
    fired_at         TIMESTAMPTZ,
    message          TEXT,
    pipelines        JSONB,
    properties       JSONB,
    ingested_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS telemetry.checks (
    source_id        INTEGER NOT NULL REFERENCES telemetry.sources ON DELETE CASCADE,
    activity_run_id  TEXT NOT NULL,
    run_id           TEXT NOT NULL,
    check_type       TEXT NOT NULL,
    side             TEXT NOT NULL,            -- source / target
    asset            TEXT,
    status           TEXT NOT NULL,            -- PASS / INFO / WARN / FAIL / UNKNOWN
    expected         TEXT,
    observed         TEXT,
    detail           TEXT,
    checked_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (activity_run_id, check_type)
);
CREATE INDEX IF NOT EXISTS checks_by_run ON telemetry.checks (run_id);

CREATE TABLE IF NOT EXISTS telemetry.sync_log (
    sync_id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    source_id        INTEGER NOT NULL REFERENCES telemetry.sources ON DELETE CASCADE,
    started_by       TEXT,
    started_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at      TIMESTAMPTZ,
    status           TEXT,
    pipeline_runs    INTEGER DEFAULT 0,
    task_runs        INTEGER DEFAULT 0,
    trigger_runs     INTEGER DEFAULT 0,
    checks           INTEGER DEFAULT 0,
    error            TEXT
);

-- what an admin typed on Connect ADF but has not connected yet (one form; no secrets, only variable names)
CREATE TABLE IF NOT EXISTS telemetry.setup_draft (
    draft_id   INTEGER PRIMARY KEY DEFAULT 1 CHECK (draft_id = 1),
    body       JSONB NOT NULL,
    saved_by   TEXT,
    saved_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""


def ensure_tables() -> None:
    with connect() as conn:
        conn.execute(DDL)
        conn.commit()


def J(value):
    return Jsonb(value) if value is not None else None


# ============================================================================ sources
def validate_config(cfg: dict) -> dict:
    """Keep only known settings and check their shape. Secrets are never stored: only env-variable names."""
    out = {
        "subscription_id": (cfg.get("subscription_id") or "").strip(),
        "resource_group": (cfg.get("resource_group") or "").strip(),
        "factory_name": (cfg.get("factory_name") or "").strip(),
        "auth_mode": cfg.get("auth_mode") or "default",
        "tenant_id": (cfg.get("tenant_id") or "").strip(),
        "client_id": (cfg.get("client_id") or "").strip(),
        "client_secret_env": (cfg.get("client_secret_env") or "").strip(),
        "storage_connection_env": (cfg.get("storage_connection_env") or "").strip(),
        "lookback_days": int(cfg.get("lookback_days") or 30),
        "freshness_hours": float(cfg.get("freshness_hours") or 0) or None,
        "target_connection_id": int(cfg["target_connection_id"]) if str(cfg.get("target_connection_id") or "").isdigit() else None,
    }
    if not GUID.match(out["subscription_id"]):
        raise ValueError("subscription_id must be the subscription GUID (Azure portal -> Subscriptions)")
    if not out["resource_group"] or not re.match(r"^[\w\-.()]{1,90}$", out["resource_group"]):
        raise ValueError("resource_group is required")
    if not re.match(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{1,61}[A-Za-z0-9])$", out["factory_name"]):
        raise ValueError("factory_name must be the Data Factory name (letters, digits, hyphens)")
    if out["auth_mode"] not in ("default", "service_principal"):
        raise ValueError("auth_mode must be 'default' or 'service_principal'")
    if out["auth_mode"] == "service_principal":
        if not GUID.match(out["tenant_id"]) or not GUID.match(out["client_id"]):
            raise ValueError("tenant_id and client_id must be GUIDs (App registration -> Overview)")
        if not ENV_NAME.match(out["client_secret_env"]):
            raise ValueError("client_secret_env must be the NAME of a variable in .env, e.g. ADF_CLIENT_SECRET")
    for k in ("client_secret_env", "storage_connection_env"):
        if out[k] and not ENV_NAME.match(out[k]):
            raise ValueError(f"{k} must be the NAME of a variable in .env")
    if not 1 <= out["lookback_days"] <= 45:
        raise ValueError("lookback_days must be 1-45 (ADF keeps run history for 45 days)")
    return out


DRAFT_KEYS = ("subscription_id", "resource_group", "factory_name", "auth_mode", "tenant_id", "client_id",
              "client_secret_env", "storage_connection_env", "target_connection_id", "lookback_days", "freshness_hours")


def clean_draft(name: str, cfg: dict) -> dict:
    """The unfinished form, kept as typed (no validation), minus anything that is not a variable NAME where one is expected,
    so a secret pasted into the wrong field is never saved."""
    out = {k: str(cfg.get(k) if cfg.get(k) is not None else "").strip()[:200] for k in DRAFT_KEYS}
    for k in ("client_secret_env", "storage_connection_env"):
        if out[k] and not ENV_NAME.match(out[k]):
            out[k] = ""
    return {"name": (name or "").strip()[:90], "config": out}


def get_draft() -> dict | None:
    with connect() as conn:
        return conn.execute("SELECT body, saved_by, saved_at FROM telemetry.setup_draft WHERE draft_id = 1").fetchone()


def save_draft(name: str, cfg: dict, by: str) -> dict:
    body = clean_draft(name, cfg)
    with connect() as conn:
        conn.execute("""INSERT INTO telemetry.setup_draft (draft_id, body, saved_by, saved_at) VALUES (1, %s, %s, now())
                        ON CONFLICT (draft_id) DO UPDATE SET body = EXCLUDED.body, saved_by = EXCLUDED.saved_by, saved_at = now()""",
                     (J(body), by))
        conn.commit()
    return body


def clear_draft() -> None:
    with connect() as conn:
        conn.execute("DELETE FROM telemetry.setup_draft")
        conn.commit()


def public(src: dict) -> dict:
    """A source row as the page may see it (no token hash)."""
    return {k: v for k, v in src.items() if k != "webhook_token_hash"}


def list_sources(include_disabled: bool = True) -> list:
    with connect() as conn:
        return conn.execute(f"""SELECT * FROM telemetry.sources WHERE kind = %s {'' if include_disabled else 'AND enabled'}
                                ORDER BY source_id""", (KIND,)).fetchall()


def get_source(source_id: int) -> dict:
    with connect() as conn:
        row = conn.execute("SELECT * FROM telemetry.sources WHERE source_id = %s", (source_id,)).fetchone()
    if not row:
        raise ValueError(f"Data Factory connection {source_id} not found")
    return row


def add_source(name: str, cfg: dict, by: str) -> int:
    name = (name or cfg.get("factory_name") or "").strip()
    if not NAME.match(name):
        raise ValueError("name: 1-90 letters, digits, spaces, . _ - ( )")
    cfg = validate_config(cfg)
    with connect() as conn:
        try:
            row = conn.execute("""INSERT INTO telemetry.sources (kind, name, config, created_by, sync_requested_at)
                                  VALUES (%s, %s, %s, %s, now()) RETURNING source_id""", (KIND, name, J(cfg), by)).fetchone()
        except psycopg.errors.UniqueViolation:
            raise ValueError(f"a connection named {name} already exists")
        conn.commit()
    return row["source_id"]


def update_source(source_id: int, name: str, cfg: dict, enabled: bool) -> None:
    cfg = validate_config(cfg)
    with connect() as conn:
        conn.execute("UPDATE telemetry.sources SET name = %s, config = %s, enabled = %s, sync_requested_at = now() WHERE source_id = %s",
                     (name.strip(), J(cfg), enabled, source_id))
        conn.commit()


def delete_source(source_id: int) -> None:
    with connect() as conn:
        conn.execute("DELETE FROM telemetry.sources WHERE source_id = %s", (source_id,))
        conn.commit()


def request_sync(source_id: int) -> None:
    with connect() as conn:
        conn.execute("UPDATE telemetry.sources SET sync_requested_at = now() WHERE source_id = %s", (source_id,))
        conn.commit()


def new_webhook_token(source_id: int) -> str:
    """A new token for the ADF 'notify DataTrust' Web activity; only its hash is kept, so it is shown once."""
    token = secrets.token_urlsafe(32)
    with connect() as conn:
        conn.execute("UPDATE telemetry.sources SET webhook_token_hash = %s WHERE source_id = %s",
                     (hashlib.sha256(token.encode()).hexdigest(), source_id))
        conn.commit()
    return token


def check_webhook_token(source_id: int, token: str) -> bool:
    with connect() as conn:
        row = conn.execute("SELECT webhook_token_hash FROM telemetry.sources WHERE source_id = %s AND enabled", (source_id,)).fetchone()
    if not row or not row["webhook_token_hash"] or not token:
        return False
    return secrets.compare_digest(row["webhook_token_hash"], hashlib.sha256(token.encode()).hexdigest())


def bootstrap_from_env() -> int | None:
    """ADF_SUBSCRIPTION_ID + ADF_RESOURCE_GROUP + ADF_FACTORY_NAME in .env -> the factory is added on first start."""
    import os
    sub, rg, fac = os.getenv("ADF_SUBSCRIPTION_ID"), os.getenv("ADF_RESOURCE_GROUP"), os.getenv("ADF_FACTORY_NAME")
    if not (sub and rg and fac):
        return None
    with connect() as conn:
        exists = conn.execute("SELECT 1 FROM telemetry.sources WHERE kind = %s AND config->>'factory_name' = %s",
                              (KIND, fac)).fetchone()
    if exists:
        return None
    sp = bool(os.getenv("ADF_CLIENT_ID"))
    return add_source(fac, {"subscription_id": sub, "resource_group": rg, "factory_name": fac,
                            "auth_mode": "service_principal" if sp else "default",
                            "tenant_id": os.getenv("ADF_TENANT_ID", ""), "client_id": os.getenv("ADF_CLIENT_ID", ""),
                            "client_secret_env": "ADF_CLIENT_SECRET" if sp else "",
                            "storage_connection_env": os.getenv("ADF_STORAGE_CONNECTION_ENV", "")}, "env")


# ============================================================================ sync bookkeeping
def try_lock(conn, source_id: int) -> bool:
    """One sync per factory at a time, also across the web server and a separate collector process."""
    return conn.execute("SELECT pg_try_advisory_lock(hashtext('datatrust-adf'), %s) AS ok", (source_id,)).fetchone()["ok"]


def unlock(conn, source_id: int) -> None:
    conn.execute("SELECT pg_advisory_unlock(hashtext('datatrust-adf'), %s)", (source_id,))


def sync_started(conn, source_id: int, by: str) -> int:
    conn.execute("UPDATE telemetry.sources SET last_sync_started = now(), last_sync_status = 'RUNNING', sync_requested_at = NULL WHERE source_id = %s",
                 (source_id,))
    sid = conn.execute("INSERT INTO telemetry.sync_log (source_id, started_by, status) VALUES (%s, %s, 'RUNNING') RETURNING sync_id",
                       (source_id, by)).fetchone()["sync_id"]
    conn.commit()
    return sid


def sync_finished(conn, source_id: int, sync_id: int, counts: dict, error: str | None, watermark: datetime | None,
                  definitions_synced: bool) -> None:
    status = "FAILED" if error else ("PARTIAL" if counts.get("warnings") else "SUCCEEDED")
    conn.execute("""UPDATE telemetry.sync_log SET finished_at = now(), status = %s, pipeline_runs = %s, task_runs = %s,
                           trigger_runs = %s, checks = %s, error = %s WHERE sync_id = %s""",
                 (status, counts.get("pipeline_runs", 0), counts.get("task_runs", 0), counts.get("trigger_runs", 0),
                  counts.get("checks", 0), error or "; ".join(counts.get("warnings", []))[:4000] or None, sync_id))
    conn.execute(f"""UPDATE telemetry.sources SET last_sync_finished = now(), last_sync_status = %s, last_sync_error = %s
                        {", last_success_at = now()" if not error else ""}
                        {", watermark = GREATEST(COALESCE(watermark, %s), %s)" if watermark else ""}
                        {", definitions_synced_at = now()" if definitions_synced else ""}
                     WHERE source_id = %s""",
                 (status, error or ("; ".join(counts.get("warnings", []))[:4000] or None),
                  *((watermark, watermark) if watermark else ()), source_id))
    conn.execute("DELETE FROM telemetry.sync_log WHERE source_id = %s AND sync_id < "
                 "(SELECT MIN(sync_id) FROM (SELECT sync_id FROM telemetry.sync_log WHERE source_id = %s ORDER BY sync_id DESC LIMIT 500) k)",
                 (source_id, source_id))
    conn.commit()


# ============================================================================ upserts
def save_definitions(conn, source_id: int, kind: str, items: list) -> None:
    names = []
    for it in items:
        props = it.get("properties") or {}
        names.append(it["name"])
        conn.execute("""INSERT INTO telemetry.definitions (source_id, kind, name, type, body, removed, synced_at)
                        VALUES (%s, %s, %s, %s, %s, false, now())
                        ON CONFLICT (source_id, kind, name) DO UPDATE SET type = EXCLUDED.type, body = EXCLUDED.body,
                            removed = false, synced_at = now()""",
                     (source_id, kind, it["name"], props.get("type"), J(it)))
    conn.execute("UPDATE telemetry.definitions SET removed = true WHERE source_id = %s AND kind = %s AND NOT (name = ANY(%s))",
                 (source_id, kind, names))


def _ts(value):
    if not value:
        return None
    v = str(value).replace("Z", "+00:00")
    m = re.match(r"^(.*T\d\d:\d\d:\d\d)(\.\d+)?(.*)$", v)          # ADF sends 7 fraction digits; Python takes 6
    if m:
        v = m.group(1) + ((m.group(2) or "")[:7]) + m.group(3)
    try:
        dt = datetime.fromisoformat(v)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def save_pipeline_run(conn, source_id: int, r: dict) -> None:
    inv = r.get("invokedBy") or {}
    conn.execute("""INSERT INTO telemetry.pipeline_runs (source_id, run_id, pipeline_name, status, run_start, run_end, duration_ms,
                        invoked_by_type, invoked_by_name, parent_run_id, run_group_id, is_latest, parameters, message, last_updated, raw)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT (run_id) DO UPDATE SET status = EXCLUDED.status, run_start = EXCLUDED.run_start,
                        run_end = EXCLUDED.run_end, duration_ms = EXCLUDED.duration_ms, is_latest = EXCLUDED.is_latest,
                        message = EXCLUDED.message, last_updated = EXCLUDED.last_updated, raw = EXCLUDED.raw,
                        parameters = EXCLUDED.parameters, invoked_by_type = EXCLUDED.invoked_by_type,
                        invoked_by_name = EXCLUDED.invoked_by_name, parent_run_id = EXCLUDED.parent_run_id""",
                 (source_id, r["runId"], r.get("pipelineName"), r.get("status"), _ts(r.get("runStart")), _ts(r.get("runEnd")),
                  r.get("durationInMs"), inv.get("invokedByType"), inv.get("name"), inv.get("pipelineRunId"),
                  r.get("runGroupId"), r.get("isLatest"), J(r.get("parameters") or {}), r.get("message") or None,
                  _ts(r.get("lastUpdated")), J(r)))


def _num(v):
    try:
        return int(v) if v is not None and v != "" else None
    except (TypeError, ValueError):
        try:
            return int(float(v))
        except (TypeError, ValueError):
            return None


def copy_metrics(activity_type: str, output) -> dict:
    """Rows / bytes / files from the activity output (Copy, Data flow, Lookup, Script)."""
    o = output if isinstance(output, dict) else {}
    m = {"rows_read": None, "rows_written": None, "rows_skipped": None, "data_read": None, "data_written": None,
         "files_read": None, "files_written": None, "throughput_kbps": None}
    if activity_type == "Copy":
        m.update(rows_read=_num(o.get("rowsRead")), rows_written=_num(o.get("rowsCopied")), rows_skipped=_num(o.get("rowsSkipped")),
                 data_read=_num(o.get("dataRead")), data_written=_num(o.get("dataWritten")), files_read=_num(o.get("filesRead")),
                 files_written=_num(o.get("filesWritten")), throughput_kbps=o.get("throughput"))
    elif activity_type == "ExecuteDataFlow":
        metrics = ((o.get("runStatus") or {}).get("metrics")) or {}
        written = [_num(v.get("rowsWritten")) for v in metrics.values() if isinstance(v, dict)]
        read = [_num(s.get("rowsRead")) for v in metrics.values() if isinstance(v, dict)
                for s in (v.get("sources") or {}).values() if isinstance(s, dict)]
        m.update(rows_written=sum(x for x in written if x) if any(written) else None,
                 rows_read=sum(x for x in read if x) if any(read) else None)
    elif activity_type == "Lookup":
        m["rows_read"] = _num(o.get("count"))
    elif activity_type == "Script":
        sets = o.get("resultSets") or []
        affected = [_num(x.get("rowsAffected")) for x in sets if isinstance(x, dict)] + [_num(o.get("rowsAffected"))]
        m["rows_written"] = sum(x for x in affected if x) if any(affected) else None
    return m


def save_task_run(conn, source_id: int, a: dict) -> None:
    err = a.get("error") or {}
    has_err = bool(err.get("message") or err.get("errorCode"))
    m = copy_metrics(a.get("activityType"), a.get("output"))
    conn.execute("""INSERT INTO telemetry.task_runs (source_id, activity_run_id, run_id, pipeline_name, activity_name, activity_type,
                        status, run_start, run_end, duration_ms, error_code, error_message, failure_type, rows_read, rows_written,
                        rows_skipped, data_read, data_written, files_read, files_written, throughput_kbps, input, output, error,
                        user_properties)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT (activity_run_id) DO UPDATE SET status = EXCLUDED.status, run_start = EXCLUDED.run_start,
                        run_end = EXCLUDED.run_end, duration_ms = EXCLUDED.duration_ms, error_code = EXCLUDED.error_code,
                        error_message = EXCLUDED.error_message, failure_type = EXCLUDED.failure_type,
                        rows_read = EXCLUDED.rows_read, rows_written = EXCLUDED.rows_written, rows_skipped = EXCLUDED.rows_skipped,
                        data_read = EXCLUDED.data_read, data_written = EXCLUDED.data_written, files_read = EXCLUDED.files_read,
                        files_written = EXCLUDED.files_written, throughput_kbps = EXCLUDED.throughput_kbps,
                        input = EXCLUDED.input, output = EXCLUDED.output, error = EXCLUDED.error,
                        user_properties = EXCLUDED.user_properties""",
                 (source_id, a["activityRunId"], a["pipelineRunId"], a.get("pipelineName"), a.get("activityName"),
                  a.get("activityType"), a.get("status"), _ts(a.get("activityRunStart")), _ts(a.get("activityRunEnd")),
                  a.get("durationInMs"), (err.get("errorCode") or None) if has_err else None,
                  (err.get("message") or None) if has_err else None, (err.get("failureType") or None) if has_err else None,
                  m["rows_read"], m["rows_written"], m["rows_skipped"], m["data_read"], m["data_written"], m["files_read"],
                  m["files_written"], m["throughput_kbps"], J(a.get("input")), J(a.get("output")), J(err if has_err else None),
                  J(a.get("userProperties"))))


def save_trigger_run(conn, source_id: int, t: dict) -> None:
    conn.execute("""INSERT INTO telemetry.trigger_runs (source_id, trigger_run_id, trigger_name, trigger_type, status, fired_at,
                        message, pipelines, properties)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT (trigger_run_id) DO UPDATE SET status = EXCLUDED.status, message = EXCLUDED.message,
                        pipelines = EXCLUDED.pipelines, properties = EXCLUDED.properties""",
                 (source_id, t.get("triggerRunId") or f"{t.get('triggerName')}|{t.get('triggerRunTimestamp')}",
                  t.get("triggerName"), t.get("triggerType"), t.get("status"), _ts(t.get("triggerRunTimestamp")),
                  t.get("message") or None, J(t.get("triggeredPipelines") or {}), J(t.get("properties") or {})))


def mark_activities_synced(conn, run_id: str) -> None:
    conn.execute("UPDATE telemetry.pipeline_runs SET activities_synced_at = now() WHERE run_id = %s", (run_id,))


def save_check(conn, source_id: int, run_id: str, activity_run_id: str, c: dict) -> None:
    conn.execute("""INSERT INTO telemetry.checks (source_id, activity_run_id, run_id, check_type, side, asset, status, expected,
                        observed, detail, checked_at)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,now())
                    ON CONFLICT (activity_run_id, check_type) DO UPDATE SET status = EXCLUDED.status, asset = EXCLUDED.asset,
                        expected = EXCLUDED.expected, observed = EXCLUDED.observed, detail = EXCLUDED.detail, checked_at = now()""",
                 (source_id, activity_run_id, run_id, c["check_type"], c["side"], c.get("asset"), c["status"],
                  None if c.get("expected") is None else str(c["expected"]),
                  None if c.get("observed") is None else str(c["observed"]), c.get("detail")))


def loads(v):
    return json.loads(v) if isinstance(v, str) else v
