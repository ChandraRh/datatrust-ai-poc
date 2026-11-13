"""
Ask DataTrust AI about any connected source. The model gets read-only tools (never raw access) and answers from their
results; the source picked at the top of the page is the default for every tool, and it can look at other sources by key.
The POC source also keeps its revenue / customer tools (agent.py).
"""
from __future__ import annotations

import json

import agent
import scopes

S = {"scope": {"type": "string", "description": "source key, e.g. db:1 or adf:3 (default: the selected source)"}}
TOOLS = [
    ("list_sources", "Every connected source (databases and data factories) with its key, name and type.", {}, []),
    ("get_source_overview", "Trust score (0-100), what lowers it, key figures and quality dimensions of one source.", S, []),
    ("get_incidents", "Open incidents of one source (failing checks, pipeline failures, stale data, mismatches ...), "
                      "with how long they are open and who acknowledged them.", S, []),
    ("investigate_incident", "Evidence, root cause, sample rows and impact of one incident id from get_incidents.",
     {"incident_id": {"type": "string"}}, ["incident_id"]),
    ("list_data_checks", "Data checks of a database source with their latest result.", S, []),
    ("get_check_history", "Recent results of one data check (status, measured value, message, when).",
     {"check_id": {"type": "integer"}}, ["check_id"]),
    ("get_adf_runs", "Recent pipeline runs of a Data Factory source; filter by pipeline name and status (Succeeded, Failed, ...).",
     {**S, "pipeline": {"type": "string"}, "status": {"type": "string"}, "days": {"type": "integer"}}, []),
    ("get_adf_run", "One ADF run in detail: steps, errors, diagnosed root cause, source / target checks.",
     {"run_id": {"type": "string"}}, ["run_id"]),
    ("list_tables", "Schemas and tables / views of a database source (from its catalog).", {**S, "schema": {"type": "string"}}, []),
    ("describe_table", "Columns and types of one table of a database source.",
     {**S, "schema": {"type": "string"}, "table": {"type": "string"}}, ["schema", "table"]),
    ("query_source", "Run ONE read-only SELECT on a database source (at most 200 rows). Use fully qualified table names.",
     {**S, "sql": {"type": "string"}}, ["sql"]),
]


def _trim(rows, keys, n=50):
    return [{k: r.get(k) for k in keys} for r in (rows or [])[:n]]


def runner_for(selected: str):
    def run(name: str, args: dict):
        try:
            return _run(name, args or {}, selected)
        except Exception as e:                                        # noqa: BLE001  the model sees the error and can retry
            return {"error": f"{type(e).__name__}: {e}"}
    return run


def _run(name: str, a: dict, selected: str):
    sc = scopes.resolve(a.get("scope") or selected)
    if name == "list_sources":
        return [{k: s[k] for k in ("key", "name", "type", "poc")} for s in scopes.list_scopes()]
    if name == "get_source_overview":
        o = scopes.overview(sc["key"])
        t = o["trust"]
        out = {"source": sc["name"], "trust_score": t["score"], "label": t["label"], "why_not_100": t["reasons"]}
        if o["kind"] == "poc":
            out.update(dimensions=t.get("dimensions"), incidents=o["overview"]["incidents"], coverage=o["overview"]["coverage"])
        else:
            out.update(kpis=o["kpis"], dimensions=o["dimensions"], watched=o["items"][:30])
        return out
    if name == "get_incidents":
        return _trim(scopes.incidents(sc["key"])["open"], ("id", "severity", "type", "title", "summary", "first_seen", "acknowledged_by"))
    if name == "investigate_incident":
        inv = scopes.investigate(a["incident_id"])
        return {k: inv.get(k) for k in ("found", "root_cause", "checks", "chain", "impact")} | {"sample": (inv.get("sample") or [])[:10]}
    if name == "list_data_checks":
        _need(sc, "db")
        return _trim(scopes._checks_of(sc["id"], skip_dedupe=False),
                     ("check_id", "name", "check_type", "schema_name", "table_name", "severity", "last_status", "message", "last_run_at"))
    if name == "get_check_history":
        from checks import store
        return _trim(store.history(int(a["check_id"]), 20), ("run_at", "status", "observed", "expected", "message"))
    if name == "get_adf_runs":
        _need(sc, "adf")
        from adf import insights
        return _trim(insights.runs(sc["id"], a.get("pipeline"), a.get("status"), int(a.get("days") or 30), 50),
                     ("run_id", "pipeline_name", "status", "run_start", "duration_ms", "rows_written", "cause", "message"))
    if name == "get_adf_run":
        from adf import insights
        d = insights.run_detail(a["run_id"])
        return {"run": {k: (d.get("run") or {}).get(k) for k in ("pipeline_name", "status", "run_start", "run_end", "duration_ms", "message")},
                "root_causes": d.get("root_causes"), "steps": _trim(d.get("tasks"), ("activity_name", "activity_type", "status",
                "rows_read", "rows_written", "rows_skipped", "error_message"), 30), "checks": (d.get("checks") or [])[:20]}
    if name in ("list_tables", "describe_table", "query_source"):
        _need(sc, "db")
        from estate import service
        from checks import dialects, engine
        src = service.source(sc["id"])
        db = src.cfg.get("database")
        if name == "list_tables":
            schemas = [a["schema"]] if a.get("schema") else [s["name"] for s in src.schemas(db)][:15]
            return {s: [o["name"] for o in src.objects(db, s) if o["type"] in ("TABLE", "VIEW", "MATERIALIZED VIEW")][:80] for s in schemas}
        if name == "describe_table":
            return engine.columns(sc["id"], db, a["schema"], a["table"])
        d = dialects.for_type(src.cfg["source_type"])
        rows = engine.query(sc["id"], db, d.limit(dialects.clean_select(a["sql"]), 200), max_rows=200)
        return [{k: engine._plain(v) for k, v in r.items()} for r in rows]
    raise ValueError(f"unknown tool {name}")


def _need(sc: dict, kind: str):
    if sc["kind"] != kind:
        raise ValueError(f"{sc['name']} is a {sc['type']} source; this tool needs a {'database' if kind == 'db' else 'Data Factory'} source")


def _system(sc: dict) -> str:
    others = ", ".join(f"{s['key']} = {s['name']} ({s['type']})" for s in scopes.list_scopes())
    extra = ""
    if sc["poc"]:
        extra = ("\nThis is also the POC payments pipeline: for revenue, settlement, customers and pipeline batches use the POC tools "
                 "(get_revenue, get_settlement_reconciliation, get_top_customers, ...). Money is INR, Indian format (₹18,32,333.70).")
    return (f"You are DataTrust AI, a data trust assistant. The user is looking at source {sc['key']} = {sc['name']} ({sc['type']}); "
            f"use it unless they name another. All sources: {others}.\n"
            "Answer ONLY from tool results; never invent numbers, tables or causes. If the tools cannot answer, say so and say "
            "what to add (a data check, a connection). Start with a one-line answer in bold, then 2-5 short bullets, and name "
            "the source and where each fact came from (check, run, query)." + extra)


def ask(question: str, history: list = None, scope: str | None = None) -> dict:
    sc = scopes.resolve(scope)
    import llm
    if llm.provider() == "none":
        if sc["poc"]:
            return agent.rules_agent(question)
        return _plain_answer(sc)
    tools = TOOLS + (agent.TOOLS if sc["poc"] else [])
    base = runner_for(sc["key"])
    poc_names = {t[0] for t in agent.TOOLS}

    def run(name, args):
        return agent._safe_tool(name, args) if name in poc_names and name not in {t[0] for t in TOOLS} else base(name, args)
    try:
        out = agent.llm_agent(question, history or [], tools=tools, runner=run, system=_system(sc))
    except Exception as e:                                            # noqa: BLE001
        out = agent.rules_agent(question) if sc["poc"] else _plain_answer(sc)
        import llm as _llm
        out["answer"] += f"\n\n_({_llm.describe_error(e)} This is the built-in answer.)_"
    out["source"] = {"key": sc["key"], "name": sc["name"]}
    return out


def _plain_answer(sc: dict) -> dict:
    """Without a model: the source's state in words (every number from the same functions the pages use)."""
    o = scopes.overview(sc["key"])
    inc = scopes.incidents(sc["key"])["open"]
    t = o["trust"]
    lines = [f"**{sc['name']}: trust score {t['score'] if t['score'] is not None else '–'} ({t['label']})**"]
    lines += [f"- {i['severity']}: {i['title']}" for i in inc[:5]] or ["- No open incidents."]
    lines.append("\n_Free-form questions about this source need an AI model (LLM_PROVIDER in .env)._")
    return agent.reply("\n".join(lines))
