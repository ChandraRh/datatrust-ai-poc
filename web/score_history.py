"""
Trust score over time: DataTrust keeps its own record of every source's score, so Observe can show how it moved and what
moved it (incidents opening and closing), instead of only the score of this moment.

    app.trust_history   one row per source per change: score, label, open incidents (all, critical, high).
                        A row is written when anything of that changes, and at least once an hour (so a flat line
                        still shows it was measured), never more often than the snapshot interval.

    snapshot_all()      every source: its incidents (this also keeps the incident lifecycle current: first seen /
                        resolved with a reason, even when nobody has the page open) and its score -> record()
    record(scope, t)    write one row if something changed (also called when someone opens Observe)
    history(scope, h)   the rows of the last h hours (plus the one before, so the line starts at the left edge) and
                        the incidents that opened or closed in that window

Runs in the web process every TRUST_HISTORY_MINUTES (default 5). TRUST_HISTORY_IN_WEB=false switches it off
(tests, a second copy of the server). Old rows go with retention (RETENTION_DAYS).
"""
from __future__ import annotations

import os
import threading
import time
from datetime import datetime, timedelta, timezone

import appdb

DDL = """
CREATE SCHEMA IF NOT EXISTS app;
CREATE TABLE IF NOT EXISTS app.trust_history (
    scope          TEXT        NOT NULL,
    recorded_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    score          INTEGER,
    label          TEXT,
    open_incidents INTEGER     NOT NULL DEFAULT 0,
    critical       INTEGER     NOT NULL DEFAULT 0,
    high           INTEGER     NOT NULL DEFAULT 0,
    PRIMARY KEY (scope, recorded_at)
);
"""
HEARTBEAT = timedelta(hours=1)          # an unchanged score is still written once an hour
MAX_HOURS = 24 * 90
state = {"running": False, "last_run": None, "last_result": None, "last_error": None}


def ensure_table() -> None:
    with appdb.connect() as conn:
        conn.execute(DDL)
        conn.commit()


def interval_seconds() -> int:
    try:
        return max(60, int(float(os.getenv("TRUST_HISTORY_MINUTES", "5")) * 60))
    except ValueError:
        return 300


# ============================================================================ write
def _open_counts(conn, scope: str) -> dict:
    r = conn.execute("""SELECT count(*) AS n, count(*) FILTER (WHERE severity = 'CRITICAL') AS critical,
                               count(*) FILTER (WHERE severity = 'HIGH') AS high
                        FROM app.incident_state WHERE scope = %s AND resolved_at IS NULL""", (scope,)).fetchone()
    return {"open_incidents": r["n"], "critical": r["critical"], "high": r["high"]}


def record(scope: str, trust: dict, now: datetime | None = None) -> bool:
    """Write the source's score if it changed (or the last row is an hour old). -> True when a row was written."""
    now = now or datetime.now(timezone.utc)
    row = {"score": trust.get("score"), "label": trust.get("label")}
    with appdb.connect() as conn:
        row.update(_open_counts(conn, scope))
        last = conn.execute("""SELECT recorded_at, score, label, open_incidents, critical, high FROM app.trust_history
                               WHERE scope = %s ORDER BY recorded_at DESC LIMIT 1""", (scope,)).fetchone()
        if last and all(last[k] == row[k] for k in row) and now - last["recorded_at"] < HEARTBEAT:
            return False
        conn.execute("""INSERT INTO app.trust_history (scope, recorded_at, score, label, open_incidents, critical, high)
                        VALUES (%(s)s, %(at)s, %(score)s, %(label)s, %(open_incidents)s, %(critical)s, %(high)s)
                        ON CONFLICT DO NOTHING""", {"s": scope, "at": now, **row})
        conn.commit()
    return True


def snapshot_all() -> dict:
    """Every source: incidents first (so the open counts are current), then its score. One source failing never stops
    the others. -> {scope: "written" | "unchanged" | "error: ..."}"""
    import scopes
    out = {}
    for s in scopes.list_scopes():
        try:
            scopes.incidents(s["key"])
            t = scopes.overview(s["key"])["trust"]
            out[s["key"]] = "written" if record(s["key"], t) else "unchanged"
        except Exception as e:                                        # noqa: BLE001  an unreachable source
            out[s["key"]] = f"error: {type(e).__name__}: {e}"[:200]
    return out


# ============================================================================ read
def _dt(v) -> datetime | None:
    """appdb.rows gives timestamps as text: back to an aware datetime."""
    if v is None or isinstance(v, datetime):
        return v
    d = datetime.fromisoformat(str(v))
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def _iso(v) -> str | None:
    v = _dt(v)
    return v.isoformat(timespec="seconds") if v else None


def history(scope: str, hours: int = 168) -> dict:
    """Score points and incident events of one source over the last `hours`."""
    hours = max(1, min(int(hours), MAX_HOURS))
    since = datetime.now(timezone.utc) - timedelta(hours=hours)
    if not appdb.table_exists("app.trust_history"):
        return {"scope": scope, "hours": hours, "since": _iso(since), "points": [], "events": []}
    import resolution
    rows = appdb.rows("""(SELECT * FROM app.trust_history WHERE scope = %(s)s AND recorded_at < %(since)s
                          ORDER BY recorded_at DESC LIMIT 1)
                         UNION ALL
                         (SELECT * FROM app.trust_history WHERE scope = %(s)s AND recorded_at >= %(since)s)
                         ORDER BY recorded_at""", {"s": scope, "since": since})
    points = [{"at": _iso(max(_dt(r["recorded_at"]), since)), "score": r["score"], "label": r["label"],
               "open": r["open_incidents"], "critical": r["critical"], "high": r["high"]} for r in rows]
    events = []
    if appdb.table_exists("app.incident_state"):
        for r in appdb.rows("""SELECT incident_id, title, severity, first_seen, resolved_at, resolution, resolution_detail
                               FROM app.incident_state
                               WHERE scope = %(s)s AND (first_seen >= %(since)s OR resolved_at >= %(since)s)
                                 AND resolution IS DISTINCT FROM 'superseded'""", {"s": scope, "since": since}):
            base = {"incident_id": r["incident_id"], "title": r["title"], "severity": r["severity"]}
            first, gone = _dt(r["first_seen"]), _dt(r["resolved_at"])
            if first and first >= since:
                events.append({**base, "kind": "opened", "at": _iso(first)})
            if gone and gone >= since:
                name, verified = resolution.label(r["resolution"])
                events.append({**base, "kind": "resolved", "at": _iso(gone), "reason": name, "verified": verified,
                               "detail": r["resolution_detail"] or ""})
    events.sort(key=lambda e: e["at"])
    return {"scope": scope, "hours": hours, "since": _iso(since), "points": points, "events": events}


# ============================================================================ background
def _loop():
    time.sleep(30)                                                    # let the server finish starting
    while True:
        try:
            state.update(last_run=datetime.now().isoformat(timespec="seconds"), last_result=snapshot_all(), last_error=None)
        except Exception as e:                                        # noqa: BLE001  never stop the loop
            state.update(last_error=f"{type(e).__name__}: {e}")
        time.sleep(interval_seconds())


def start_background() -> bool:
    if state["running"] or os.getenv("TRUST_HISTORY_IN_WEB", "true").lower() == "false":
        return False
    ensure_table()
    state["running"] = True
    threading.Thread(target=_loop, name="datatrust-trust-history", daemon=True).start()
    return True
