"""
Retention for DataTrust's own data: what grows with every run is removed once it is older than RETENTION_DAYS (.env,
default 90; 0 keeps everything). Runs in the web process once a day (the first time 10 minutes after start).

    removed after RETENTION_DAYS   monitor.results (each rule's latest result is always kept), ADF run history:
                                   pipeline / step / trigger runs and after-run checks (each pipeline's latest run is
                                   always kept), the AI answer cache (rebuilt on the next button press),
                                   the trust score history (app.trust_history)
    removed after 30 days          telemetry.sync_log (when the collector read the factory)
    removed once expired           portal.auth_tokens (sign-in and e-mail verification links)
    kept forever                   portal.audit_log, portal.access_requests, app.remediation_log (the audit trail),
                                   app.incident_state (Verify's resolved history), app.alert_log (removing an entry
                                   could send an old alert again), rules, connections, users, settings

Every delete is one statement in its own transaction; a table that does not exist yet is skipped. When anything was
removed, one line goes to the audit log.
"""
from __future__ import annotations

import os
import threading
import time
from datetime import datetime, timedelta

import appdb

SYNC_LOG_DAYS = 30
FIRST_RUN_SECONDS = 600
EVERY_SECONDS = 24 * 3600
state = {"running": False, "last_run": None, "last_result": None, "last_error": None}

# (table, what is removed). %(cut)s = the cutoff; each keeps what the pages still need
STEPS = [
    ("monitor.results", """FROM monitor.results r WHERE r.run_at < %(cut)s
        AND r.result_id NOT IN (SELECT max(result_id) FROM monitor.results GROUP BY check_id)"""),
    ("telemetry.pipeline_runs", """FROM telemetry.pipeline_runs p WHERE p.run_start < %(cut)s
        AND (p.source_id, p.run_id) NOT IN (SELECT DISTINCT ON (source_id, pipeline_name) source_id, run_id
                                            FROM telemetry.pipeline_runs ORDER BY source_id, pipeline_name, run_start DESC)"""),
    ("telemetry.task_runs", """FROM telemetry.task_runs t WHERE t.run_start < %(cut)s
        AND NOT EXISTS (SELECT 1 FROM telemetry.pipeline_runs p WHERE p.source_id = t.source_id AND p.run_id = t.run_id)"""),
    ("telemetry.checks", """FROM telemetry.checks c WHERE c.checked_at < %(cut)s
        AND NOT EXISTS (SELECT 1 FROM telemetry.pipeline_runs p WHERE p.source_id = c.source_id AND p.run_id = c.run_id)"""),
    ("telemetry.trigger_runs", "FROM telemetry.trigger_runs WHERE fired_at < %(cut)s"),
    ("app.ai_notes", "FROM app.ai_notes WHERE created_at < %(cut)s"),
    ("app.trust_history", "FROM app.trust_history WHERE recorded_at < %(cut)s"),
    ("telemetry.sync_log", "FROM telemetry.sync_log WHERE started_at < %(sync_cut)s"),
    ("portal.auth_tokens", "FROM portal.auth_tokens WHERE expires_at < %(now)s - interval '1 day'"),
]


def days() -> int:
    try:
        return max(0, int(os.getenv("RETENTION_DAYS", "90")))
    except ValueError:
        return 90


def prune(keep_days: int | None = None, dry_run: bool = False, now: datetime | None = None) -> dict:
    """Remove (or with dry_run only count) what is older than keep_days. -> {table: rows}."""
    keep_days = days() if keep_days is None else keep_days
    if keep_days <= 0:
        return {}
    now = now or datetime.now()
    params = {"cut": now - timedelta(days=keep_days), "sync_cut": now - timedelta(days=min(SYNC_LOG_DAYS, keep_days)), "now": now}
    out = {}
    for table, body in STEPS:
        if not appdb.table_exists(table):
            continue
        with appdb.connect() as conn:                       # the same "FROM ... WHERE ..." counts or deletes
            if dry_run:
                out[table] = conn.execute("SELECT count(*) AS n " + body, params).fetchone()["n"]
            else:
                out[table] = conn.execute("DELETE " + body, params).rowcount
                conn.commit()
    removed = {t: n for t, n in out.items() if n}
    if removed and not dry_run:
        try:
            from estate import store
            store.audit("datatrust", "RETENTION", f"older than {keep_days} days",
                        ", ".join(f"{t}: {n}" for t, n in removed.items()))
        except Exception:                                                     # noqa: BLE001  the cleanup itself succeeded
            pass
    return out


def _loop():
    time.sleep(FIRST_RUN_SECONDS)
    while True:
        try:
            state.update(last_run=datetime.now().isoformat(timespec="seconds"), last_result=prune(), last_error=None)
        except Exception as e:                                                # noqa: BLE001  never stop the loop
            state.update(last_error=f"{type(e).__name__}: {e}")
        time.sleep(EVERY_SECONDS)


def start_background() -> bool:
    """Once a day in the web process. RETENTION_IN_WEB=false (tests, a second copy) or RETENTION_DAYS=0 switch it off."""
    if state["running"] or days() <= 0 or os.getenv("RETENTION_IN_WEB", "true").lower() == "false":
        return False
    state["running"] = True
    threading.Thread(target=_loop, name="datatrust-retention", daemon=True).start()
    return True
