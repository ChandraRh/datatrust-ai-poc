"""
Scheduler: every CHECKS_POLL_SECONDS, run each check that is due (its schedule elapsed, or "Run now" was pressed).
One run per check at a time, also across processes (advisory lock in DataTrust's database).

    in the web server:   started by server.py (set CHECKS_IN_WEB=false to run it elsewhere)
    on its own:          python web/checks/runner.py          (or --once for a single round)
"""
from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path

WEB = Path(__file__).resolve().parents[1]
if str(WEB) not in sys.path:
    sys.path.insert(0, str(WEB))

import appdb  # noqa: E402
from checks import engine, store  # noqa: E402

POLL_SECONDS = max(10, int(os.getenv("CHECKS_POLL_SECONDS", "30")))
state = {"running": False, "last_tick": None, "last_error": None}


def tick(by: str = "schedule") -> list:
    done = []
    for row in store.due():
        cid = row["check_id"]
        with appdb.connect(autocommit=True) as lock:
            if not lock.execute("SELECT pg_try_advisory_lock(hashtext('datatrust-check'), %s) AS ok", (cid,)).fetchone()["ok"]:
                continue                                                 # another process is running it
            try:
                r = engine.run_and_save(cid, by)
                done.append((cid, r["status"]))
            finally:
                lock.execute("SELECT pg_advisory_unlock(hashtext('datatrust-check'), %s)", (cid,))
    return done


def _loop():
    last_prune = 0.0
    while True:
        try:
            tick()
            if time.time() - last_prune > 86400:
                store.prune()
                last_prune = time.time()
            state["last_error"] = None
        except Exception as e:                                           # noqa: BLE001  keep the loop alive
            state["last_error"] = f"{type(e).__name__}: {e}"
        state["last_tick"] = time.time()
        time.sleep(POLL_SECONDS)


def start_background() -> bool:
    if os.getenv("CHECKS_IN_WEB", "true").lower() in ("0", "false", "no", "off") or state["running"]:
        return False
    state["running"] = True
    threading.Thread(target=_loop, name="datatrust-checks", daemon=True).start()
    return True


# ============================================================================ starter checks for the POC source
def seed_starter_checks() -> int:
    """First start only: a few checks on the POC database (the demo source), so the pages are not empty.
    Includes the payment-gateway reconciliation that used to be hard-wired in the dashboard."""
    import config
    from estate import store as estate_store
    if appdb.one("SELECT count(*) AS n FROM monitor.checks")["n"]:
        return 0
    poc = next((c for c in estate_store.list_connections() if c["source_type"] == "postgresql"
                and c["host"].lower() == config.PG_HOST.lower() and c["database"] == config.PG_DATABASE), None)
    if not poc:
        return 0
    cid = poc["connection_id"]
    starters = [
        {"name": "Gateway settlement matches captured payments", "check_type": "reconciliation", "severity": "HIGH",
         "schedule_minutes": 60,
         "description": "What the payment gateway settled per business date must equal the captured payments that reached silver.",
         "config": {"match": "left", "tolerance": 0.01, "dedupe": "poc",
                    "left": {"connection_id": cid, "label": "gateway settlement",
                             "sql": "SELECT business_date, payments_count, gross_amount FROM silver.settlements"},
                    "right": {"connection_id": cid, "label": "silver payments",
                              "sql": "SELECT business_date, COUNT(*) AS payments, SUM(amount) AS amount FROM silver.payments "
                                     "WHERE status = 'CAPTURED' GROUP BY business_date"}}},
        {"name": "No rows held back by quality rules", "check_type": "custom_sql", "severity": "MEDIUM", "connection_id": cid,
         "schedule_minutes": 60, "description": "Rows the pipeline rejected never reach silver or gold.",
         "config": {"mode": "value", "op": "=", "threshold": 0, "dedupe": "poc",
                    "sql": "SELECT COUNT(*) AS rejected_rows FROM silver.rejected_records"}},
        {"name": "silver.payments loaded in the last 24 h", "check_type": "freshness", "severity": "MEDIUM", "connection_id": cid,
         "schema_name": "silver", "table_name": "payments", "schedule_minutes": 60, "config": {"column": "loaded_at", "max_age_hours": 24}},
        {"name": "payment_id is unique in silver.payments", "check_type": "uniqueness", "severity": "HIGH", "connection_id": cid,
         "schema_name": "silver", "table_name": "payments", "schedule_minutes": 360, "config": {"columns": ["payment_id"]}},
        {"name": "silver.payments row volume", "check_type": "volume", "severity": "MEDIUM", "connection_id": cid,
         "schema_name": "silver", "table_name": "payments", "schedule_minutes": 360, "config": {"min_rows": 1, "max_drop_pct": 50}},
        {"name": "silver.payments columns unchanged", "check_type": "schema", "severity": "HIGH", "connection_id": cid,
         "schema_name": "silver", "table_name": "payments", "schedule_minutes": 360, "config": {}},
        {"name": "Every payment has a currency", "check_type": "null_rate", "severity": "MEDIUM", "connection_id": cid,
         "schema_name": "silver", "table_name": "payments", "schedule_minutes": 360, "config": {"column": "currency", "max_null_pct": 0}},
    ]
    for s in starters:
        store.add_check(s, "setup", origin="starter")
    return len(starters)


if __name__ == "__main__":
    store.ensure_tables()
    if "--once" in sys.argv:
        print(tick("cli"))
    else:
        print(f"DataTrust checks: every {POLL_SECONDS} s (Ctrl+C to stop)")
        _loop()
