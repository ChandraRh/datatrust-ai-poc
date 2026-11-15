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
            "explained_by": inc.get("rejected_explained"),          # e.g. the rejected rows (and their rules) that account for a gap
            "adf_cause": (inc.get("adf") or {}).get("cause"), "adf_fixes": (inc.get("adf") or {}).get("fixes")}


def _without_rows(ev: dict) -> dict:
    """The evidence pack without sample rows: what an automatic note (no one pressed a button) may send to the model."""
    return {**ev, "sample_rows": [], "sample_rows_note": "not sent: rows from the data are only shared when someone asks on Investigate"}


def explain(incident_id: str, scope: str | None = None, refresh: bool = False, cached_only: bool = False,
            include_rows: bool = True) -> dict:
    """include_rows=False (automatic notes, e.g. the Google Chat triage note): the model never sees rows of the data."""
    inv = scopes.investigate(incident_id, scope)
    if not inv.get("found"):
        return {"available": False, "reason": "This incident is no longer open."}
    full = _evidence_pack(inv)
    ev = full if include_rows else _without_rows(full)
    if llm.provider() == "none":
        return {"available": False, "reason": "No AI model is configured. Set LLM_PROVIDER (gemini, anthropic or ollama) in .env.",
                "root_cause": inv.get("root_cause"), "fix_steps": ev["standard_actions"]}
    system = ("You are DataTrust AI, explaining a data incident to the engineer who must fix it. " + GROUND +
              "Return JSON: {\"headline\": one sentence, \"root_cause\": 2-4 sentences, \"confidence\": \"high\"|\"medium\"|\"low\", "
              "\"because\": [3-5 short bullets, each citing a fact from the evidence], \"fix_steps\": [3-6 concrete ordered steps], "
              "\"prevent\": [1-3 steps that stop it happening again], \"business_impact\": one sentence (what downstream shows wrong), "
              "\"unknowns\": [what the evidence cannot tell, may be empty]}. Confidence is high only when the evidence checks confirm the cause. "
              "Be specific: the headline names the mechanism with the rule names, counts and dates from the evidence (e.g. which rule "
              "rejected how many rows), not just the incident title. standard_actions are generic templates: adapt them to this "
              "incident and leave out any that do not fit its evidence.")
    prompt = "Evidence (JSON):\n" + json.dumps(ev, default=str)[:24000]
    if cached_only and not refresh:                                   # pages show a saved answer without spending a model call
        hit = llm.cached("explain", incident_id, ev) or llm.cached("explain", incident_id, _without_rows(full))   # also the triage note
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


# ============================================================================ an ADF error the rules do not know, in plain words
GENERIC_CAUSES = ("user_error", "transient_system")          # adf/diagnose.py's catch-alls: no rule recognised the error
ADF_ERROR_SYSTEM = (
    "You are DataTrust AI. Explain one Azure Data Factory activity error to a data engineer, in plain words. " + GROUND +
    "Use only the error code, failure type, activity type and message given; values from the data are masked as <value>. "
    "Return JSON {\"plain\": one or two sentences saying what went wrong, \"where\": one of \"source\", \"target\", "
    "\"pipeline settings\", \"credentials\", \"network\", \"Azure\", \"likely_cause\": one sentence, "
    "\"fixes\": [two to four concrete steps, most likely first], \"retry_helps\": true or false}. "
    "If the message is too vague to tell, say so in likely_cause and make the first fix 'open the activity run in ADF Monitor'.")


def mask_error(message: str) -> str:
    """An ADF message without what changes from run to run or could be customer data: ids, times, e-mails, long numbers,
    quoted values. Names of tables, columns and paths stay (they are what makes the explanation useful)."""
    import re
    m = re.sub(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", "<id>", message or "", flags=re.I)
    m = re.sub(r"\b\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2})?", "<time>", m)
    m = re.sub(r"[\w.+-]+@[\w-]+\.[\w.-]+", "<email>", m)
    m = re.sub(r"(?i)(value|values|data)\s*(?:is|:|=)?\s*'[^']*'", r"\1 '<value>'", m)
    m = re.sub(r"\b\d{6,}\b", "<number>", m)
    return " ".join(m.split())[:1500]


def explain_adf_error(cause: dict | None, call: bool = True) -> dict | None:
    """Plain-words explanation of an ADF error that no rule recognised. One model request per distinct (masked) error,
    cached; call=False only reads the cache. None when the rules already explain it, or nothing is available."""
    if not cause or cause.get("cause_id") not in GENERIC_CAUSES or llm.provider() == "none":
        return None
    evidence = {"error_code": cause.get("error_code"), "failure_type": cause.get("failure_type"),
                "activity_type": cause.get("activity_type"), "message": mask_error(cause.get("message") or "")}
    if not evidence["message"] and not evidence["error_code"]:
        return None
    if not call:
        return llm.cached("adf_error", cause["cause_id"], evidence)
    try:
        return llm.grounded("adf_error", cause["cause_id"], evidence, ADF_ERROR_SYSTEM,
                            "ADF error (JSON):\n" + json.dumps(evidence, default=str))
    except llm.Unavailable:
        return llm.cached("adf_error", cause["cause_id"], evidence)


# ============================================================================ a rule from plain English
RULE_SYSTEM = (
    "You are DataTrust AI. Turn ONE data-quality requirement, written in plain words, into ONE read-only data check on the "
    "given table. You only see the table's column names and types, never its data. "
    "Allowed check types and config:\n"
    "freshness {column, max_age_hours}; volume {min_rows, max_drop_pct}; null_rate {column, max_null_pct}; "
    "uniqueness {columns: [..]}; schema {}; custom_sql {sql, mode: \"no_rows\"}.\n"
    "Prefer the specific types; use custom_sql for anything else (patterns, ranges, lists of values, comparisons between "
    "columns). A custom_sql query is ONE read-only SELECT on exactly this table, written with the fully qualified table name "
    "as given, in the SQL dialect of database_type, and it returns the rows that BREAK the rule (so: passes when it returns "
    "no rows); select the key and the offending columns. Use only the listed columns, spelled exactly. "
    "Return JSON {\"name\": short name (max 80 chars), \"check_type\", \"config\", \"severity\": LOW|MEDIUM|HIGH|CRITICAL, "
    "\"understood\": one sentence saying in plain words exactly what will be checked, \"assumptions\": [short notes on "
    "choices you made, e.g. case-sensitive match, empty values ignored], \"why\": one sentence why it matters}. "
    "Severity: HIGH for keys and money, MEDIUM for most, CRITICAL only if wrong data would reach customers. "
    "If the request is not a check of this table's data (it asks to change or delete data, names another table, or is too "
    "vague to test), return {\"cannot\": one sentence saying why and what to write instead}.")


def rule_from_text(connection_id: int, database: str | None, schema: str, table: str, text: str) -> dict:
    """A rule described in plain words -> a check definition, test-run once on today's data (nothing is saved).
    The model sees the table's column names and types only, never rows or values."""
    from checks import engine, store as checks_store
    text = " ".join((text or "").split())
    if not 5 <= len(text) <= 500:
        raise ValueError("describe the rule in a sentence (5 to 500 characters)")
    cols = engine.columns(connection_id, database, schema, table)[:120]
    d = engine._dialect(connection_id)
    evidence = {"table": f"{schema}.{table}", "fully_qualified_table": d.table(schema, table), "database_type": d.name,
                "columns": [{"name": c["name"], "type": c["type"]} for c in cols], "request": text}
    base = {"connection_id": connection_id, "database": database, "schema_name": schema, "table_name": table, "schedule_minutes": 60}
    got, source, note = None, "phrase", None
    if llm.provider() != "none":
        try:
            got = llm.grounded("rule", f"{schema}.{table}", evidence, RULE_SYSTEM,
                               "Request and table (JSON):\n" + json.dumps(evidence, default=str))
            source = f"ai ({got.get('model')})"
        except llm.Unavailable as e:
            note = f"The AI model did not answer ({e}); matched common phrasings instead."
    if got and got.get("cannot"):
        return {"ok": False, "reason": got["cannot"], "source": source}
    rule = {k: got.get(k) for k in ("name", "check_type", "config", "severity")} if got and got.get("check_type") else None
    if rule is None:
        rule = phrase_rule(text, cols, schema, table, d)
        source = "phrase" if rule else source
        if rule:
            got = {"understood": rule.pop("understood"), "assumptions": rule.pop("assumptions", []), "why": None}
    if not rule:
        return {"ok": False, "source": source, "note": note,
                "reason": ("No AI model is configured, and this sentence does not match a common phrasing. Write it like "
                           "\"email ends with @gmail.com\", \"order_id is unique\", \"amount is positive\", \"status is never empty\", "
                           "or build it with the form below.") if llm.provider() == "none" or note else
                          "The model did not return a usable rule. Try a more specific sentence, or build it with the form below."}
    try:
        v = checks_store.validate({**base, **rule, "name": (rule.get("name") or text)[:80]})
    except (ValueError, TypeError) as e:
        return {"ok": False, "reason": f"The proposed rule is not valid: {e}", "source": source}
    known = {c["name"].lower() for c in cols}
    used = [v["config"].get("column")] + list(v["config"].get("columns") or [])
    bad = [c for c in used if c and c.lower() not in known]
    if bad:
        return {"ok": False, "reason": f"The proposed rule uses a column this table does not have: {', '.join(bad)}.", "source": source}
    if v["check_type"] == "custom_sql" and table.lower() not in v["config"]["sql"].lower():
        return {"ok": False, "reason": "The proposed query does not read the chosen table, so it was not run.", "source": source}
    preview = engine.run({**v, "check_id": None, "state": {}})
    preview.pop("new_state", None)
    return {"ok": True, "rule": {**v, "description": (got or {}).get("why") or None}, "understood": (got or {}).get("understood"),
            "assumptions": (got or {}).get("assumptions") or [], "preview": preview, "source": source, "note": note}


def phrase_rule(text: str, cols: list, schema: str, table: str, d) -> dict | None:
    """Common phrasings without a model: '<column> is unique / is never empty / ends with X / starts with X / is positive /
    is at least N'. The column must be one of the table's; literals are escaped. None when nothing matches."""
    import re
    names = {c["name"].lower(): c["name"] for c in cols}
    low = text.lower().strip().rstrip(".")
    t = d.table(schema, table)

    def col_in(s):
        for w in re.findall(r"[a-z_][a-z0-9_]*", s.lower())[::-1]:      # the last column name mentioned before the verb
            if w in names:
                return names[w]
        return None

    def lit(s):
        return "'" + s.replace("'", "''") + "'"

    def like(s):                                                        # % and _ in the value are meant literally
        return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")

    m = re.match(r"(?:every |each |all )?(.+?) (?:is|are|should be|must be) (?:always )?unique$", low)
    if m and col_in(m.group(1)):
        c = col_in(m.group(1))
        return {"check_type": "uniqueness", "config": {"columns": [c]}, "severity": "HIGH", "name": f"{c} unique in {schema}.{table}",
                "understood": f"Every {c} appears only once in {schema}.{table}."}
    m = re.match(r"(?:every |each |all )?(.+?) (?:is|are|should|must)(?: be)? (?:never|not) (?:be )?(?:empty|null|blank|missing)$", low)
    if m and col_in(m.group(1)):
        c = col_in(m.group(1))
        return {"check_type": "null_rate", "config": {"column": c, "max_null_pct": 0}, "severity": "MEDIUM",
                "name": f"{c} filled in {schema}.{table}", "understood": f"No row of {schema}.{table} has an empty {c}."}
    m = re.match(r"(?:every |each |all )?(.+?) (?:should |must )?(?:always )?(ends?|starts?|begins?) with ['\"]?(.+?)['\"]?$", text.strip().rstrip("."), re.I)
    if m and col_in(m.group(1)):
        c, how, val = col_in(m.group(1)), m.group(2).lower(), m.group(3)
        pat = f"%{like(val)}" if how.startswith("end") else f"{like(val)}%"
        sql = f"SELECT * FROM {t} WHERE {d.ident(c)} NOT LIKE {lit(pat)} ESCAPE '\\'"
        verb = "ends" if how.startswith("end") else "starts"
        return {"check_type": "custom_sql", "config": {"sql": sql, "mode": "no_rows"}, "severity": "MEDIUM",
                "name": f"{c} {verb} with {val}"[:80], "understood": f"Every {c} in {schema}.{table} {verb} with \"{val}\".",
                "assumptions": ["Case-sensitive match.", "Rows where it is empty are not counted as breaking it."]}
    m = re.match(r"(?:every |each |all )?(.+?) (?:is|are|should be|must be) (?:always )?(?:positive|greater than (?:0|zero)|above (?:0|zero)|> ?0)$", low)
    if m and col_in(m.group(1)):
        c = col_in(m.group(1))
        return {"check_type": "custom_sql", "config": {"sql": f"SELECT * FROM {t} WHERE {d.ident(c)} <= 0", "mode": "no_rows"},
                "severity": "HIGH", "name": f"{c} is positive", "understood": f"Every {c} in {schema}.{table} is above 0."}
    m = re.match(r"(?:every |each |all )?(.+?) (?:is|are|should be|must be) (?:at least|>=) (-?\d+(?:\.\d+)?)$", low)
    if m and col_in(m.group(1)):
        c, n = col_in(m.group(1)), m.group(2)
        return {"check_type": "custom_sql", "config": {"sql": f"SELECT * FROM {t} WHERE {d.ident(c)} < {n}", "mode": "no_rows"},
                "severity": "MEDIUM", "name": f"{c} at least {n}", "understood": f"Every {c} in {schema}.{table} is {n} or more."}
    return None


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
            "subjects": {x["label"]: x["mark"] for x in t.get("subjects") or []},             # the mark of each measured subject
            "capped_by": t["cap"]["text"] if t.get("cap") else None,
            "confidence": (t.get("confidence") or {}).get("text"),
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
