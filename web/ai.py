"""
The AI features, each built on evidence DataTrust measured (see llm.py for the rules):

    explain(incident_id, scope)       Investigate / Impact: root cause in plain words + a fix plan, from the evidence pack
    suggest_checks(conn, db, s, t)    Add check: checks that fit a table, from its profile (works without a model too)
    briefing(scope)                   Observe: what broke, what got fixed, what to watch, for one source
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

import appdb
import llm
import scopes

GROUND = ("Use ONLY the evidence given. Never invent numbers, names, dates or causes; copy numbers exactly as they appear. "
          "If the evidence does not show the cause, say what is known and what to check next. Write plainly for a data engineer. ")


# ============================================================================ root cause + fix
def _evidence_pack(inv: dict) -> dict:
    inc = inv["incident"]
    imp = inv.get("impact") or {}
    return {"incident": {k: inc.get(k) for k in ("id", "severity", "type", "title", "summary", "business_date")},
            "metrics": {k: v for k, v in (inc.get("metrics") or {}).items() if k not in ("evidence",)},
            "evidence_checks": [{"fact": c.get("text"), "confirmed": c.get("ok"), "source": c.get("source")} for c in inv.get("checks") or []],
            "evidence_path": inv.get("chain") or [], "deterministic_root_cause": inv.get("root_cause"),
            "sample_rows": (inv.get("sample") or [])[:15], "sql_that_ran": inv.get("sql"),
            "downstream": {"label": imp.get("views_label") or "gold views", "objects": imp.get("gold_views") or []},
            "amount_at_risk": imp.get("amount_at_risk"), "standard_actions": imp.get("actions") or [],
            "adf_cause": (inc.get("adf") or {}).get("cause"), "adf_fixes": (inc.get("adf") or {}).get("fixes")}


def explain(incident_id: str, scope: str | None = None, refresh: bool = False, cached_only: bool = False) -> dict:
    inv = scopes.investigate(incident_id, scope)
    if not inv.get("found"):
        return {"available": False, "reason": "This incident is no longer open."}
    ev = _evidence_pack(inv)
    if llm.provider() == "none":
        return {"available": False, "reason": "No AI model is configured. Set LLM_PROVIDER (gemini, anthropic or ollama) in .env.",
                "root_cause": inv.get("root_cause"), "fix_steps": ev["standard_actions"]}
    system = ("You are DataTrust AI, explaining a data incident to the engineer who must fix it. " + GROUND +
              "Return JSON: {\"headline\": one sentence, \"root_cause\": 2-4 sentences, \"confidence\": \"high\"|\"medium\"|\"low\", "
              "\"because\": [3-5 short bullets, each citing a fact from the evidence], \"fix_steps\": [3-6 concrete ordered steps], "
              "\"prevent\": [1-3 steps that stop it happening again], \"business_impact\": one sentence (what downstream shows wrong), "
              "\"unknowns\": [what the evidence cannot tell, may be empty]}. Confidence is high only when the evidence checks confirm the cause.")
    prompt = "Evidence (JSON):\n" + json.dumps(ev, default=str)[:24000]
    if cached_only and not refresh:                                   # pages show a saved answer without spending a model call
        hit = llm.cached("explain", incident_id, ev)
        return {"available": True, **hit} if hit else {"available": False, "not_generated": True}
    try:
        out = llm.grounded("explain", incident_id, ev, system, prompt, refresh=refresh)
    except llm.Unavailable as e:
        return {"available": False, "reason": str(e), "root_cause": inv.get("root_cause"), "fix_steps": ev["standard_actions"]}
    return {"available": True, **out}


# ============================================================================ suggested checks for a table
def _profile(connection_id: int, database: str | None, schema: str, table: str) -> dict:
    """Column facts measured on the table: type, empty %, distinct values, min / max (dates and numbers). Read-only."""
    from checks import engine
    d = engine._dialect(connection_id)
    cols = engine.columns(connection_id, database, schema, table)[:40]
    t = d.table(schema, table)
    parts = ["COUNT(*) AS n_rows"]
    for i, c in enumerate(cols):
        q = d.ident(c["name"])
        parts.append(f"COUNT({q}) AS f{i}")
        if not any(x in c["type"].lower() for x in ("json", "xml", "bytea", "binary", "image", "text[]", "array", "geometry")):
            parts.append(f"COUNT(DISTINCT {q}) AS d{i}")
        if any(x in c["type"].lower() for x in ("date", "time", "int", "numeric", "decimal", "double", "real", "float", "money")):
            parts.append(f"MIN({q}) AS lo{i}")
            parts.append(f"MAX({q}) AS hi{i}")
    row = engine.query(connection_id, database, f"SELECT {', '.join(parts)} FROM {t}")[0]
    n = int(row["n_rows"] or 0)
    out = []
    for i, c in enumerate(cols):
        filled = int(row.get(f"f{i}") or 0)
        out.append({"column": c["name"], "type": c["type"], "nullable": c["nullable"],
                    "empty_pct": round(100 * (n - filled) / n, 2) if n else None,
                    "distinct": row.get(f"d{i}"), "unique": bool(n) and row.get(f"d{i}") == n,
                    "min": engine._plain(row.get(f"lo{i}")), "max": engine._plain(row.get(f"hi{i}"))})
    return {"table": f"{schema}.{table}", "rows": n, "columns": out}


TIME_HINT = ("loaded_at", "updated_at", "modified", "ingested", "created_at", "inserted", "_ts", "timestamp", "event_time")


def _heuristic(profile: dict) -> list:
    """Checks that follow directly from the measured profile (also the answer when no model is configured)."""
    cols, n = profile["columns"], profile["rows"]
    s = []
    times = [c for c in cols if any(x in c["type"].lower() for x in ("timestamp", "datetime", "date"))]
    best = sorted(times, key=lambda c: (not any(h in c["column"].lower() for h in TIME_HINT), "date" == c["type"].lower()))
    if best:
        s.append({"check_type": "freshness", "config": {"column": best[0]["column"], "max_age_hours": 24}, "severity": "MEDIUM",
                  "why": f"{best[0]['column']} is the newest-data marker (latest value {best[0]['max']})."})
    s.append({"check_type": "volume", "config": {"min_rows": 1, "max_drop_pct": 50}, "severity": "MEDIUM",
              "why": f"The table has {n:,} rows today; a sudden drop usually means a partial or failed load."})
    keys = [c for c in cols if c["unique"] and (c["column"].lower().endswith("id") or c["column"].lower().endswith("_key"))]
    if keys:
        s.append({"check_type": "uniqueness", "config": {"columns": [keys[0]["column"]]}, "severity": "HIGH",
                  "why": f"{keys[0]['column']} has {n:,} distinct values for {n:,} rows: it behaves like the key."})
    for c in [c for c in cols if c["empty_pct"] == 0 and c["nullable"] and not c["unique"]][:2]:
        s.append({"check_type": "null_rate", "config": {"column": c["column"], "max_null_pct": 0}, "severity": "MEDIUM",
                  "why": f"{c['column']} is never empty today although the column allows it: empties would be new."})
    s.append({"check_type": "schema", "config": {}, "severity": "HIGH", "why": "Catches dropped, renamed or retyped columns early."})
    return s


def suggest_checks(connection_id: int, database: str | None, schema: str, table: str) -> dict:
    from checks import store as checks_store
    profile = _profile(connection_id, database, schema, table)
    from checks import engine
    profile["database_type"] = engine._dialect(connection_id).name               # SQL must fit the database (PostgreSQL / SQL Server)
    base = {"connection_id": connection_id, "database": database, "schema_name": schema, "table_name": table, "schedule_minutes": 60}
    source = "profile"
    ideas = _heuristic(profile)
    if llm.provider() != "none":
        system = ("You are DataTrust AI. Propose data checks for one table from its measured profile. " + GROUND +
                  "Allowed check types and config:\n"
                  "freshness {column, max_age_hours}; volume {min_rows, max_drop_pct}; null_rate {column, max_null_pct}; "
                  "uniqueness {columns: [..]}; schema {}; custom_sql {sql (one read-only SELECT on this table that returns the BAD rows), mode: \"no_rows\"}.\n"
                  "Return JSON {\"checks\": [{\"name\", \"check_type\", \"config\", \"severity\": LOW|MEDIUM|HIGH|CRITICAL, \"why\": one sentence citing the profile}]}. "
                  "3 to 8 checks, the most useful first. Use only columns from the profile. For custom_sql use plausible business rules "
                  "visible in the profile (e.g. negative amounts, end before start), with the fully qualified table name, in the SQL dialect of "
                  "database_type. Severity: HIGH for keys and money rules, MEDIUM for most, CRITICAL only if wrong data would be "
                  "reported to customers.")
        try:
            got = llm.grounded("suggest", profile["table"], profile, system, "Table profile (JSON):\n" + json.dumps(profile, default=str))
            if got.get("checks"):
                ai_ideas = got["checks"]
                covered = {(i.get("check_type"), json.dumps((i.get("config") or {}).get("column") or (i.get("config") or {}).get("columns")))
                           for i in ai_ideas}
                # keep the profile's own basics (freshness, key, schema ...) unless the model already proposed the same
                ideas = ai_ideas + [h for h in ideas if (h["check_type"], json.dumps(h["config"].get("column") or h["config"].get("columns")))
                                    not in covered and h["check_type"] not in {i.get("check_type") for i in ai_ideas if i.get("check_type") in ("volume", "schema")}]
                source = f"ai ({got.get('model')})"
        except llm.Unavailable:
            pass
    out = []
    for i in ideas:
        body = {**base, **{k: i.get(k) for k in ("check_type", "config", "severity")}, "name": i.get("name") or _name(i, profile["table"])}
        try:
            v = checks_store.validate(body)
        except (ValueError, TypeError) as e:
            continue                                                   # the model proposed something we cannot run: dropped
        out.append({**v, "why": i.get("why")})
    return {"profile": profile, "suggestions": out, "source": source}


def _name(i: dict, table: str) -> str:
    c = i.get("config") or {}
    return {"freshness": f"{table} is fresh", "volume": f"{table} row volume", "null_rate": f"{c.get('column')} filled in {table}",
            "uniqueness": f"{' + '.join(c.get('columns') or [])} unique in {table}", "schema": f"{table} columns unchanged"}.get(
        i.get("check_type"), f"{table} rule")


# ============================================================================ daily briefing of one source
def _facts(scope: str | None) -> dict:
    o = scopes.overview(scope)
    inc = scopes.incidents(scope)
    s = o["scope"]
    st = {r["incident_id"]: r for r in appdb.rows("SELECT * FROM app.incident_state WHERE scope = %s", (s["key"],))} \
        if appdb.table_exists("app.incident_state") else {}
    now = datetime.now(timezone.utc)

    def age_h(t):
        try:
            return (now - datetime.fromisoformat(str(t))).total_seconds() / 3600
        except ValueError:
            return None
    new = [i for i in inc["open"] if (age_h((st.get(i["id"]) or {}).get("first_seen")) or 1e9) <= 24]
    gone = [r for r in inc["resolved"] if (age_h(r.get("resolved_at")) or 1e9) <= 24]
    fixed = [r for r in gone if r.get("verified")]
    unverified = [r for r in gone if not r.get("verified")]
    t = o["trust"]
    if o["kind"] == "poc":
        trend = {"metric": "trust score by business date", "points": [{"day": x["business_date"], "value": x["score"]} for x in (t.get("trend") or [])][-5:]}
        measured = True
    else:
        trend = {"metric": o.get("trend_label"), "points": [{"day": x["day"], "value": x["score"]} for x in (o.get("trend") or [])][-5:]}
        measured = t["score"] is not None
    return {"source": {"name": s["name"], "type": s["type"], "scored_from": t.get("basis") or "pipeline rules and data checks"},
            "measured": measured,
            "note": None if measured else ("Nothing is measured on this source yet: it has no data checks (database) or no pipeline "
                                           "runs (factory). This is a setup gap, not a data problem."),
            "trust_score": t["score"], "label": t["label"],
            "why_not_100": [r["reason"] for r in t["reasons"]][:8], "recent_trend": trend,
            "open_incidents": [{"severity": i["severity"], "title": i["title"], "summary": i["summary"][:200]} for i in inc["open"][:12]],
            "new_in_24h": [i["title"] for i in new],
            "resolved_in_24h": [f"{r['incident_title']} ({r['resolution_label'].lower()})" for r in fixed],
            "gone_but_not_verified_24h": [f"{r['incident_title']} ({r['resolution_label'].lower()}: {r['resolution_detail']})" for r in unverified],
            "acknowledged": [i["title"] for i in inc["open"] if i.get("acknowledged_by")]}


def _plain_briefing(f: dict) -> dict:
    lines = [f"{f['source']['name']}: trust score {f['trust_score'] if f['trust_score'] is not None else 'not measured yet'} ({f['label']})."]
    lines.append(f"{len(f['open_incidents'])} open incident(s)" + (f", {len(f['new_in_24h'])} new in the last 24 h." if f["new_in_24h"] else "."))
    if f["resolved_in_24h"]:
        lines.append(f"Resolved in the last 24 h: {', '.join(f['resolved_in_24h'][:3])}.")
    if f.get("gone_but_not_verified_24h"):
        lines.append(f"{len(f['gone_but_not_verified_24h'])} incident(s) stopped showing without a verified fix (see Verify).")
    return {"headline": lines[0], "summary": " ".join(lines[1:]), "watch": [i["title"] for i in f["open_incidents"][:3]]}


def briefing(scope: str | None, refresh: bool = False, cached_only: bool = False) -> dict:
    f = _facts(scope)
    day = datetime.now(timezone.utc).date().isoformat()
    if llm.provider() == "none":
        return {"available": False, "facts": f, **_plain_briefing(f), "reason": "No AI model is configured: this is the plain summary."}
    system = ("You are DataTrust AI writing the morning briefing for one data source. " + GROUND +
              "new_in_24h / resolved_in_24h mean first seen / resolved (verified fix) in the last 24 hours. "
              "gone_but_not_verified_24h stopped showing WITHOUT a verified fix (a rule changed or no reason found): never call them fixed. "
              "If measured is false, say plainly "
              "that nothing is monitored yet and that the next step is to add data checks (database) or connect runs (factory). "
              "recent_trend.metric names what the trend measures; do not call it the trust score unless it says so. " +
              "Return JSON {\"headline\": one sentence with the state, \"summary\": 2-4 sentences (what broke, what got fixed), "
              "\"watch\": [up to 3 things to look at today, most important first], \"good_news\": [up to 2, may be empty]}.")
    if cached_only and not refresh:
        hit = llm.cached("briefing", f"{f['source']['name']}:{day}", f)
        return {"available": True, "facts": f, **hit} if hit else {"available": False, "not_generated": True, "facts": f, **_plain_briefing(f)}
    try:
        out = llm.grounded("briefing", f"{f['source']['name']}:{day}", f, system, "Facts (JSON):\n" + json.dumps(f, default=str), refresh)
    except llm.Unavailable as e:
        return {"available": False, "facts": f, **_plain_briefing(f), "reason": str(e)}
    return {"available": True, "facts": f, **out}
