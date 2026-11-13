"""
One source at a time: every number on Observe, Detect, Investigate, Impact and Verify belongs to the source picked at the
top of the page. Nothing is blended across sources.

    scope key     "db:<connection_id>"   a PostgreSQL / SQL Server connection (Data estate -> Connections)
                  "adf:<source_id>"      an Azure Data Factory (Connect ADF)

How each kind is scored
    POC database  its pipeline rules (data_service.trust_score) + its other data checks
    any database  its data checks (monitor.*): 100 minus a penalty per failing check, by severity
    ADF factory   its pipelines (adf.insights): the average pipeline score

Incident ids are unique across sources: POC ids are unchanged (RECON-..., DQ-...), checks are "db<id>:CHECK-<n>",
ADF issues keep their "adf:<id>:..." ids. Incidents get a lifecycle in app.incident_state:
first seen -> (acknowledged) -> resolved (no longer in the data), so Verify can show what the data proves fixed.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

import appdb
import config
import data_service as ds

CHECK_PENALTY = {"CRITICAL": 40, "HIGH": 25, "MEDIUM": 10, "LOW": 4}
ERROR_PENALTY, WARN_PENALTY = 5, 2
HEALTHY, ATTENTION = ds.HEALTHY, ds.ATTENTION
CATEGORY = {"freshness": "Freshness", "volume": "Volume", "null_rate": "Completeness", "uniqueness": "Uniqueness",
            "schema": "Schema", "custom_sql": "Business rules", "reconciliation": "Reconciliation"}

STATE_DDL = """
CREATE SCHEMA IF NOT EXISTS app;
CREATE TABLE IF NOT EXISTS app.incident_state (
    incident_id     TEXT PRIMARY KEY,
    scope           TEXT NOT NULL,
    title           TEXT,
    severity        TEXT,
    first_seen      TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen       TIMESTAMPTZ NOT NULL DEFAULT now(),
    resolved_at     TIMESTAMPTZ,
    acknowledged_by TEXT,
    acknowledged_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS incident_state_by_scope ON app.incident_state (scope, resolved_at);
"""


def ensure_tables() -> None:
    with appdb.connect() as conn:
        conn.execute(STATE_DDL)
        # the two starter checks that repeat the POC pipeline's own findings: shown on Data checks, not counted twice
        conn.execute("""UPDATE monitor.checks SET config = config || '{"dedupe": "poc"}'::jsonb
                        WHERE origin = 'starter' AND NOT config ? 'dedupe'
                          AND name IN ('Gateway settlement matches captured payments', 'No rows held back by quality rules')""")
        conn.commit()


def label_for(score) -> str:
    if score is None:
        return "No data yet"
    return "Healthy" if score >= HEALTHY else "Needs attention" if score >= ATTENTION else "At risk"


# ============================================================================ which sources
def _poc_connection_id():
    for c in _connections():
        if c["source_type"] == "postgresql" and c["host"].lower() == config.PG_HOST.lower() and c["database"] == config.PG_DATABASE:
            return c["connection_id"]
    return None


def _connections() -> list:
    from estate import store as estate_store
    return estate_store.list_connections()


def list_scopes() -> list:
    poc = _poc_connection_id()
    out = [{"key": f"db:{c['connection_id']}", "kind": "db", "id": c["connection_id"], "name": c["name"],
            "type": {"postgresql": "PostgreSQL", "sqlserver": "SQL Server"}[c["source_type"]], "poc": c["connection_id"] == poc}
           for c in _connections() if c["source_type"] in ("postgresql", "sqlserver")]
    try:
        from adf import insights
        out += [{"key": f"adf:{s['source_id']}", "kind": "adf", "id": s["source_id"], "name": s["name"], "type": "Azure Data Factory",
                 "poc": False} for s in insights.sources()]
    except Exception:                                                 # noqa: BLE001  no telemetry yet
        pass
    out.sort(key=lambda s: (not s["poc"], s["kind"], s["name"].lower()))
    return out


def resolve(scope: str | None) -> dict:
    scopes = list_scopes()
    if not scopes:
        raise ValueError("No source connected yet: add one under Data estate → Connections or Connect ADF")
    return next((s for s in scopes if s["key"] == scope), scopes[0])


def scope_of_incident(incident_id: str) -> str | None:
    m = re.match(r"^db(\d+):", incident_id)
    if m:
        return f"db:{m.group(1)}"
    m = re.match(r"^adf:(\d+):", incident_id)
    if m:
        return f"adf:{m.group(1)}"
    poc = _poc_connection_id()
    return f"db:{poc}" if poc else None


# ============================================================================ data checks of a connection
def _checks_of(connection_id: int, skip_dedupe: bool) -> list:
    rows = appdb.rows("""
        SELECT c.check_id, c.name, c.check_type, c.connection_id, c.database, c.schema_name, c.table_name, c.config, c.severity,
               c.enabled, c.last_status, c.last_run_at, c.description, r.observed, r.expected, r.message, r.details, r.run_at
        FROM monitor.checks c
        LEFT JOIN LATERAL (SELECT observed, expected, message, details, run_at FROM monitor.results x
                           WHERE x.check_id = c.check_id ORDER BY run_at DESC LIMIT 1) r ON true
        WHERE c.enabled AND (c.connection_id = %(c)s
              OR (c.check_type = 'reconciliation' AND (c.config->'left'->>'connection_id')::int = %(c)s))""",
        {"c": connection_id}) if appdb.table_exists("monitor.checks") else []
    return [r for r in rows if not (skip_dedupe and (r["config"] or {}).get("dedupe") == "poc")]


def _target(c) -> str:
    return f"{c['schema_name']}.{c['table_name']}" if c.get("table_name") else (
        f"{(c['config'].get('left') or {}).get('label', 'left')} vs {(c['config'].get('right') or {}).get('label', 'right')}"
        if c["check_type"] == "reconciliation" else "custom query")


def _check_incident(c, scope_id: int) -> dict | None:
    st = c["last_status"]
    if st not in ("FAIL", "ERROR", "WARN"):
        return None
    sev = c["severity"] if st == "FAIL" else ("MEDIUM" if st == "ERROR" else "LOW")
    verb = {"FAIL": "failed", "ERROR": "could not run", "WARN": "warning"}[st]
    return {"id": f"db{scope_id}:CHECK-{c['check_id']}", "severity": sev, "type": {"FAIL": "CHECK_FAILED", "ERROR": "CHECK_ERROR",
            "WARN": "CHECK_WARNING"}[st], "title": f"{c['name']}: {verb}", "summary": c["message"] or "",
            "business_date": None, "source": "check",
            "metrics": {"check_id": c["check_id"], "check_type": c["check_type"], "observed": c["observed"], "expected": c["expected"],
                        "target": _target(c), "n": c["observed"], "status": st, "run_at": c["run_at"]}}


def _check_penalty(c) -> float:
    st = c["last_status"]
    return CHECK_PENALTY[c["severity"]] if st == "FAIL" else ERROR_PENALTY if st == "ERROR" else WARN_PENALTY if st == "WARN" else 0


# ============================================================================ incidents of one source
def incidents(scope: str | None) -> dict:
    s = resolve(scope)
    found = _incidents(s)
    _approvals(found)
    _track(s["key"], found)
    state = _state(s["key"])
    for i in found:
        st = state.get(i["id"]) or {}
        i["first_seen"], i["acknowledged_by"], i["acknowledged_at"] = st.get("first_seen"), st.get("acknowledged_by"), st.get("acknowledged_at")
    order = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3}
    found.sort(key=lambda i: (order.get(i["severity"], 4), i["id"]))
    return {"scope": s, "open": found, "resolved": _resolved(s["key"], {i["id"] for i in found})}


def _incidents(s: dict) -> list:
    if s["kind"] == "adf":
        from adf import insights
        out = []
        for i in insights.issues(s["id"]):
            ev = i.get("evidence") or []
            out.append({"id": i["id"], "severity": i["severity"], "type": i["type"], "title": i["title"], "summary": i["summary"] or "",
                        "business_date": None, "source": "adf", "adf": i,
                        "metrics": {"pipeline": i.get("pipeline"), "run_id": i.get("run_id"), "n": None, "evidence": ev}})
        return out
    found = [x for x in (_check_incident(c, s["id"]) for c in _checks_of(s["id"], skip_dedupe=s["poc"])) if x]
    if s["poc"]:
        found = ds.incidents() + found
    return found


def all_incidents() -> list:
    """Every source's open incidents (for alerts). Each keeps its source in 'scope'."""
    out = []
    for s in list_scopes():
        try:
            for i in _incidents(s):
                out.append({**i, "scope": s["key"], "scope_name": s["name"]})
        except Exception:                                             # noqa: BLE001  one unreachable source must not stop alerts
            continue
    return out


def _approvals(found: list) -> None:
    if not appdb.table_exists("app.remediation_log"):
        return
    ids = [i["id"] for i in found if not i.get("approval")]
    if not ids:
        return
    rows = appdb.rows("""SELECT DISTINCT ON (incident_id) incident_id, approved_by, approved_at, status FROM app.remediation_log
                         WHERE incident_id = ANY(%s) ORDER BY incident_id, approved_at DESC""", (ids,))
    by = {r["incident_id"]: r for r in rows}
    for i in found:
        if not i.get("approval"):
            i["approval"] = by.get(i["id"])


def _track(scope_key: str, found: list) -> None:
    """Lifecycle: new ids are first seen now; ids that were open and are gone are resolved now (the data no longer shows them)."""
    try:
        with appdb.connect() as conn:
            for i in found:
                conn.execute("""INSERT INTO app.incident_state (incident_id, scope, title, severity) VALUES (%s, %s, %s, %s)
                                ON CONFLICT (incident_id) DO UPDATE SET last_seen = now(), title = EXCLUDED.title,
                                    severity = EXCLUDED.severity, scope = EXCLUDED.scope,
                                    first_seen = CASE WHEN app.incident_state.resolved_at IS NOT NULL THEN now() ELSE app.incident_state.first_seen END,
                                    acknowledged_by = CASE WHEN app.incident_state.resolved_at IS NOT NULL THEN NULL ELSE app.incident_state.acknowledged_by END,
                                    acknowledged_at = CASE WHEN app.incident_state.resolved_at IS NOT NULL THEN NULL ELSE app.incident_state.acknowledged_at END,
                                    resolved_at = NULL""", (i["id"], scope_key, i["title"][:300], i["severity"]))
            conn.execute("""UPDATE app.incident_state SET resolved_at = now()
                            WHERE scope = %s AND resolved_at IS NULL AND NOT (incident_id = ANY(%s))""",
                         (scope_key, [i["id"] for i in found]))
            conn.commit()
    except Exception as e:                                            # noqa: BLE001  the page still works without the lifecycle
        print(f"!! incident lifecycle not updated: {e}")


def _state(scope_key: str) -> dict:
    if not appdb.table_exists("app.incident_state"):
        return {}
    return {r["incident_id"]: r for r in appdb.rows("SELECT * FROM app.incident_state WHERE scope = %s", (scope_key,))}


def _resolved(scope_key: str, open_ids: set) -> list:
    """Verified resolved: approved fixes whose incident is gone, plus anything that disappeared from the data in the last 7 days."""
    out = {}
    if appdb.table_exists("app.incident_state"):
        for r in appdb.rows("""SELECT s.incident_id, s.title, s.severity, s.first_seen, s.resolved_at, a.actions, a.approved_by, a.approved_at
                               FROM app.incident_state s
                               LEFT JOIN LATERAL (SELECT actions, approved_by, approved_at FROM app.remediation_log l
                                                  WHERE l.incident_id = s.incident_id ORDER BY approved_at DESC LIMIT 1) a ON true
                               WHERE s.scope = %s AND s.resolved_at > now() - interval '7 days'
                               ORDER BY s.resolved_at DESC""", (scope_key,)):
            if r["incident_id"] not in open_ids:
                out[r["incident_id"]] = {"incident_id": r["incident_id"], "incident_title": r["title"], "severity": r["severity"],
                                         "actions": r["actions"] or "No longer present in the data", "approved_by": r["approved_by"],
                                         "approved_at": r["approved_at"], "first_seen": r["first_seen"], "resolved_at": r["resolved_at"]}
    return list(out.values())


def acknowledge(incident_id: str, by: str) -> None:
    with appdb.connect() as conn:
        n = conn.execute("""UPDATE app.incident_state SET acknowledged_by = %s, acknowledged_at = now()
                            WHERE incident_id = %s AND resolved_at IS NULL""", (by, incident_id)).rowcount
        conn.commit()
    if not n:
        raise ValueError("That incident is not open any more.")


# ============================================================================ trust score + Observe page of one source
def overview(scope: str | None) -> dict:
    s = resolve(scope)
    if s["kind"] == "adf":
        return _overview_adf(s)
    if s["poc"]:
        return _overview_poc(s)
    return _overview_db(s)


def _overview_poc(s: dict) -> dict:
    trust = ds.trust_score()
    extra = [c for c in _checks_of(s["id"], skip_dedupe=True) if _check_penalty(c)]
    for c in extra:
        p = _check_penalty(c)
        trust["reasons"].append({"points": p, "reason": f"data check '{c['name']}' {c['last_status'].lower()}"})
        trust["score"] = max(0, trust["score"] - p)
    if extra:
        trust["label"] = label_for(trust["score"]) if trust["label"] != "At risk" else "At risk"
    return {"kind": "poc", "scope": s, "overview": ds.overview(), "trust": trust, "pipeline": ds.pipeline_status(),
            "as_of": ds.as_of_date().isoformat(), "checks": _check_summary(s["id"], True),
            "settlement": ds.settlement_recon()}                     # per date: gateway vs captured, for the revenue chart


def _check_summary(connection_id: int, skip_dedupe: bool) -> dict:
    cs = _checks_of(connection_id, skip_dedupe)
    return {"total": len(cs), "failing": sum(c["last_status"] == "FAIL" for c in cs), "errors": sum(c["last_status"] == "ERROR" for c in cs),
            "warnings": sum(c["last_status"] == "WARN" for c in cs), "passing": sum(c["last_status"] == "PASS" for c in cs)}


def _overview_db(s: dict) -> dict:
    checks = _checks_of(s["id"], skip_dedupe=False)
    ran = [c for c in checks if c["last_status"]]
    reasons = [{"points": _check_penalty(c), "reason": f"'{c['name']}' {c['last_status'].lower()}: {c['message'] or ''}".strip()}
               for c in ran if _check_penalty(c)]
    score = max(0, round(100 - sum(r["points"] for r in reasons))) if ran else None
    dims = []
    for t, label in CATEGORY.items():
        mine = [c for c in ran if c["check_type"] == t]
        if mine:
            ok = sum(c["last_status"] == "PASS" for c in mine)
            dims.append({"label": f"{label} ({ok} of {len(mine)} checks pass)", "value": round(100 * ok / len(mine), 1)})
    tables = sorted({f"{c['schema_name']}.{c['table_name']}" for c in checks if c.get("table_name")})
    last = max((c["last_run_at"] for c in checks if c["last_run_at"]), default=None)
    n_open = sum(1 for c in ran if c["last_status"] != "PASS")
    kpis = [{"label": "Data checks", "value": len(checks), "sub": f"{len(ran)} run"},
            {"label": "Failing", "value": sum(c["last_status"] == "FAIL" for c in ran), "tone": "bad"},
            {"label": "Could not run", "value": sum(c["last_status"] == "ERROR" for c in ran), "tone": "bad"},
            {"label": "Warnings", "value": sum(c["last_status"] == "WARN" for c in ran), "tone": "warn"},
            {"label": "Tables watched", "value": len(tables)},
            {"label": "Open incidents", "value": n_open, "tone": "bad"}]
    items = [{"name": t, "detail": ", ".join(CATEGORY[c["check_type"]] for c in checks if f"{c['schema_name']}.{c['table_name']}" == t),
              "status": _worst([c["last_status"] for c in checks if f"{c['schema_name']}.{c['table_name']}" == t])} for t in tables]
    items += [{"name": c["name"], "detail": CATEGORY[c["check_type"]], "status": c["last_status"]}
              for c in checks if not c.get("table_name")]
    return {"kind": "generic", "scope": s, "trust": {"score": score, "label": label_for(score), "reasons": reasons, "basis": "data checks",
            "rules": {"healthy_from": HEALTHY, "attention_from": ATTENTION}},
            "kpis": kpis, "dimensions": dims, "trend": _check_trend(s["id"]), "trend_label": "Trust score by day (from data check results)",
            "items_title": "What is watched", "items": items, "last_activity": last,
            "empty": None if checks else "No data checks on this connection yet. Add one on Data checks → Add check: freshness, volume, "
                                         "empty values, keys, schema, your own SQL or a reconciliation."}


def _worst(statuses: list) -> str | None:
    for s in ("FAIL", "ERROR", "WARN", "PASS"):
        if s in statuses:
            return s
    return None


def _check_trend(connection_id: int, days: int = 14) -> list:
    """Score per day with the same rule: each check's last result of that day (or the latest before it)."""
    checks = {c["check_id"]: c for c in _checks_of(connection_id, skip_dedupe=False)}
    if not checks:
        return []
    start = datetime.now(timezone.utc).date() - timedelta(days=days - 1)
    rows = appdb.rows("""SELECT check_id, status, run_at FROM monitor.results WHERE check_id = ANY(%s) ORDER BY run_at""",
                      (list(checks),))
    out, latest = [], {}
    idx = 0
    for n in range(days):
        day = start + timedelta(days=n)
        while idx < len(rows) and datetime.fromisoformat(str(rows[idx]["run_at"])).astimezone(timezone.utc).date() <= day:
            latest[rows[idx]["check_id"]] = rows[idx]["status"]
            idx += 1
        if not latest:
            continue
        pen = sum(CHECK_PENALTY[checks[cid]["severity"]] if st == "FAIL" else ERROR_PENALTY if st == "ERROR" else WARN_PENALTY if st == "WARN" else 0
                  for cid, st in latest.items())
        out.append({"day": day.isoformat(), "score": max(0, 100 - pen)})
    return out


def _overview_adf(s: dict) -> dict:
    from adf import insights
    o = insights.overview(s["id"])
    k = o["kpis"]
    issues = insights.issues(s["id"])
    reasons = [{"points": insights.SEV_PENALTY[i["severity"]], "reason": i["title"]} for i in issues]
    score = k.get("trust")
    pipes = o["pipelines"]
    healthy = sum(p["health"] == "Healthy" for p in pipes if p["score"] is not None)
    scored = [p for p in pipes if p["score"] is not None]
    chk = appdb.one("""SELECT count(*) FILTER (WHERE status = 'PASS') AS ok, count(*) FILTER (WHERE status IN ('PASS','WARN','FAIL')) AS n
                       FROM telemetry.checks WHERE source_id = %s AND checked_at > now() - interval '7 days'""", (s["id"],))
    dims = []
    if k.get("success_rate_7d") is not None:
        dims.append({"label": f"Run success, 7 days ({k['ok_7d']} of {k['runs_7d']} runs)", "value": k["success_rate_7d"]})
    if scored:
        dims.append({"label": f"Healthy pipelines ({healthy} of {len(scored)})", "value": round(100 * healthy / len(scored), 1)})
    if chk.get("n"):
        dims.append({"label": f"Source / target checks passing, 7 days ({chk['ok']} of {chk['n']})", "value": round(100 * chk["ok"] / chk["n"], 1)})
    by_day = {}
    for r in o.get("by_day") or []:
        d = by_day.setdefault(r["day"], {"ok": 0, "n": 0})
        if r["status"] in ("Succeeded", "Failed", "Cancelled"):
            d["n"] += r["n"]
            d["ok"] += r["n"] if r["status"] == "Succeeded" else 0
    trend = [{"day": d, "score": round(100 * v["ok"] / v["n"], 1)} for d, v in sorted(by_day.items()) if v["n"]]
    # runs per day for the last 14 days, every day present (a quiet day is a zero, not a gap in the axis)
    today = datetime.now(timezone.utc).date()
    runs_by_day = []
    for n in range(13, -1, -1):
        d = (today - timedelta(days=n)).isoformat()
        rows = [r for r in o.get("by_day") or [] if r["day"] == d]
        runs_by_day.append({"day": d, **{st: sum(r["n"] for r in rows if r["status"] == st) for st in ("Succeeded", "Failed", "Cancelled")},
                            "Other": sum(r["n"] for r in rows if r["status"] not in ("Succeeded", "Failed", "Cancelled"))})
    kpis = [{"label": "Pipelines", "value": k["pipelines"]},
            {"label": "Runs, 24 h", "value": k["runs_24h"], "sub": f"{k['failed_24h']} failed"},
            {"label": "Success rate, 7 d", "value": f"{k['success_rate_7d']}%" if k.get("success_rate_7d") is not None else "–"},
            {"label": "Rows loaded, 7 d", "value": int(k["rows_7d"] or 0)},
            {"label": "Rows skipped, 7 d", "value": int(k["skipped_7d"] or 0), "tone": "warn"},
            {"label": "Open incidents", "value": len(issues), "tone": "bad"}]
    items = [{"name": p["name"], "detail": f"{p['runs_30d']} runs in 30 d" + (f", {p['success_rate']}% succeeded" if p["success_rate"] is not None else ""),
              "status": {"Healthy": "PASS", "Needs attention": "WARN", "At risk": "FAIL", "Failing": "FAIL"}.get(p["health"]), "label": p["health"]}
             for p in pipes]
    last = (o["sources"][0] if o["sources"] else {}).get("last_sync_finished")
    return {"kind": "generic", "scope": s, "trust": {"score": score, "label": label_for(score), "reasons": reasons, "basis": "pipeline runs",
            "rules": {"healthy_from": HEALTHY, "attention_from": ATTENTION}},
            "kpis": kpis, "dimensions": dims, "trend": trend, "trend_label": "Run success rate by day (%)", "runs_by_day": runs_by_day,
            "items_title": "Pipelines", "items": items, "last_activity": last,
            "empty": None if pipes else "No pipelines read from this factory yet: check Connect ADF."}


# ============================================================================ Explain (lineage) of one source
def _pretty(text: str) -> str:
    """ADF expressions that only resolve at run time, written readably: {p_batch_id}, {target_schema from LK_Get_File_Config}."""
    t = re.sub(r"@\{(.+?)\}", r"@\1", str(text or ""))
    t = re.sub(r"@?pipeline\(\)\.parameters\.(\w+)", r"{\1}", t)                  # also inside a function: @json(activity(...))
    t = re.sub(r"@?activity\('([^']+)'\)\.output\.firstRow\.(\w+)", r"{\2 from \1}", t)
    t = re.sub(r"@?activity\('([^']+)'\)[\w.()[\]']*", r"{output of \1}", t)
    t = re.sub(r"@item\(\)\.(\w+)", r"{each \1}", t)
    return t


def lineage(s: dict) -> dict:
    """Rows of 'from -> to (how)' for the source: an ADF factory's copy steps, or a database's watched tables and the
    views that read them. Each row carries the worst current status of what touches it."""
    flows = []
    if s["kind"] == "adf":
        from adf import insights
        g = insights.lineage(s["id"])
        label = {n["id"]: n.get("label") or n["id"] for n in g["nodes"]}
        kind = {n["id"]: n.get("kind") for n in g["nodes"]}
        bad = {i["pipeline"]: i["severity"] for i in insights.issues(s["id"]) if i.get("pipeline")}
        by_pipe = {}
        for ln in g["links"]:
            p = ln["to"] if ln["to"].startswith("pipeline:") and not ln["from"].startswith("pipeline:") else ln["from"]
            d = by_pipe.setdefault(p, {"sources": [], "targets": [], "children": []})
            if ln["to"] == p:
                d["sources"].append(label[ln["from"]])
            elif ln["to"].startswith("pipeline:"):
                d["children"].append(label[ln["to"]])
            else:
                d["targets"].append(f"{label[ln['to']]}" + (f" ({kind[ln['to']]})" if kind.get(ln["to"]) else ""))
        for p, d in sorted(by_pipe.items()):
            name = label.get(p, p.split(":", 1)[1])
            flows.append({"from": _pretty(", ".join(d["sources"])) or "–", "via": f"pipeline {name}" + (f" → runs {', '.join(d['children'])}" if d["children"] else ""),
                          "to": _pretty(", ".join(d["targets"])) or "–", "status": "FAIL" if bad.get(name) in ("CRITICAL", "HIGH") else "WARN" if name in bad else "PASS"})
        return {"title": f"What the pipelines of {s['name']} read and write", "flows": flows,
                "note": "From the pipeline definitions. Values in {braces} are only known when a run starts; each run's exact paths are on ADF runs."}
    checks = _checks_of(s["id"], skip_dedupe=False)
    tables = {}
    for c in checks:
        if c.get("table_name"):
            tables.setdefault((c["schema_name"], c["table_name"], c.get("database")), []).append(c)
    for (schema, table, db), cs in sorted(tables.items()):
        views = _downstream(s["id"], {"schema_name": schema, "table_name": table, "database": db})
        flows.append({"from": f"{schema}.{table}", "via": ", ".join(sorted({CATEGORY[c['check_type']] for c in cs})) + " checks",
                      "to": ", ".join(views) or "no views read it", "status": _worst([c["last_status"] for c in cs])})
    for c in checks:
        if c["check_type"] == "reconciliation":
            flows.append({"from": (c["config"].get("left") or {}).get("label", "left"), "via": f"reconciliation '{c['name']}'",
                          "to": (c["config"].get("right") or {}).get("label", "right"), "status": c["last_status"]})
    return {"title": f"Watched tables of {s['name']} and the views that read them", "flows": flows,
            "note": "Views come from the database's own catalog. Add data checks to watch more tables."
                    if flows else "Nothing is watched on this connection yet: add data checks to see its tables here."}


# ============================================================================ Investigate / Impact of one incident
def investigate(incident_id: str, scope: str | None = None) -> dict:
    scope = scope or scope_of_incident(incident_id)
    s = resolve(scope)
    if s["poc"] and not incident_id.startswith(f"db{s['id']}:"):
        return ds.investigate(incident_id)
    inc = next((i for i in _incidents(s) if i["id"] == incident_id), None)
    if inc is None:
        return {"found": False}
    _approvals([inc])
    if inc.get("source") == "check":
        return _investigate_check(s, inc)
    return _investigate_adf(s, inc)


ACTIONS = {
    "freshness": ["Check whether the job that loads this table ran and succeeded", "Re-run the load if it was skipped or failed",
                  "Confirm the source delivered new data"],
    "volume": ["Compare with the source: was less data delivered?", "Check the load job for filters or partial runs",
               "Re-run the load for the missing period"],
    "null_rate": ["Find the rows with empty values (see the evidence)", "Fix them at the source or add a default in the load",
                  "Re-run the load"],
    "uniqueness": ["Inspect the duplicate keys (see the evidence)", "Remove duplicates or fix the load so it upserts on the key",
                   "Add a unique constraint once the data is clean"],
    "schema": ["Contact the owner of the change", "Revert it, or update the jobs and views that use the table",
               "Accept the new schema on the check once everything is updated"],
    "custom_sql": ["Review the rows the rule returned", "Fix them at the source", "Re-run the check"],
    "reconciliation": ["Look at the keys that differ (see the evidence)", "Find whether rows are missing or values changed on one side",
                       "Re-load the affected keys and re-run the check"],
}


def _investigate_check(s: dict, inc: dict) -> dict:
    from checks import store as checks_store
    cid = inc["metrics"]["check_id"]
    chk = checks_store.get_check(cid)
    hist = checks_store.history(cid, 50)
    last = hist[0] if hist else {}
    streak = 0
    for r in hist:
        if r["status"] != last.get("status"):
            break
        streak += 1
    last_pass = next((r for r in hist if r["status"] == "PASS"), None)
    where = f"{s['name']}" + (f" · {chk['schema_name']}.{chk['table_name']}" if chk.get("table_name") else "")
    obs = last.get("observed")
    obs_txt = "–" if obs is None else f"{obs:,.0f}" if float(obs).is_integer() else f"{obs:,.4g}"
    # ok = the fact was confirmed by a query (True) or could not be measured (None); a failing check's findings are confirmed facts
    checks = [{"ok": True, "text": last.get("message") or inc["summary"], "source": f"data check '{chk['name']}' on {where}"},
              {"ok": None if obs is None else True, "text": f"Expected {last.get('expected') or 'see rule'}; measured {obs_txt}",
               "source": "monitor.results (latest run)"},
              {"ok": True, "text": f"{streak} run(s) in a row with this result" + (f"; last passed {str(last_pass['run_at'])[:16]}" if last_pass else "; it has not passed since the check was added"),
               "source": "monitor.results (history)"}]
    d = last.get("details") or {}
    sample = d.get("duplicates") or d.get("rows") or []
    if chk["check_type"] == "reconciliation" and d.get("mismatches"):
        sample = []
        for m in d["mismatches"][:20]:
            if m.get("differences"):
                for k, v in m["differences"].items():
                    sample.append({"key": m["key"], "measure": k, **{str(a): b for a, b in v.items()}})
            else:
                sample.append({"key": m["key"], "measure": "–", "difference": m["problem"]})
    if chk["check_type"] == "schema":
        sample = [{"change": "removed", "column": c} for c in d.get("removed", [])] + \
                 [{"change": "type changed", "column": x["column"], "was": x["was"], "now": x["now"]} for x in d.get("type_changed", [])] + \
                 [{"change": "added", "column": c} for c in d.get("added", [])]
    views = _downstream(s["id"], chk) if chk.get("table_name") else []
    root = f"{chk['name']}: {last.get('message') or inc['summary']}"
    chain = [f"{s['type']} connection {s['name']}" + (f", database {chk['database']}" if chk.get("database") else ""),
             f"{'table ' + chk['schema_name'] + '.' + chk['table_name'] if chk.get('table_name') else 'query'}",
             f"rule: {last.get('expected') or chk['check_type']}", last.get("message") or inc["summary"]]
    if chk.get("description"):
        chain.insert(0, f"Why it matters: {chk['description']}")
    impact = {"entity": f"{chk['schema_name']}.{chk['table_name']}" if chk.get("table_name") else chk["name"], "gold_views": views,
              "views_label": "Views that read this table", "business_date": None, "amount_at_risk": None,
              "actions": ACTIONS.get(chk["check_type"], ["Investigate the evidence", "Fix at the source", "Re-run the check"])}
    return {"found": True, "incident": inc, "checks": checks, "root_cause": root, "chain": chain, "sample": sample, "impact": impact,
            "sql": last.get("sql_text"), "check_id": cid}


def _downstream(connection_id: int, chk: dict) -> list:
    """Views (PostgreSQL catalog) that read the checked table: what else shows the problem."""
    try:
        from estate import service
        src = service.source(connection_id)
        if src.cfg["source_type"] != "postgresql":
            return []
        rows = src.q("""SELECT DISTINCT view_schema || '.' || view_name AS v FROM information_schema.view_table_usage
                        WHERE table_schema = %s AND table_name = %s ORDER BY 1""", (chk["schema_name"], chk["table_name"]),
                     chk.get("database") or None)
        return [r["v"] for r in rows]
    except Exception:                                                 # noqa: BLE001
        return []


def _investigate_adf(s: dict, inc: dict) -> dict:
    i = inc["adf"]
    cause = i.get("cause") or {}
    checks = [{"ok": False, "text": i["summary"], "source": f"Azure Data Factory {s['name']}" + (f" · pipeline {i['pipeline']}" if i.get("pipeline") else "")}]
    for e in i.get("evidence") or []:
        text = e if isinstance(e, str) else e.get("text") or e.get("label") or str(e)
        checks.append({"ok": None, "text": text, "source": (e.get("source") if isinstance(e, dict) else None) or "telemetry.*"})
    root = cause.get("explanation") or cause.get("title") if isinstance(cause, dict) else str(cause or "")
    chain = [f"Factory {s['name']}"] + ([f"pipeline {i['pipeline']}"] if i.get("pipeline") else []) + \
            ([" › ".join(cause["path"])] if isinstance(cause, dict) and cause.get("path") else []) + [i["title"]]
    impact = {"entity": i.get("pipeline") or s["name"], "gold_views": [], "views_label": "Tables this pipeline writes",
              "business_date": None, "amount_at_risk": None, "actions": i.get("fixes") or ["Open the run in ADF runs and follow the cause"]}
    try:
        from adf import insights
        if i.get("pipeline"):
            impact["gold_views"] = sorted({(e["target"].get("name") or e["target"].get("uri") or "?")
                                           for e in insights.pipeline_detail(s["id"], i["pipeline"]).get("lineage", [])
                                           if isinstance(e.get("target"), dict)})
    except Exception:                                                 # noqa: BLE001
        pass
    return {"found": True, "incident": inc, "checks": checks, "root_cause": root or i["summary"], "chain": chain, "sample": [],
            "impact": impact, "run_id": i.get("run_id")}
