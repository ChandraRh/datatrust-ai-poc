"""
monitor.* in DataTrust's own database: what to check (checks) and every outcome (results).
"""
from __future__ import annotations

import re

from psycopg.types.json import Jsonb

import appdb

TYPES = ("freshness", "volume", "null_rate", "uniqueness", "schema", "custom_sql", "reconciliation")
SEVERITIES = ("LOW", "MEDIUM", "HIGH", "CRITICAL")
TABLE_TYPES = ("freshness", "volume", "null_rate", "uniqueness", "schema")
KEEP_DAYS = 90

DDL = """
CREATE SCHEMA IF NOT EXISTS monitor;
CREATE TABLE IF NOT EXISTS monitor.checks (
    check_id         INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name             TEXT NOT NULL,
    check_type       TEXT NOT NULL,
    connection_id    INTEGER,                     -- portal.connections (the database checked)
    database         TEXT,                        -- NULL = the connection's default database
    schema_name      TEXT,
    table_name       TEXT,
    config           JSONB NOT NULL DEFAULT '{}'::jsonb,
    state            JSONB NOT NULL DEFAULT '{}'::jsonb,   -- e.g. the accepted schema of a schema check
    severity         TEXT NOT NULL DEFAULT 'MEDIUM',
    schedule_minutes INTEGER NOT NULL DEFAULT 60,
    enabled          BOOLEAN NOT NULL DEFAULT true,
    origin           TEXT NOT NULL DEFAULT 'manual',        -- manual / starter / suggested
    description      TEXT,
    created_by       TEXT,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_run_at      TIMESTAMPTZ,
    last_status      TEXT,
    run_requested_at TIMESTAMPTZ
);
CREATE TABLE IF NOT EXISTS monitor.results (
    result_id    BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    check_id     INTEGER NOT NULL REFERENCES monitor.checks ON DELETE CASCADE,
    run_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    status       TEXT NOT NULL,                 -- PASS / WARN / FAIL / ERROR
    observed     DOUBLE PRECISION,              -- the number the check measured (rows, hours, %, mismatches ...)
    expected     TEXT,                          -- what it should be, in words
    message      TEXT,
    details      JSONB,                         -- evidence: sample rows, duplicate keys, mismatches, schema diff
    sql_text     TEXT,                          -- exactly what ran
    duration_ms  INTEGER,
    triggered_by TEXT
);
CREATE INDEX IF NOT EXISTS results_by_check ON monitor.results (check_id, run_at DESC);
-- when a person changed what the rule measures (updated_at also moves when DataTrust learns a baseline):
-- an incident that disappears after either is not counted as fixed (web/resolution.py)
ALTER TABLE monitor.checks ADD COLUMN IF NOT EXISTS edited_at TIMESTAMPTZ;
ALTER TABLE monitor.checks ADD COLUMN IF NOT EXISTS baseline_accepted_at TIMESTAMPTZ;
"""


def ensure_tables() -> None:
    with appdb.connect() as conn:
        conn.execute(DDL)
        conn.commit()


def J(v):
    return Jsonb(v) if v is not None else None


def validate(body: dict) -> dict:
    """Keep known fields and check their shape. The SQL itself is validated by the engine on preview / run."""
    t = body.get("check_type")
    if t not in TYPES:
        raise ValueError(f"check_type must be one of {', '.join(TYPES)}")
    name = (body.get("name") or "").strip()
    if not name or len(name) > 120:
        raise ValueError("name is required (up to 120 characters)")
    sev = (body.get("severity") or "MEDIUM").upper()
    if sev not in SEVERITIES:
        raise ValueError(f"severity must be one of {', '.join(SEVERITIES)}")
    minutes = int(body.get("schedule_minutes") or 60)
    if not 5 <= minutes <= 10080:
        raise ValueError("schedule must be between 5 minutes and 7 days")
    out = {"name": name, "check_type": t, "severity": sev, "schedule_minutes": minutes,
           "enabled": bool(body.get("enabled", True)), "description": (body.get("description") or "").strip()[:500] or None,
           "config": dict(body.get("config") or {}), "connection_id": None, "database": None, "schema_name": None, "table_name": None}
    if t != "reconciliation":
        if not str(body.get("connection_id") or "").isdigit():
            raise ValueError("choose the connection to check")
        out["connection_id"] = int(body["connection_id"])
        out["database"] = (body.get("database") or "").strip() or None
    if t in TABLE_TYPES:
        for k in ("schema_name", "table_name"):
            v = (body.get(k) or "").strip()
            if not v:
                raise ValueError("choose the schema and table to check")
            out[k] = v
    c = out["config"]
    if t in ("freshness", "null_rate") and not (c.get("column") or "").strip():
        raise ValueError("choose the column")
    if t == "uniqueness":
        cols = c.get("columns")
        if isinstance(cols, str):
            cols = [x.strip() for x in re.split(r"[,\s]+", cols) if x.strip()]
        if not cols:
            raise ValueError("choose the key column(s)")
        c["columns"] = cols
    if t == "custom_sql" and not (c.get("sql") or "").strip():
        raise ValueError("write the SQL query")
    if t == "reconciliation":
        for side in ("left", "right"):
            s = c.get(side) or {}
            if not str(s.get("connection_id") or "").isdigit() or not (s.get("sql") or "").strip():
                raise ValueError(f"the {side} side needs a connection and a query")
            s["connection_id"] = int(s["connection_id"])
    return out


# ============================================================================ definitions
def list_checks(connection_id: int | None = None) -> list:
    return appdb.rows("""
        SELECT c.*, cn.name AS connection_name, cn.source_type,
               r.observed, r.message, r.expected, r.run_at AS last_result_at,
               (SELECT array_agg(status ORDER BY run_at) FROM (SELECT status, run_at FROM monitor.results x
                  WHERE x.check_id = c.check_id ORDER BY run_at DESC LIMIT 20) h) AS history
        FROM monitor.checks c
        LEFT JOIN portal.connections cn ON cn.connection_id = c.connection_id
        LEFT JOIN LATERAL (SELECT observed, message, expected, run_at FROM monitor.results x
                           WHERE x.check_id = c.check_id ORDER BY run_at DESC LIMIT 1) r ON true
        WHERE %(c)s::int IS NULL OR c.connection_id = %(c)s::int
              OR (c.check_type = 'reconciliation' AND (c.config->'left'->>'connection_id')::int = %(c)s::int)
        ORDER BY CASE c.last_status WHEN 'FAIL' THEN 0 WHEN 'ERROR' THEN 1 WHEN 'WARN' THEN 2 ELSE 3 END, c.name""",
        {"c": connection_id})


def get_check(check_id: int) -> dict:
    with appdb.connect() as conn:
        row = conn.execute("""SELECT c.*, cn.name AS connection_name, cn.source_type FROM monitor.checks c
                              LEFT JOIN portal.connections cn ON cn.connection_id = c.connection_id
                              WHERE c.check_id = %s""", (check_id,)).fetchone()
    if not row:
        raise ValueError(f"check {check_id} not found")
    return row


def add_check(body: dict, by: str, origin: str = "manual") -> int:
    v = validate(body)
    with appdb.connect() as conn:
        cid = conn.execute("""INSERT INTO monitor.checks (name, check_type, connection_id, database, schema_name, table_name, config,
                                   severity, schedule_minutes, enabled, origin, description, created_by, run_requested_at)
                              VALUES (%(name)s, %(check_type)s, %(connection_id)s, %(database)s, %(schema_name)s, %(table_name)s,
                                      %(config)s, %(severity)s, %(schedule_minutes)s, %(enabled)s, %(origin)s, %(description)s,
                                      %(by)s, now())
                              RETURNING check_id""", {**v, "config": J(v["config"]), "origin": origin, "by": by}).fetchone()["check_id"]
        conn.commit()
    return cid


def update_check(check_id: int, body: dict) -> None:
    old = get_check(check_id)
    v = validate(body)
    keep_state = old["check_type"] == v["check_type"] and (old["schema_name"], old["table_name"], old["connection_id"]) == \
        (v["schema_name"], v["table_name"], v["connection_id"])
    with appdb.connect() as conn:
        conn.execute("""UPDATE monitor.checks SET name = %(name)s, check_type = %(check_type)s, connection_id = %(connection_id)s,
                               database = %(database)s, schema_name = %(schema_name)s, table_name = %(table_name)s, config = %(config)s,
                               severity = %(severity)s, schedule_minutes = %(schedule_minutes)s, enabled = %(enabled)s,
                               description = %(description)s, updated_at = now(), edited_at = now(), run_requested_at = now(),
                               state = CASE WHEN %(keep)s THEN state ELSE '{}'::jsonb END
                        WHERE check_id = %(id)s""", {**v, "config": J(v["config"]), "keep": keep_state, "id": check_id})
        conn.commit()


def delete_check(check_id: int) -> None:
    with appdb.connect() as conn:
        conn.execute("DELETE FROM monitor.checks WHERE check_id = %s", (check_id,))
        conn.commit()


def set_state(check_id: int, state: dict, accepted: bool = False) -> None:
    """accepted: a person accepted the current schema as the new baseline (not DataTrust learning it)."""
    with appdb.connect() as conn:
        conn.execute("""UPDATE monitor.checks SET state = %s, updated_at = now(),
                               baseline_accepted_at = CASE WHEN %s THEN now() ELSE baseline_accepted_at END
                        WHERE check_id = %s""", (J(state), accepted, check_id))
        conn.commit()


def request_run(check_id: int) -> None:
    with appdb.connect() as conn:
        conn.execute("UPDATE monitor.checks SET run_requested_at = now() WHERE check_id = %s", (check_id,))
        conn.commit()


# ============================================================================ results
def save_result(check_id: int, r: dict, by: str) -> None:
    with appdb.connect() as conn:
        conn.execute("""INSERT INTO monitor.results (check_id, status, observed, expected, message, details, sql_text, duration_ms,
                                                     triggered_by)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                     (check_id, r["status"], r.get("observed"), r.get("expected"), r.get("message"), J(r.get("details")),
                      r.get("sql"), r.get("duration_ms"), by))
        conn.execute("""UPDATE monitor.checks SET last_run_at = now(), last_status = %s, run_requested_at = NULL
                        WHERE check_id = %s""", (r["status"], check_id))
        conn.commit()


def history(check_id: int, limit: int = 50) -> list:
    return appdb.rows("""SELECT result_id, run_at, status, observed, expected, message, details, sql_text, duration_ms, triggered_by
                         FROM monitor.results WHERE check_id = %s ORDER BY run_at DESC LIMIT %s""", (check_id, limit))


def usual_values(check_id: int, n: int = 14) -> list:
    """Observed values of recent runs that were not failures: the 'usual' a volume check compares against."""
    return [r["observed"] for r in appdb.rows(
        """SELECT observed FROM monitor.results WHERE check_id = %s AND status IN ('PASS', 'WARN') AND observed IS NOT NULL
           ORDER BY run_at DESC LIMIT %s""", (check_id, n))]


def due(limit: int = 20) -> list:
    return appdb.rows("""SELECT check_id FROM monitor.checks
                         WHERE run_requested_at IS NOT NULL
                            OR (enabled AND (last_run_at IS NULL OR last_run_at + make_interval(mins => schedule_minutes) <= now()))
                         ORDER BY run_requested_at NULLS LAST, last_run_at NULLS FIRST LIMIT %s""", (limit,))


def prune() -> None:
    with appdb.connect() as conn:
        conn.execute("DELETE FROM monitor.results WHERE run_at < now() - make_interval(days => %s)", (KEEP_DAYS,))
        conn.commit()


def summary() -> dict:
    rows = appdb.rows("""SELECT COALESCE(last_status, 'NOT RUN') AS status, count(*) AS n FROM monitor.checks
                         WHERE enabled GROUP BY 1""")
    return {r["status"]: r["n"] for r in rows}
