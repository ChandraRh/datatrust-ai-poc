"""
DataTrust AI web app.

    python web/server.py            then open http://localhost:8000

Reads PostgreSQL live (gold views + audit tables). The page checks /api/version every 15 seconds:
when a pipeline run writes new data, every panel refreshes by itself.
"""
import os
import sys
from pathlib import Path

from typing import Optional

import uvicorn
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.middleware.sessions import SessionMiddleware

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import agent          # noqa: E402
import data_service as ds  # noqa: E402
import batch_status  # noqa: E402
import pipeline_trigger  # noqa: E402  Azure: a retried batch starts the pipeline job via its queue
import scopes  # noqa: E402  one source at a time: overview / incidents / investigate per connection

app = FastAPI(title="DataTrust AI")
app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")

# DataTrust's own database (APP_PG_*): created on first start; DataTrust's tables move out of the POC database once
import appdb  # noqa: E402
try:
    _prep = appdb.prepare()
    print(f"DataTrust database: {_prep['database']}" + (" (created)" if _prep.get("created") else "")
          + (f"; moved from the POC database: {_prep['copied']}" if _prep.get("copied") else ""))
except Exception as e:
    print(f"!! DataTrust database ({appdb.label()}) not ready: {e}")

# Data Estate module (connections, catalog, changes, access): separate package, own API under /api/estate
from estate import routes as estate_routes, store as estate_store  # noqa: E402
app.include_router(estate_routes.router)
try:
    estate_store.init()
except Exception as e:
    print(f"!! Data Estate module not ready: {e}")
    session_secret = os.getenv("PORTAL_SECRET") or os.urandom(32).hex()
else:
    session_secret = estate_store.session_secret()
app.add_middleware(SessionMiddleware, secret_key=session_secret, same_site="lax",
                   https_only=os.getenv("PORTAL_COOKIE_SECURE", "false").lower() == "true")

import auth as auth_routes  # noqa: E402
app.include_router(auth_routes.router)


@app.get("/login")
def login_page(request: Request):
    if not estate_store.auth_enabled():
        return RedirectResponse("/app", status_code=303)
    if estate_store.read_session(request.cookies.get("dt_session", "")):
        return RedirectResponse("/app", status_code=303)
    page = _page("login.html")                       # stamps login.css / login.js so an update is never served stale
    page.headers["Cache-Control"] = "no-store"
    return page


@app.exception_handler(Exception)
async def on_error(request, exc):
    """Any database / query problem is shown on the page instead of a blank screen."""
    return JSONResponse(status_code=500, content={"error": f"{type(exc).__name__}: {exc}"})


try:                                                    # PostgreSQL lifecycle state for ADLS batches
    batch_status.ensure_table()
except Exception as e:
    print(f"!! could not prepare audit.adls_batch_runs: {e}")

try:                                                    # DQ Rule Studio + Rejected records (own tables, own API)
    import dq
    import dq_routes
    app.include_router(dq_routes.router)
    dq.ensure_tables()
except Exception as e:
    print(f"!! DQ Rule Studio / Rejected records not ready: {e}")

import history_routes  # noqa: E402  Audit & approvals page (admins): reads the tables above, has none of its own
app.include_router(history_routes.router)

try:                                                    # Azure Data Factory connector (own tables, own API, collector thread)
    from adf import collector as adf_collector, routes as adf_routes, store as adf_store
    app.include_router(adf_routes.router)
    app.include_router(adf_routes.hooks)
    adf_store.ensure_tables()
    if adf_store.bootstrap_from_env():
        print("Azure Data Factory: factory from .env (ADF_FACTORY_NAME) added")
    if adf_collector.start_background():
        print(f"Azure Data Factory collector: on (each factory every {adf_collector.POLL_SECONDS} s)")
except Exception as e:
    print(f"!! Azure Data Factory connector not ready: {e}")

try:                                                    # who owns which source / table / pipeline (owners.py)
    import owners
    owners.ensure_tables()
except Exception as e:
    print(f"!! Owners not ready (incidents fall back to the Connect guess): {e}")

try:                                                    # Built-in check settings (Data quality -> Built-in checks)
    import builtin, builtin_routes
    app.include_router(builtin_routes.router)
    builtin.ensure_tables()
except Exception as e:
    print(f"!! Built-in check settings not ready (defaults are used): {e}")

try:                                                    # Data checks on any connected database (own tables, own API, scheduler)
    from checks import routes as checks_routes, runner as checks_runner, store as checks_store
    app.include_router(checks_routes.router)
    checks_store.ensure_tables()
    if (n := checks_runner.seed_starter_checks()):
        print(f"Data checks: {n} starter checks added for the POC database")

    scopes.ensure_tables()
    import llm  # noqa: E402  AI answers cache (app.ai_notes)
    llm.ensure_tables()
    if checks_runner.start_background():
        print(f"Data checks scheduler: on (every {checks_runner.POLL_SECONDS} s)")
except Exception as e:
    print(f"!! Data checks not ready: {e}")

try:                                                    # retention: remove DataTrust's own run history older than RETENTION_DAYS
    import retention
    if retention.start_background():
        print(f"Retention: on (keeps {retention.days()} days of results and ADF run history; audit trail kept forever)")
except Exception as e:
    print(f"!! Retention not started: {e}")

try:                                                    # incident fixes: requested, then approved by an admin (fix_requests.py)
    import fix_requests
    fix_requests.ensure_table()
except Exception as e:
    print(f"!! Fix requests not ready: {e}")

try:                                                    # lineage map (4. Explain): one graph per source, read on demand
    from lineage import store as lineage_store
    lineage_store.ensure_table()
except Exception as e:
    print(f"!! Lineage map not ready: {e}")

try:                                                    # trust score over time (Observe), every source, even with no page open
    import score_history
    score_history.ensure_table()
    if score_history.start_background():
        print(f"Trust score history: on (every source every {score_history.interval_seconds() // 60} min)")
except Exception as e:
    print(f"!! Trust score history not started: {e}")

try:                                                    # Google Chat alerts (only when GOOGLE_CHAT_WEBHOOK_URL is set)
    import alerts
    if alerts.start_background():
        print(f"Google Chat alerts: on ({alerts.masked()}, severity {alerts.min_severity()}+, every {alerts.interval()} s)"
              if alerts.webhook() else "Google Chat alerts: waiting for GOOGLE_CHAT_WEBHOOK_URL in .env (no restart needed)")
except Exception as e:
    print(f"!! Google Chat alerts not started: {e}")


def _page(name: str) -> HTMLResponse:
    """A page from web/static, with every /static/*.css|js link stamped with that file's last-change time (?v=...),
    so after an update the browser always loads the new files instead of old cached copies."""
    import re
    html = (HERE / "static" / name).read_text(encoding="utf-8")

    def stamp(m):
        f = HERE / "static" / m.group(2)
        return f'{m.group(1)}/static/{m.group(2)}?v={int(f.stat().st_mtime)}"' if f.exists() else m.group(0)
    html = re.sub(r'((?:src|href)=")/static/([^"?]+\.(?:css|js))"', stamp, html)
    return HTMLResponse(html, headers={"Cache-Control": "no-cache"})


@app.get("/")
def landing():
    """Public product landing page; dashboard remains available at /app."""
    return _page("landing.html")


@app.get("/app")
def index(request: Request):
    """The dashboard; needs a portal sign-in (unless PORTAL_AUTH_ENABLED=false). Signed out -> /login. The browser keeps
    the #fragment across the redirect, so an alert link such as /app#investigate=INC-1 opens that page after sign-in."""
    if estate_store.auth_enabled() and not estate_store.read_session(request.cookies.get("dt_session", "")):
        return RedirectResponse("/login", status_code=303)
    return _page("index.html")


@app.get("/health")
def health():
    """For the cloud load balancer / container health check: 200 when the app and its own database answer."""
    try:
        appdb.one("SELECT 1 AS ok")
        return {"status": "ok"}
    except Exception as e:
        return JSONResponse(status_code=503, content={"status": "database unavailable", "error": type(e).__name__})


@app.get("/api/version")
def version():
    """Changes whenever any source writes something: the POC pipeline, a data check result, or an ADF sync."""
    v = ds.data_version()
    try:
        extra = appdb.one("""SELECT (SELECT max(result_id) FROM monitor.results) AS r,
                                    (SELECT max(last_sync_finished)::text FROM telemetry.sources) AS t,
                                    (SELECT count(*) FROM app.remediation_log) AS a""")
        v["signature"] += f"|{extra.get('r')}|{extra.get('t')}|{extra.get('a')}"
    except Exception:
        pass
    return v


def _scoped(fn, *a):
    try:
        return fn(*a)
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.get("/api/scopes")
def scopes_list():
    """The sources a user can pick at the top of the page; every page then shows only that source."""
    return {"scopes": scopes.list_scopes()}


@app.get("/api/overview")
def overview(scope: Optional[str] = None):
    o = _scoped(scopes.overview, scope)
    o["llm"] = os.getenv("LLM_PROVIDER", "none")
    try:                                                # the score just computed also goes into its history (if it changed)
        import score_history
        score_history.record(o["scope"]["key"], o["trust"])
    except Exception as e:                              # noqa: BLE001  the page never fails because of the history
        print(f"!! trust history not recorded: {e}")
    return o


@app.get("/api/trust/history")
def trust_history(scope: Optional[str] = None, hours: int = 168):
    """Observe, Trust score over time: the source's recorded scores and the incidents that opened / closed meanwhile."""
    import score_history
    s = _scoped(scopes.resolve, scope)
    return score_history.history(s["key"], hours)


@app.get("/api/incidents")
def incidents(scope: Optional[str] = None):
    return _scoped(scopes.incidents, scope)


@app.get("/api/incidents/resolved")
def resolved_incidents(scope: Optional[str] = None, all: bool = False, date_from: Optional[str] = None,
                       date_to: Optional[str] = None, verified: Optional[str] = None, q: Optional[str] = None):
    """Verify page, Resolved incidents: every resolution on record (not only the last 7 days), this source or all."""
    return _scoped(scopes.resolved_history, scope, all, date_from, date_to, verified, q)


@app.get("/api/incidents/resolved.csv")
def resolved_incidents_csv(scope: Optional[str] = None, all: bool = False, date_from: Optional[str] = None,
                           date_to: Optional[str] = None, verified: Optional[str] = None, q: Optional[str] = None):
    from datetime import date
    import history
    from fastapi.responses import Response
    rows = [dict(r, verified="yes" if r["verified"] else "no")
            for r in _scoped(scopes.resolved_history, scope, all, date_from, date_to, verified, q)["rows"]]
    return Response(history.to_csv(rows, scopes.RESOLVED_COLUMNS), media_type="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="datatrust-resolved-incidents-{date.today()}.csv"'})


@app.get("/api/quality/rows")
def quality_rows(scope: Optional[str] = None):
    """Rejected records for any source other than the POC: the rows its quality findings point at."""
    import quality
    return _scoped(quality.rows, scope)


@app.get("/api/quality/rules")
def quality_rules(scope: Optional[str] = None):
    """Rule Studio for any source other than the POC: its checks as rules, or a factory's Copy data settings."""
    import quality
    return _scoped(quality.rules, scope)


@app.post("/api/incidents/{incident_id}/ack")
def acknowledge(incident_id: str, user: dict = Depends(estate_routes.current_user)):
    _scoped(scopes.acknowledge, incident_id, user["username"])
    return {"ok": True}


class Owner(BaseModel):
    scope: str
    target: str = ""
    person_id: int


@app.get("/api/owners")
def owners_list(scope: str, user: dict = Depends(estate_routes.current_user)):
    """Who owns this source, and which of its tables / pipelines."""
    import owners
    return owners.list_for(scope)


@app.put("/api/owners")
def owners_set(body: Owner, user: dict = Depends(estate_routes.admin_user)):
    import owners
    _scoped(owners.set_owner, body.scope, body.target, body.person_id, user["username"])
    estate_store.audit(user["username"], "OWNER_SET", f"{body.scope} {body.target or '(whole source)'}", f"person {body.person_id}")
    return {"ok": True}


@app.delete("/api/owners")
def owners_remove(scope: str, person_id: int, target: str = "", user: dict = Depends(estate_routes.admin_user)):
    import owners
    owners.remove_owner(scope, target, person_id)
    estate_store.audit(user["username"], "OWNER_REMOVED", f"{scope} {target or '(whole source)'}", f"person {person_id}")
    return {"ok": True}


class Recheck(BaseModel):
    incident_id: str


@app.post("/api/recheck")
def recheck(body: Recheck, user: dict = Depends(estate_routes.current_user)):
    """Check one incident again now (its rule, its factory, or the pipeline's latest results) and say if it closed."""
    return _scoped(scopes.recheck, body.incident_id, user["username"])


@app.get("/api/investigate/{incident_id}")
def investigate(incident_id: str, scope: Optional[str] = None):
    return _scoped(scopes.investigate, incident_id, scope)


@app.get("/api/investigate/{incident_id}/followups")
def investigate_followups(incident_id: str, scope: Optional[str] = None, refresh: bool = False):
    """Step 1: the follow-up checks for the incident's kind of problem, run read-only on its database (no AI)."""
    import followups
    return _scoped(followups.plan, incident_id, scope, refresh)


@app.get("/api/investigate/{incident_id}/dig")
def investigate_dig_cached(incident_id: str, scope: Optional[str] = None):
    """Step 2, the last answer only: AI-proposed queries for this evidence, run again; no model call."""
    import followups
    return _scoped(followups.dig, incident_id, scope, False, True)


@app.post("/api/investigate/{incident_id}/dig")
def investigate_dig(incident_id: str, scope: Optional[str] = None, refresh: bool = False,
                    user: dict = Depends(estate_routes.current_user)):
    """Step 2: the AI model proposes up to 3 read-only queries, DataTrust checks and runs them, the model reads the results."""
    import followups
    out = _scoped(followups.dig, incident_id, scope, refresh, False)
    if out.get("queries"):
        from estate import store as estate_store
        estate_store.audit(user.get("username") or "local", "ai_dig", incident_id,
                           f"{sum(1 for q in out['queries'] if 'rows' in q)} AI query(ies) run, "
                           f"{sum(1 for q in out['queries'] if q.get('refused'))} refused")
    return out


@app.get("/api/lineage/map")
def lineage_map(scope: Optional[str] = None, control: bool = False, refresh: bool = False):
    """4. Explain, Lineage map: what each system knows about itself (catalogs, code, pipeline definitions, recent runs),
    merged across sources, cut to the picked source and what other sources read from or write into it."""
    from lineage import graph as lineage_graph, store as lineage_store
    all_scopes = scopes.list_scopes()
    s = _scoped(scopes.resolve, scope)
    snaps = lineage_store.snapshots(all_scopes, refresh=refresh)
    return {"scope": s, **lineage_graph.view(s, snaps, all_scopes, control=control)}


@app.get("/api/lineage")
def lineage(scope: Optional[str] = None):
    s = _scoped(scopes.resolve, scope)
    if s["poc"]:
        return {**ds.lineage(), "kind": "poc"}
    return {"kind": s["kind"], **_scoped(scopes.lineage, s)}


@app.get("/api/autofix/{incident_id}")
def autofix_proposal(incident_id: str):
    """What the auto-fix would change for this incident, with the evidence per row (read-only)."""
    import autofix
    return autofix.proposal(incident_id)


@app.get("/api/corrections")
def corrections():
    import autofix
    return autofix.corrections()


@app.get("/api/pipeline")
def pipeline():
    return {**ds.pipeline_status(), "adls_batches": batch_status.list_batches()}


@app.post("/api/pipeline/batches/{batch_id}/retry")
def retry_pipeline_batch(batch_id: int, user: dict = Depends(estate_routes.admin_user)):
    try:
        row = batch_status.request_retry(batch_id, user["username"])
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True, **row, **pipeline_trigger.start_pipeline(row["batch_name"], user["username"])}


class Approval(BaseModel):
    incident_id: str
    title: str = ""
    actions: list = []
    note: str = ""


@app.post("/api/approve")
def approve(a: Approval, user: dict = Depends(estate_routes.admin_user)):
    """Recorded under the signed-in user, never a name typed on the page (the approval is an audit record)."""
    return _scoped(scopes.approve, a.incident_id, a.title, a.actions, a.note, user["username"])


class Question(BaseModel):
    question: str
    history: list = []
    scope: Optional[str] = None


@app.post("/api/ask")
def ask(q: Question):
    """Ask DataTrust AI about the selected source (or any other by name); read-only tools, answers from their results."""
    import ask as ask_module
    return _scoped(ask_module.ask, q.question, q.history, q.scope)


# ---------------------------------------------------------------- AI features (llm.py rules: evidence only, numbers checked)
@app.get("/api/ai/status")
def ai_status():
    import llm
    return {"provider": llm.provider(), "model": llm.model_name()}


@app.get("/api/ai/explain/{incident_id}")
def ai_explain(incident_id: str, scope: Optional[str] = None, refresh: bool = False, cached_only: bool = False):
    """Root cause in plain words + a fix plan for one incident, written from its evidence pack (cached_only: no model call)."""
    import ai
    return _scoped(ai.explain, incident_id, scope, refresh, cached_only)


@app.get("/api/ai/briefing")
def ai_briefing(scope: Optional[str] = None, refresh: bool = False, cached_only: bool = False):
    """What broke, what got fixed and what to watch, for the selected source (cached_only: no model call)."""
    import ai
    return _scoped(ai.briefing, scope, refresh, cached_only)


@app.post("/api/ai/briefing/send")
def ai_briefing_send(scope: Optional[str] = None, user: dict = Depends(estate_routes.admin_user)):
    """Post the selected source's briefing to the Google Chat space (alerts webhook)."""
    import ai
    import alerts
    b = _scoped(ai.briefing, scope)
    try:
        alerts.send(alerts.text_message(alerts.briefing_text(b)))
    except Exception as e:
        raise HTTPException(400, f"Could not post to Google Chat: {e}")
    return {"ok": True}


if __name__ == "__main__":
    # laptop: 127.0.0.1 (only this computer). Container / cloud: WEB_HOST=0.0.0.0, and the platform's PORT if it sets one
    port = int(os.getenv("PORT") or os.getenv("WEB_PORT", "8000"))
    host = os.getenv("WEB_HOST", "127.0.0.1")
    print(f"DataTrust AI  ->  http://localhost:{port}   (listening on {host}; Ctrl+C to stop)")
    uvicorn.run(app, host=host, port=port, log_level=os.getenv("WEB_LOG_LEVEL", "warning"), proxy_headers=True,
                forwarded_allow_ips="*")
