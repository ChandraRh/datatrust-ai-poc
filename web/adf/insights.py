"""
What the Azure Data Factory pages show, computed from telemetry.* on every request (nothing cached, nothing invented).

    sources()                     connected factories and how the connector is doing (last sync, errors)
    overview(source_id)           KPIs, runs per day, every pipeline with its health and trust score
    issues(source_id)             open problems, most severe first, each with cause, evidence and what to do
    runs(...)                     run history with filters
    run_detail(run_id)            one run: steps (timeline), root cause, source/target checks, lineage, parameters,
                                  comparison with the pipeline's usual duration and volume, parent/child runs
    pipeline_detail(sid, name)    definition (steps and their dependencies), triggers, lineage, recent runs, trends

Issue rules (each is resolved automatically when the condition is gone):
    RUN_FAILED       newest finished run failed (CRITICAL after 3 failures in a row)
    RUN_CANCELLED    newest finished run was cancelled
    STALE            no successful run for 2x the trigger's interval (+30 min), or the factory's freshness_hours
    TRIGGER_STOPPED  a trigger for this pipeline exists but is stopped
    TRIGGER_FAILED   a trigger failed to start the pipeline in the last 24 h
    LONG_RUNNING     a run has been in progress for 3x its usual duration (2 h when there is no history)
    ROWS_SKIPPED     the newest successful run skipped incompatible rows
    VOLUME           rows loaded by the newest successful run: 0 or < 50% of the median of earlier runs
    SLOW             the newest successful run took > 2x the median duration (and 5+ minutes more)
    CHECK_FAILED     a source/target check of the newest run failed (FAIL) or found a difference (WARN)
    CONNECTOR        DataTrust cannot read the factory (last sync failed)
"""
from __future__ import annotations

import statistics
from datetime import datetime, timezone

import data_service as ds

from . import diagnose, resolve, store

SEV_ORDER = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3}
SEV_PENALTY = {"CRITICAL": 45, "HIGH": 30, "MEDIUM": 12, "LOW": 4}
FREQ_MINUTES = {"Minute": 1, "Hour": 60, "Day": 1440, "Week": 10080, "Month": 43200}
FINISHED = ("Succeeded", "Failed", "Cancelled")


def _j(v):
    return store.loads(v) if v is not None else None


def _dt(v):
    if not v:
        return None
    return resolve._as_dt(v)


def _short(v) -> str:
    d = _dt(v)
    return d.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC") if d else "?"


def _age_hours(v) -> float | None:
    d = _dt(v)
    return (datetime.now(timezone.utc) - d).total_seconds() / 3600 if d else None


# ============================================================================ sources
def sources() -> list:
    rows = ds.rows("""SELECT s.source_id, s.name, s.kind, s.config, s.enabled, s.created_by, s.created_at, s.watermark,
                             s.definitions_synced_at, s.last_sync_started, s.last_sync_finished, s.last_sync_status,
                             s.last_sync_error, s.last_success_at, s.sync_requested_at, s.webhook_token_hash IS NOT NULL AS has_webhook,
                             (SELECT count(*) FROM telemetry.definitions d WHERE d.source_id = s.source_id AND d.kind = 'pipeline' AND NOT d.removed) AS pipelines,
                             (SELECT count(*) FROM telemetry.pipeline_runs r WHERE r.source_id = s.source_id) AS runs
                      FROM telemetry.sources s WHERE s.kind = %s ORDER BY s.source_id""", (store.KIND,))
    for r in rows:
        r["config"] = _j(r["config"])
    return rows


def sync_log(source_id: int, limit: int = 30) -> list:
    return ds.rows("""SELECT sync_id, started_by, started_at, finished_at, status, pipeline_runs, task_runs, trigger_runs, checks, error
                      FROM telemetry.sync_log WHERE source_id = %s ORDER BY sync_id DESC LIMIT %s""", (source_id, limit))


# ============================================================================ shared reads
def _definitions(source_id: int) -> dict:
    out = {"pipeline": {}, "dataset": {}, "linkedservice": {}, "trigger": {}}
    for r in ds.rows("SELECT kind, name, body FROM telemetry.definitions WHERE source_id = %s AND NOT removed", (source_id,)):
        out.setdefault(r["kind"], {})[r["name"]] = _j(r["body"])
    return out


def _run_stats_sql(where: str) -> str:
    return f"""SELECT r.source_id, r.run_id, r.pipeline_name, r.status, r.run_start, r.run_end, r.duration_ms, r.invoked_by_type,
                      r.invoked_by_name, r.parent_run_id, r.message, r.parameters,
                      t.rows_read, t.rows_written, t.rows_skipped, t.data_read, t.data_written, t.steps, t.steps_failed
               FROM telemetry.pipeline_runs r
               LEFT JOIN LATERAL (
                   SELECT sum(rows_read) FILTER (WHERE activity_type IN ('Copy','ExecuteDataFlow')) AS rows_read,
                          sum(rows_written) FILTER (WHERE activity_type IN ('Copy','ExecuteDataFlow')) AS rows_written,
                          sum(rows_skipped) AS rows_skipped, sum(data_read) AS data_read, sum(data_written) AS data_written,
                          count(*) AS steps, count(*) FILTER (WHERE status = 'Failed') AS steps_failed
                   FROM telemetry.task_runs k WHERE k.run_id = r.run_id) t ON true
               WHERE {where}"""


def _triggers_for(defs: dict, pipeline: str) -> list:
    out = []
    for name, t in defs["trigger"].items():
        p = t.get("properties") or {}
        refs = [x.get("pipelineReference", {}).get("referenceName") for x in p.get("pipelines") or []]
        if p.get("pipeline"):
            refs.append((p["pipeline"].get("pipelineReference") or {}).get("referenceName"))
        if pipeline not in refs:
            continue
        tp = p.get("typeProperties") or {}
        rec = tp.get("recurrence") or tp
        minutes = None
        if rec.get("frequency") in FREQ_MINUTES:
            minutes = FREQ_MINUTES[rec["frequency"]] * int(rec.get("interval") or 1)
        desc = {"ScheduleTrigger": "Schedule", "TumblingWindowTrigger": "Tumbling window", "BlobEventsTrigger": "Storage event",
                "CustomEventsTrigger": "Custom event"}.get(p.get("type"), p.get("type"))
        when = f"every {rec.get('interval') or 1} {str(rec.get('frequency', '')).lower()}(s)" if minutes else ""
        if p.get("type") == "BlobEventsTrigger":
            when = f"when a file is {'/'.join(e.split('.')[-1].replace('Blob', '').lower() for e in tp.get('events') or [])} under {tp.get('blobPathBeginsWith') or '/'}"
        out.append({"name": name, "type": desc, "state": p.get("runtimeState"), "interval_minutes": minutes, "when": when.strip()})
    return out


def _median(values):
    v = [x for x in values if x is not None]
    return statistics.median(v) if v else None


# ============================================================================ issues
def _issue(sev, kind, src, pipeline, title, summary, run_id=None, cause=None, fixes=None, evidence=None, system=None):
    return {"id": f"adf:{src['source_id']}:{pipeline or '-'}:{kind}:{run_id or '-'}", "severity": sev, "type": kind,
            "source_id": src["source_id"], "factory": src["name"], "pipeline": pipeline, "title": title, "summary": summary,
            "run_id": run_id, "cause": cause, "fixes": fixes or [], "evidence": evidence or [], "system": system}


def _child_lookup(run_id: str):
    run = ds.one("SELECT * FROM telemetry.pipeline_runs WHERE run_id = %s", (run_id,))
    if not run:
        return None
    return run, _tasks(run_id)


def _tasks(run_id: str) -> list:
    rows = ds.rows("""SELECT activity_run_id, run_id, pipeline_name, activity_name, activity_type, status, run_start, run_end,
                             duration_ms, error_code, error_message, failure_type, rows_read, rows_written, rows_skipped,
                             data_read, data_written, files_read, files_written, throughput_kbps, input, output, error
                      FROM telemetry.task_runs WHERE run_id = %s ORDER BY run_start NULLS LAST, activity_name""", (run_id,))
    for r in rows:
        for k in ("input", "output", "error"):
            r[k] = _j(r[k])
    return rows


def _checks(run_id: str) -> list:
    return ds.rows("""SELECT c.activity_run_id, k.activity_name, c.check_type, c.side, c.asset, c.status, c.expected, c.observed,
                             c.detail, c.checked_at
                      FROM telemetry.checks c LEFT JOIN telemetry.task_runs k USING (activity_run_id)
                      WHERE c.run_id = %s ORDER BY k.run_start, k.activity_name, c.side, c.check_type""", (run_id,))


def _pipeline_issues(src: dict, name: str, runs: list, defs: dict) -> list:
    out = []
    cfg = src.get("config") or {}
    finished = [r for r in runs if r["status"] in FINISHED]
    succeeded = [r for r in finished if r["status"] == "Succeeded"]
    newest = finished[0] if finished else None
    triggers = _triggers_for(defs, name)
    if newest and newest["status"] == "Failed":
        streak = next((i for i, r in enumerate(finished) if r["status"] != "Failed"), len(finished))
        roots = diagnose.failure_chain(newest, _tasks(newest["run_id"]), _child_lookup)
        top = roots[0] if roots else None
        out.append(_issue("CRITICAL" if streak >= 3 else "HIGH", "RUN_FAILED", src, name,
                          f"{name} failed" + (f" {streak} times in a row" if streak > 1 else ""),
                          (f"{top['title']} in step '{top.get('activity_name') or '?'}'"
                           + (f" of child pipeline {top['path'][-2]}" if len(top.get("path") or []) > 2 else "") + ". " if top else "") +
                          f"Last attempt {_short(newest['run_start'])}.", newest["run_id"],
                          cause=top, fixes=(top or {}).get("fixes"), system=(top or {}).get("system"),
                          evidence=[(top or {}).get("message", "")[:400]] if top else []))
    elif newest and newest["status"] == "Cancelled":
        out.append(_issue("MEDIUM", "RUN_CANCELLED", src, name, f"{name}: newest run was cancelled",
                          f"Run started {_short(newest['run_start'])} was cancelled; its data was not (fully) loaded.", newest["run_id"],
                          fixes=["Check who cancelled it in ADF Monitor and re-run if the data is still needed."], system=diagnose.ADF))
    # freshness
    interval = min((t["interval_minutes"] for t in triggers if t["interval_minutes"] and t["state"] == "Started"), default=None)
    limit_h = (2 * interval + 30) / 60 if interval else cfg.get("freshness_hours")
    last_ok = succeeded[0] if succeeded else None
    age = _age_hours(last_ok["run_end"] or last_ok["run_start"]) if last_ok else None
    if limit_h and runs and (age is None or age > limit_h):
        out.append(_issue("HIGH", "STALE", src, name, f"{name}: data is stale",
                          (f"Last successful run finished {age:.1f} h ago" if age is not None else "No successful run yet") +
                          f"; expected at least every {limit_h:.1f} h" + (f" (trigger runs {interval} min apart)." if interval else "."),
                          last_ok["run_id"] if last_ok else None,
                          fixes=["Check the trigger is started and the newest runs (failures are listed above).",
                                 "Downstream reports built on the target tables are showing old data until a run succeeds."],
                          system=diagnose.ADF))
    for t in triggers:
        if t["state"] == "Stopped":
            out.append(_issue("MEDIUM" if not any(x["state"] == "Started" for x in triggers) else "LOW", "TRIGGER_STOPPED", src, name,
                              f"Trigger '{t['name']}' is stopped",
                              f"{t['type']} trigger {t['when']} will not start {name} until it is started again.",
                              fixes=["ADF Studio -> Manage -> Triggers -> Start (then Publish)."], system=diagnose.ADF))
    # in progress for too long
    durations = [r["duration_ms"] for r in succeeded[:20]]
    med = _median(durations)
    for r in runs:
        if r["status"] in ("InProgress", "Queued"):
            h = _age_hours(r["run_start"]) or 0
            limit = max(3 * med / 3.6e6, 0.25) if med else 2
            if h > limit:
                out.append(_issue("MEDIUM", "LONG_RUNNING", src, name, f"{name} has been running for {h:.1f} h",
                                  f"Usually takes {med / 60000:.1f} min." if med else "No earlier run to compare with.", r["run_id"],
                                  fixes=["Open the run: the step still in progress is the one waiting (source, target, or integration runtime).",
                                         "Check locks / long queries on the target database."], system=diagnose.ADF))
    if last_ok:
        if last_ok["rows_skipped"]:
            out.append(_issue("MEDIUM", "ROWS_SKIPPED", src, name, f"{name} skipped {int(last_ok['rows_skipped']):,} row(s)",
                              "The run succeeded but some rows were not loaded (fault tolerance skipped incompatible rows).",
                              last_ok["run_id"], fixes=["Open the run to see the skipped-rows log and why they did not fit.",
                                                        "Fix the values at the source and re-load them."], system=diagnose.DATA))
        history = [r["rows_written"] for r in succeeded[1:11] if r["rows_written"] is not None]
        if len(history) >= 3 and last_ok["rows_written"] is not None:
            m = _median(history)
            now_rows = last_ok["rows_written"]
            if m and now_rows == 0:
                out.append(_issue("HIGH", "VOLUME", src, name, f"{name} loaded 0 rows",
                                  f"Usually loads about {m:,.0f} rows. The run 'succeeded' but nothing arrived: empty or missing source file?",
                                  last_ok["run_id"], fixes=["Check the source files of that run (Source checks on the run page)."],
                                  system=diagnose.SOURCE))
            elif m and now_rows < 0.5 * m:
                out.append(_issue("MEDIUM", "VOLUME", src, name, f"{name} loaded far fewer rows than usual",
                                  f"{now_rows:,.0f} rows vs a usual {m:,.0f} ({now_rows / m:.0%}).", last_ok["run_id"],
                                  fixes=["Compare the source file with earlier days: partial delivery or a filter change?"],
                                  system=diagnose.SOURCE))
        if med and len(durations) >= 4 and last_ok["duration_ms"]:
            hist = _median(durations[1:])
            if hist and last_ok["duration_ms"] > 2 * hist and last_ok["duration_ms"] - hist > 300_000:
                out.append(_issue("LOW", "SLOW", src, name, f"{name} ran much slower than usual",
                                  f"{last_ok['duration_ms'] / 60000:.1f} min vs a usual {hist / 60000:.1f} min.", last_ok["run_id"],
                                  fixes=["Open the run: the timeline shows which step took longer (queueing, source read or target write)."],
                                  system=diagnose.ADF))
    if newest:
        failed_steps = {t["activity_run_id"] for t in _tasks(newest["run_id"]) if t["status"] == "Failed"}
        for c in _checks(newest["run_id"]):
            if c["status"] in ("FAIL", "WARN") and c["check_type"] != "SKIPPED_ROWS":
                sev = "HIGH" if c["status"] == "FAIL" else ("MEDIUM" if c["check_type"] in ("SCHEMA_MATCH", "SOURCE_ROWS") else "LOW")
                if c["activity_run_id"] in failed_steps:
                    continue                                 # part of that step's failure, shown as evidence there
                label = {"SOURCE_FILES": "source files", "SOURCE_ROWS": "source row count", "TARGET_TABLE": "target table",
                         "TARGET_ROWS": "target row count", "SCHEMA_MATCH": "file vs table columns"}.get(c["check_type"], c["check_type"])
                out.append(_issue(sev, "CHECK_FAILED", src, name, f"{name}: {label} check {'failed' if c['status'] == 'FAIL' else 'found a difference'}",
                                  c["detail"] or "", newest["run_id"], evidence=[f"expected {c['expected']}, found {c['observed']}"]
                                  if c["expected"] else [], system=diagnose.SOURCE if c["side"] == "source" else diagnose.TARGET))
    return out


def issues(source_id: int | None = None) -> list:
    out = []
    for src in sources():
        if source_id and src["source_id"] != source_id:
            continue
        if src["last_sync_status"] == "FAILED":
            out.append(_issue("HIGH", "CONNECTOR", src, None, f"DataTrust cannot read factory {src['name']}",
                              src["last_sync_error"] or "", fixes=["Fix the access problem described; the next sync retries automatically."],
                              system=diagnose.CREDS))
        if not src["enabled"]:
            continue
        defs = _definitions(src["source_id"])
        by_pipe = _recent_runs(src["source_id"])
        for name in sorted(set(defs["pipeline"]) | set(by_pipe)):
            out += _pipeline_issues(src, name, by_pipe.get(name, []), defs)
        for t in ds.rows("""SELECT trigger_name, trigger_type, fired_at, message FROM telemetry.trigger_runs
                            WHERE source_id = %s AND status = 'Failed' AND fired_at > now() - interval '24 hours'
                            ORDER BY fired_at DESC""", (src["source_id"],)):
            out.append(_issue("MEDIUM", "TRIGGER_FAILED", src, None, f"Trigger '{t['trigger_name']}' failed to start a run",
                              f"{_short(t['fired_at'])}: {t['message'] or 'no message'}", fixes=["Check the trigger's settings and the pipeline parameters it passes."],
                              system=diagnose.ADF))
    out.sort(key=lambda i: (SEV_ORDER[i["severity"]], i["pipeline"] or ""))
    return out


def _recent_runs(source_id: int, per_pipeline: int = 60) -> dict:
    rows = ds.rows(f"""SELECT * FROM ({_run_stats_sql('r.source_id = %s')}) x
                       ORDER BY pipeline_name, run_start DESC NULLS LAST""", (source_id,))
    out = {}
    for r in rows:
        r["parameters"] = _j(r["parameters"])
        lst = out.setdefault(r["pipeline_name"], [])
        if len(lst) < per_pipeline:
            lst.append(r)
    return out


def health(pipeline_issues: list, runs: list) -> tuple[str, int]:
    if not runs:
        return "No runs yet", None
    score = max(0, 100 - sum(SEV_PENALTY[i["severity"]] for i in pipeline_issues))
    worst = min((SEV_ORDER[i["severity"]] for i in pipeline_issues), default=None)
    label = ("Failing" if worst is not None and worst <= 1 and any(i["type"] in ("RUN_FAILED", "STALE", "CONNECTOR") for i in pipeline_issues)
             else "At risk" if worst is not None and worst <= 1 else "Needs attention" if worst is not None and worst <= 2
             else "Healthy")
    return label, score


# ============================================================================ overview
def overview(source_id: int | None = None) -> dict:
    srcs = [s for s in sources() if not source_id or s["source_id"] == source_id]
    all_issues = issues(source_id)
    pipelines = []
    for src in srcs:
        defs = _definitions(src["source_id"])
        by_pipe = _recent_runs(src["source_id"])
        for name in sorted(set(defs["pipeline"]) | set(by_pipe)):
            runs = by_pipe.get(name, [])
            mine = [i for i in all_issues if i["source_id"] == src["source_id"] and i["pipeline"] == name]
            label, score = health(mine, runs)
            finished = [r for r in runs if r["status"] in FINISHED]
            ok = [r for r in finished if r["status"] == "Succeeded"]
            p = defs["pipeline"].get(name) or {}
            props = p.get("properties") or {}
            last30 = [r for r in finished if (_age_hours(r["run_start"]) or 1e9) <= 720]
            pipelines.append({
                "source_id": src["source_id"], "factory": src["name"], "name": name, "folder": (props.get("folder") or {}).get("name"),
                "description": props.get("description"), "steps": len(resolve.flatten_activities(props.get("activities"))),
                "removed": not p, "triggers": _triggers_for(defs, name), "health": label, "score": score,
                "issues": len(mine), "worst": min((i["severity"] for i in mine), key=lambda s: SEV_ORDER[s], default=None),
                "last_run": runs[0] if runs else None, "last_success": ok[0]["run_end"] if ok else None,
                "success_rate": round(100 * sum(r["status"] == "Succeeded" for r in last30) / len(last30)) if last30 else None,
                "runs_30d": len(last30), "median_duration_ms": _median([r["duration_ms"] for r in ok[:20]]),
                "median_rows": _median([r["rows_written"] for r in ok[:20]]),
                "spark": [{"s": r["status"], "d": r["duration_ms"], "rows": r["rows_written"], "t": r["run_start"], "id": r["run_id"]}
                          for r in finished[:20]][::-1],
            })
    ids = [s["source_id"] for s in srcs] or [-1]
    k = ds.one("""SELECT count(*) FILTER (WHERE run_start > now() - interval '24 hours') AS runs_24h,
                         count(*) FILTER (WHERE run_start > now() - interval '24 hours' AND status = 'Failed') AS failed_24h,
                         count(*) FILTER (WHERE run_start > now() - interval '7 days') AS runs_7d,
                         count(*) FILTER (WHERE run_start > now() - interval '7 days' AND status = 'Succeeded') AS ok_7d,
                         count(*) FILTER (WHERE run_start > now() - interval '7 days' AND status = 'Failed') AS failed_7d,
                         count(*) FILTER (WHERE status IN ('InProgress','Queued')) AS running
                  FROM telemetry.pipeline_runs WHERE source_id = ANY(%s)""", (ids,))
    v = ds.one("""SELECT sum(rows_written) AS rows_7d, sum(data_written) AS bytes_7d, sum(rows_skipped) AS skipped_7d
                  FROM telemetry.task_runs WHERE source_id = ANY(%s) AND run_start > now() - interval '7 days'
                  AND activity_type IN ('Copy','ExecuteDataFlow')""", (ids,))
    days = ds.rows("""SELECT to_char(date_trunc('day', run_start), 'YYYY-MM-DD') AS day, status, count(*) AS n
                      FROM telemetry.pipeline_runs WHERE source_id = ANY(%s) AND run_start > now() - interval '14 days'
                      GROUP BY 1, 2 ORDER BY 1""", (ids,))
    scores = [p["score"] for p in pipelines if p["score"] is not None]
    return {"sources": srcs, "kpis": {**k, **v, "pipelines": len(pipelines), "issues": len(all_issues),
                                      "success_rate_7d": round(100 * k["ok_7d"] / k["runs_7d"]) if k.get("runs_7d") else None,
                                      "trust": round(sum(scores) / len(scores)) if scores else None},
            "pipelines": sorted(pipelines, key=lambda p: (p["score"] if p["score"] is not None else 101, p["name"])),
            "issues": all_issues, "by_day": days}


def runs(source_id=None, pipeline=None, status=None, days: int = 30, limit: int = 200, q: str | None = None) -> list:
    where, params = ["r.run_start > now() - make_interval(days => %s)"], [int(days)]
    if source_id:
        where.append("r.source_id = %s")
        params.append(int(source_id))
    if pipeline:
        where.append("r.pipeline_name = %s")
        params.append(pipeline)
    if status:
        where.append("r.status = %s")
        params.append(status)
    if q:
        where.append("(r.run_id ILIKE %s OR r.message ILIKE %s OR r.parameters::text ILIKE %s)")
        params += [f"%{q}%"] * 3
    rows = ds.rows(_run_stats_sql(" AND ".join(where)) + " ORDER BY r.run_start DESC NULLS LAST LIMIT %s", (*params, int(limit)))
    for r in rows:
        r["parameters"] = _j(r["parameters"])
        msg = diagnose.leaf_message(r["message"] or "")
        r["cause"] = diagnose.classify(None, msg)["title"] if r["status"] == "Failed" and msg else None
        r["message"] = msg[:160] + ("…" if len(msg) > 160 else "")
    return rows


# ============================================================================ lineage
def _activity_lineage(defs: dict, pipeline: str, ctx: dict, tasks_by_name: dict | None = None) -> list:
    p = defs["pipeline"].get(pipeline) or {}
    edges = []
    for a in resolve.flatten_activities((p.get("properties") or {}).get("activities")):
        status = None
        if tasks_by_name is not None:
            ts = tasks_by_name.get(a["name"], [])
            status = ts[-1]["status"] if ts else "Not run"
        if a["type"] == "Copy":
            assets = resolve.copy_assets(a, defs["dataset"], defs["linkedservice"], ctx)
            edges.append({"activity": a["name"], "type": "Copy", "status": status, "source": assets.get("source"),
                          "target": assets.get("target")})
        elif a["type"] == "ExecutePipeline":
            edges.append({"activity": a["name"], "type": "ExecutePipeline", "status": status, "child_pipeline": a["child_pipeline"]})
        elif a["type"] in ("Script", "SqlServerStoredProcedure", "Lookup", "GetMetadata", "Delete", "ExecuteDataFlow"):
            ref = (a.get("inputs") or a.get("outputs") or [{}])[0] if a["type"] in ("Lookup", "GetMetadata", "Delete") else None
            asset = (resolve.dataset_asset(defs["dataset"].get((ref or {}).get("referenceName")), (ref or {}).get("parameters") or {},
                                           ctx, defs["linkedservice"]) if ref and ref.get("referenceName") else
                     resolve.linked_service_info(defs["linkedservice"].get(a.get("linked_service")), {}, ctx) if a.get("linked_service") else None)
            if asset and asset.get("kind") == "postgres" and not asset.get("name"):
                asset["name"] = f"{asset.get('database') or '?'} (database)"
            edges.append({"activity": a["name"], "type": a["type"], "status": status, "touches": asset, "dataflow": a.get("dataflow")})
    return edges


def _default_ctx(defs: dict, name: str) -> dict:
    """pipeline() with the parameters' default values: lineage of a pipeline outside any particular run."""
    params = ((defs["pipeline"].get(name) or {}).get("properties") or {}).get("parameters") or {}
    return {"pipeline": {"parameters": {k: v["defaultValue"] for k, v in params.items() if isinstance(v, dict) and "defaultValue" in v},
                         "Pipeline": name}}


def lineage(source_id: int) -> dict:
    """Every pipeline's sources and targets (with default parameter values), for the lineage map."""
    defs = _definitions(source_id)
    nodes, links = {}, []
    for name in sorted(defs["pipeline"]):
        pid = f"pipeline:{name}"
        nodes[pid] = {"id": pid, "kind": "pipeline", "label": name}
        for e in _activity_lineage(defs, name, _default_ctx(defs, name)):
            if e["type"] == "Copy":
                for side in ("source", "target"):
                    a = e.get(side)
                    if not a:
                        continue
                    aid = f"asset:{a.get('uri') or a.get('name')}"
                    nodes.setdefault(aid, {"id": aid, "kind": a.get("kind"), "label": a.get("name"), "system": a.get("system"),
                                           "detail": a})
                    links.append({"from": aid, "to": pid, "activity": e["activity"]} if side == "source"
                                 else {"from": pid, "to": aid, "activity": e["activity"]})
            elif e["type"] == "ExecutePipeline" and e.get("child_pipeline"):
                links.append({"from": pid, "to": f"pipeline:{e['child_pipeline']}", "activity": e["activity"]})
    return {"nodes": list(nodes.values()), "links": links}


# ============================================================================ run detail
def run_detail(run_id: str) -> dict:
    run = ds.one(_run_stats_sql("r.run_id = %s"), (run_id,))
    if not run:
        raise ValueError(f"run {run_id} not found (it may not be synced yet)")
    raw = ds.one("SELECT raw FROM telemetry.pipeline_runs WHERE run_id = %s", (run_id,)).get("raw")
    run["parameters"] = _j(run["parameters"])
    src = next((s for s in sources() if s["source_id"] == run["source_id"]), {})
    defs = _definitions(run["source_id"])
    tasks = _tasks(run_id)
    start = _dt(run["run_start"])
    by_name = {}
    for t in tasks:
        by_name.setdefault(t["activity_name"], []).append(t)
        s, e = _dt(t["run_start"]), _dt(t["run_end"])
        t["offset_ms"] = int((s - start).total_seconds() * 1000) if s and start else None
        t["queue_ms"] = None
        for d in ((t.get("output") or {}).get("executionDetails") or [])[:1]:
            dd = d.get("detailedDurations") or {}
            t["queue_ms"] = (dd.get("queuingDuration") or 0) * 1000 if dd else None
            t["transfer_ms"] = (dd.get("transferDuration") or 0) * 1000 if dd else None
            t["source_type"] = (d.get("source") or {}).get("type")
            t["sink_type"] = (d.get("sink") or {}).get("type")
            t["diu"] = d.get("usedDataIntegrationUnits")
            t["parallel"] = d.get("usedParallelCopies")
        if e is None and t["status"] in ("InProgress", "Queued") and s:
            t["duration_ms"] = int((datetime.now(timezone.utc) - s).total_seconds() * 1000)
    raw_run = dict(_j(raw) or {})
    ctx = resolve.run_context({**run, "raw": raw_run, "run_start": run["run_start"]}, run["pipeline_name"],
                              (src.get("config") or {}).get("factory_name", ""))
    roots = diagnose.failure_chain(run, tasks, _child_lookup) if run["status"] in ("Failed", "Cancelled") else []
    checks = _checks(run_id)
    for r in roots:                                     # evidence from the checks of the same step
        r["evidence"] = [c for c in checks if c["activity_run_id"] == r.get("activity_run_id") and c["status"] in ("FAIL", "WARN", "PASS")]
    steps = []
    p = defs["pipeline"].get(run["pipeline_name"]) or {}
    for a in resolve.flatten_activities((p.get("properties") or {}).get("activities")):
        ts = by_name.get(a["name"], [])
        steps.append({"name": a["name"], "type": a["type"], "parent": a["parent"], "branch": a["branch"],
                      "depends_on": a["depends_on"], "status": (ts[-1]["status"] if ts else "Not run"),
                      "runs": len(ts), "failed": sum(t["status"] == "Failed" for t in ts), "state": a.get("state")})
    history = [r for r in _recent_runs(run["source_id"]).get(run["pipeline_name"], []) if r["status"] == "Succeeded"
               and r["run_id"] != run_id][:20]
    children = ds.rows("SELECT run_id, pipeline_name, status, run_start, duration_ms FROM telemetry.pipeline_runs WHERE parent_run_id = %s ORDER BY run_start",
                       (run_id,))
    parent = ds.one("SELECT run_id, pipeline_name, status FROM telemetry.pipeline_runs WHERE run_id = %s", (run["parent_run_id"],)) \
        if run.get("parent_run_id") else None
    same_group = ds.rows("""SELECT run_id, status, run_start FROM telemetry.pipeline_runs WHERE run_group_id = %s AND run_id <> %s
                            ORDER BY run_start""", (raw_run.get("runGroupId"), run_id)) if raw_run.get("runGroupId") else []
    return {"run": run, "factory": src.get("name"), "source_id": run["source_id"], "tasks": tasks, "steps": steps,
            "root_causes": roots, "checks": checks, "lineage": _activity_lineage(defs, run["pipeline_name"], ctx, by_name),
            "compare": {"median_duration_ms": _median([r["duration_ms"] for r in history]),
                        "median_rows": _median([r["rows_written"] for r in history]), "runs": len(history)},
            "children": children, "parent": parent, "reruns": same_group,
            "triggers": _triggers_for(defs, run["pipeline_name"]), "invoked_by": raw_run.get("invokedBy"),
            "adf_link": _adf_link(src, run_id)}


def _adf_link(src: dict, run_id: str | None = None) -> str | None:
    cfg = (src or {}).get("config") or {}
    if not cfg.get("factory_name"):
        return None
    fid = (f"%2Fsubscriptions%2F{cfg['subscription_id']}%2FresourceGroups%2F{cfg['resource_group']}"
           f"%2Fproviders%2FMicrosoft.DataFactory%2Ffactories%2F{cfg['factory_name']}")
    if run_id:
        return f"https://adf.azure.com/en/monitoring/pipelineruns/{run_id}?factory={fid}"
    return f"https://adf.azure.com/en/monitoring/pipelineruns?factory={fid}"


def pipeline_detail(source_id: int, name: str) -> dict:
    src = next((s for s in sources() if s["source_id"] == source_id), None)
    if not src:
        raise ValueError(f"Data Factory connection {source_id} not found")
    defs = _definitions(source_id)
    p = defs["pipeline"].get(name)
    runs_ = _recent_runs(source_id).get(name, [])
    if not p and not runs_:
        raise ValueError(f"pipeline {name} not found")
    props = (p or {}).get("properties") or {}
    mine = [i for i in issues(source_id) if i["pipeline"] == name]
    label, score = health(mine, runs_)
    return {"name": name, "factory": src["name"], "source_id": source_id, "description": props.get("description"),
            "parameters": props.get("parameters") or {}, "folder": (props.get("folder") or {}).get("name"),
            "activities": resolve.flatten_activities(props.get("activities")), "triggers": _triggers_for(defs, name),
            "lineage": _activity_lineage(defs, name, _default_ctx(defs, name)), "runs": runs_[:60], "issues": mine, "health": label, "score": score,
            "adf_link": _adf_link(src), "removed": not p}


def changed_signature() -> str:
    """Changes when anything new is collected: the pages poll it to refresh themselves."""
    if not ds.table_exists("telemetry.sources"):
        return ""
    v = ds.one("""SELECT (SELECT max(last_sync_finished) FROM telemetry.sources)::text AS s,
                         (SELECT max(last_updated) FROM telemetry.pipeline_runs)::text AS r,
                         (SELECT count(*) FROM telemetry.checks) AS c""")
    return f"{v.get('s')}|{v.get('r')}|{v.get('c')}"
