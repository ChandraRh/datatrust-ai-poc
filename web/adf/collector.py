"""
ADF collector: one sync reads a factory and stores everything in telemetry.*; the loop repeats it.

A sync, for one factory:
  1. definitions   pipelines, datasets, linked services, triggers (every ADF_DEFINITIONS_MINUTES, or when forced)
  2. pipeline runs updated since the last sync (15 minutes overlap, so late updates are never missed; first sync
                   reads the last `lookback_days`, ADF keeps 45 days)
  3. activity runs of every pipeline run from step 2 (the steps, errors, rows, bytes, input/output)
  4. trigger runs  (schedule / tumbling window / storage events, including triggers that failed to start a pipeline)
  5. checks        source files and target table of the Copy activities of the newest runs (probes.py)
Everything is an upsert keyed on ADF's own IDs, so repeating a sync is harmless.

    python web/adf/collector.py            poll every ADF_POLL_SECONDS (default 300) + whenever a sync is requested
    python web/adf/collector.py --once     sync every enabled factory once and exit
The web server runs the same loop in a thread (ADF_COLLECTOR_IN_WEB=true, the default).
"""
from __future__ import annotations

import os
import sys
import threading
import time
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path

WEB = Path(__file__).resolve().parent.parent
if str(WEB) not in sys.path:
    sys.path.insert(0, str(WEB))

from adf import probes, resolve, store  # noqa: E402
from adf.client import AdfClient, AdfError, credential_for  # noqa: E402

POLL_SECONDS = int(os.getenv("ADF_POLL_SECONDS", "300"))
DEFINITIONS_MINUTES = int(os.getenv("ADF_DEFINITIONS_MINUTES", "10"))
OVERLAP = timedelta(minutes=15)
PROBE_RUNS_PER_PIPELINE = 3
IN_PROGRESS = ("Queued", "InProgress", "Cancelling")


def explain_azure_error(e: Exception, cfg: dict) -> str:
    if isinstance(e, AdfError):
        if e.status in (401, 403):
            who = f"app registration {cfg.get('client_id')}" if cfg.get("auth_mode") == "service_principal" else "the identity DataTrust runs as"
            return (f"Azure refused access ({e.status} {e.code}). Give {who} read access on the factory "
                    f"'{cfg.get('factory_name')}' (role 'Data Factory Contributor', or the read-only custom role in "
                    f"docs/ADF_CONNECTOR.md). Azure said: {e.message}")
        if e.status == 404:
            return (f"Factory not found (404 {e.code}): check subscription {cfg.get('subscription_id')}, resource group "
                    f"'{cfg.get('resource_group')}' and factory name '{cfg.get('factory_name')}'. Azure said: {e.message}")
        return str(e)
    name = type(e).__name__
    if name in ("ClientAuthenticationError", "CredentialUnavailableError"):
        return (f"Could not sign in to Azure ({name}). Service principal: check tenant/client ID and that the secret "
                f"variable is set. Default mode: run `az login` on this machine or use a managed identity. Details: {e}")
    return f"{name}: {e}"


def _definitions(conn, source_id: int) -> dict:
    out = {"pipeline": {}, "dataset": {}, "linkedservice": {}, "trigger": {}}
    for r in conn.execute("SELECT kind, name, body FROM telemetry.definitions WHERE source_id = %s AND NOT removed", (source_id,)):
        out.setdefault(r["kind"], {})[r["name"]] = store.loads(r["body"])
    return out


def _activity_def(defs: dict, pipeline_name: str, activity_name: str) -> dict | None:
    p = defs["pipeline"].get(pipeline_name)
    if not p:
        return None
    acts = resolve.flatten_activities((p.get("properties") or {}).get("activities"))
    return next((a for a in acts if a["name"] == activity_name), None)


def probe_copy(conn, src: dict, defs: dict, run: dict, task: dict, compare_rows: bool, storage_cache: dict) -> int:
    """Source + target checks for one Copy activity run. Returns how many checks were saved."""
    cfg = src["config"]
    adef = _activity_def(defs, run["pipeline_name"], task["activity_name"])
    if not adef:
        return 0
    ctx = resolve.run_context(run, run["pipeline_name"], cfg.get("factory_name"))
    assets = resolve.copy_assets(adef, defs["dataset"], defs["linkedservice"], ctx)
    checks, columns = [], []
    cred = storage_cache.setdefault("_cred", [])

    def credential():
        if not cred:
            cred.append(credential_for(cfg))
        return cred[0]

    def client_for(account):
        if not account:
            return None
        if account not in storage_cache:
            storage_cache[account] = probes.storage_client(account, cfg, credential)
        return storage_cache[account]

    s = assets.get("source")
    if s and s.get("kind") == "storage":
        if s.get("unresolved") and any(x in s["unresolved"] for x in ("container", "folder", "file name")):
            checks.append(probes._check("SOURCE_FILES", "source", s.get("name"), "UNKNOWN", None, None,
                                        f"Path depends on values only known inside the run ({', '.join(s['unresolved'])}); not checked."))
        elif s.get("account") and s.get("container"):
            try:
                c, columns = probes.source_checks(s, task, client_for(s["account"]))
                checks += c
            except Exception as e:                                       # noqa: BLE001
                checks.append(probes._check("SOURCE_FILES", "source", s.get("name"), "UNKNOWN", None, None,
                                            f"Could not read storage account {s['account']}: {type(e).__name__}: {str(e)[:300]}. "
                                            f"Give DataTrust's identity 'Storage Blob Data Reader' on it, or set storage_connection_env."))
    skipped = probes.skipped_rows_check(task, lambda acct: client_for(acct or (s or {}).get("account")))
    if skipped:
        skipped["asset"] = (assets.get("target") or {}).get("name")
        checks.append(skipped)
    t = assets.get("target")
    if t and t.get("kind") == "postgres":
        mapping = [m.get("sink", {}).get("name") for m in ((adef.get("translator") or {}).get("mappings") or [])
                   if isinstance(m, dict) and (m.get("sink") or {}).get("name")]
        try:
            checks += probes.target_checks(t, task, cfg, columns, mapping or None, compare_rows)
        except Exception as e:                                           # noqa: BLE001
            checks.append(probes._check("TARGET_TABLE", "target", t.get("name"), "UNKNOWN", None, None,
                                        f"Could not query the target: {type(e).__name__}: {str(e)[:300]}"))
    for c in checks:
        store.save_check(conn, src["source_id"], run["run_id"], task["activity_run_id"], c)
    conn.commit()
    return len(checks)


def sync(source_id: int, started_by: str = "poll", client=None, force_definitions: bool = False) -> dict:
    src = store.get_source(source_id)
    cfg = src["config"]
    counts = {"pipeline_runs": 0, "task_runs": 0, "trigger_runs": 0, "checks": 0, "warnings": []}
    with store.connect() as conn:
        if not store.try_lock(conn, source_id):
            return {"skipped": "a sync of this factory is already running"}
        try:
            sync_id = store.sync_started(conn, source_id, started_by)
            error, watermark, defs_done = None, None, False
            try:
                client = client or AdfClient(cfg)
                due = (force_definitions or not src["definitions_synced_at"]
                       or datetime.now(timezone.utc) - src["definitions_synced_at"] > timedelta(minutes=DEFINITIONS_MINUTES))
                if due:
                    for kind, fetch in (("pipeline", client.pipelines), ("dataset", client.datasets),
                                        ("linkedservice", client.linked_services), ("trigger", client.triggers)):
                        try:
                            store.save_definitions(conn, source_id, kind, fetch())
                        except AdfError as e:
                            if kind == "pipeline" or e.status not in (403, 404):
                                raise
                            counts["warnings"].append(f"cannot read {kind}s ({e.code})")
                    conn.commit()
                    defs_done = True

                now = datetime.now(timezone.utc)
                after = (src["watermark"] - OVERLAP) if src["watermark"] else now - timedelta(days=cfg.get("lookback_days") or 30)
                before = now + timedelta(minutes=1)
                runs = client.pipeline_runs(after, before)
                for r in runs:
                    store.save_pipeline_run(conn, source_id, r)
                    lu = store._ts(r.get("lastUpdated"))
                    watermark = max(watermark, lu) if watermark and lu else (lu or watermark)
                conn.commit()
                counts["pipeline_runs"] = len(runs)

                pending = {r["runId"]: r for r in runs}
                # runs still in progress at the previous sync are re-read even when ADF did not report them again
                for row in conn.execute("""SELECT raw FROM telemetry.pipeline_runs WHERE source_id = %s
                                           AND (status = ANY(%s) OR activities_synced_at IS NULL) AND run_start > now() - interval '45 days'""",
                                        (source_id, list(IN_PROGRESS))).fetchall():
                    raw = store.loads(row["raw"])
                    pending.setdefault(raw["runId"], raw)
                for run_id, r in pending.items():
                    start = store._ts(r.get("runStart")) or now
                    end = store._ts(r.get("runEnd")) or now
                    try:
                        acts = client.activity_runs(run_id, start - timedelta(days=1), max(end, now) + timedelta(days=1))
                    except AdfError as e:
                        if e.status in (403, 404):
                            counts["warnings"].append(f"cannot read activity runs ({e.code})")
                            break
                        raise
                    for a in acts:
                        store.save_task_run(conn, source_id, a)
                    counts["task_runs"] += len(acts)
                    if r.get("status") not in IN_PROGRESS:
                        store.mark_activities_synced(conn, run_id)
                    conn.commit()

                try:
                    trig = client.trigger_runs(after, before)
                    for t in trig:
                        store.save_trigger_run(conn, source_id, t)
                    conn.commit()
                    counts["trigger_runs"] = len(trig)
                except AdfError as e:
                    if e.status not in (403, 404):
                        raise
                    counts["warnings"].append(f"cannot read trigger runs ({e.code})")

                counts["checks"] = run_probes(conn, src, set(pending))
            except Exception as e:                                       # noqa: BLE001
                conn.rollback()
                error = explain_azure_error(e, cfg)
                if not isinstance(e, (AdfError, ValueError)):
                    traceback.print_exc()
            store.sync_finished(conn, source_id, sync_id, counts, error, watermark, defs_done)
            return {"ok": not error, "error": error, **counts}
        finally:
            store.unlock(conn, source_id)


def run_probes(conn, src: dict, new_run_ids: set) -> int:
    """Checks for the Copy activities of each pipeline's newest completed runs (always re-checked: tables change) and of
    every run collected in this sync."""
    source_id = src["source_id"]
    defs = _definitions(conn, source_id)
    latest = conn.execute("""SELECT run_id FROM (
                                SELECT run_id, row_number() OVER (PARTITION BY pipeline_name ORDER BY run_start DESC) AS n
                                FROM telemetry.pipeline_runs WHERE source_id = %s AND status NOT IN ('Queued','InProgress','Cancelling')) x
                             WHERE n <= %s""", (source_id, PROBE_RUNS_PER_PIPELINE)).fetchall()
    run_ids = {r["run_id"] for r in latest} | set(new_run_ids)
    if not run_ids:
        return 0
    # the newest successful load of each Copy step: the target must still hold at least what it wrote
    newest_success = {r["activity_run_id"] for r in conn.execute(
        """SELECT DISTINCT ON (pipeline_name, activity_name) activity_run_id FROM telemetry.task_runs
           WHERE source_id = %s AND activity_type = 'Copy' AND status = 'Succeeded'
           ORDER BY pipeline_name, activity_name, run_start DESC""", (source_id,)).fetchall()}
    runs = {r["run_id"]: r for r in conn.execute("SELECT * FROM telemetry.pipeline_runs WHERE run_id = ANY(%s)", (list(run_ids),))}
    tasks = conn.execute("""SELECT * FROM telemetry.task_runs WHERE run_id = ANY(%s) AND activity_type = 'Copy'
                            AND status IN ('Succeeded','Failed') ORDER BY run_start DESC LIMIT 40""", (list(run_ids),)).fetchall()
    total, cache = 0, {}
    for t in tasks:
        run = runs.get(t["run_id"])
        if not run:
            continue
        try:
            total += probe_copy(conn, src, defs, run, t, t["activity_run_id"] in newest_success, cache)
        except Exception as e:                                           # noqa: BLE001
            conn.rollback()
            print(f"!! ADF check failed for {t['activity_name']}: {type(e).__name__}: {e}")
    return total


# ============================================================================ loop
def due_sources() -> list:
    with store.connect() as conn:
        return conn.execute("""SELECT source_id, name, sync_requested_at IS NOT NULL AS requested FROM telemetry.sources
                               WHERE kind = %s AND enabled AND (sync_requested_at IS NOT NULL OR last_sync_started IS NULL
                                     OR last_sync_started < now() - make_interval(secs => %s))
                               ORDER BY source_id""", (store.KIND, POLL_SECONDS)).fetchall()


def tick() -> None:
    for s in due_sources():
        result = sync(s["source_id"], "request" if s["requested"] else "poll")
        if result.get("error"):
            print(f"!! ADF sync '{s['name']}' failed: {result['error'][:300]}")


_thread = None


def start_background() -> bool:
    """Start the collector loop in a daemon thread (once per process)."""
    global _thread
    if _thread or os.getenv("ADF_COLLECTOR_IN_WEB", "true").lower() == "false":
        return False

    def loop():
        while True:
            try:
                tick()
            except Exception as e:                                       # noqa: BLE001
                print(f"!! ADF collector: {type(e).__name__}: {e}")
            time.sleep(15)
    _thread = threading.Thread(target=loop, name="adf-collector", daemon=True)
    _thread.start()
    return True


if __name__ == "__main__":
    store.ensure_tables()
    added = store.bootstrap_from_env()
    if added:
        print(f"added factory from .env (source {added})")
    if "--once" in sys.argv:
        for s in store.list_sources(include_disabled=False):
            print(s["name"], sync(s["source_id"], "cli", force_definitions=True))
    else:
        print(f"ADF collector: checking every 15 s, syncing each factory every {POLL_SECONDS} s (Ctrl+C to stop)")
        while True:
            try:
                tick()
            except Exception as e:                                       # noqa: BLE001
                print(f"!! {type(e).__name__}: {e}")
            time.sleep(15)
