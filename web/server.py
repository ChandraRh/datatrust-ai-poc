"""
DataTrust AI web app.

    python web/server.py            then open http://localhost:8000

Reads PostgreSQL live (gold views + audit tables). The page checks /api/version every 15 seconds:
when a pipeline run writes new data, every panel refreshes by itself.
"""
import os
import sys
from pathlib import Path

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
    return HTMLResponse((HERE / "static" / "login.html").read_text(encoding="utf-8"),
                        headers={"Cache-Control": "no-store"})


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

try:                                                    # Data checks on any connected database (own tables, own API, scheduler)
    from checks import routes as checks_routes, runner as checks_runner, store as checks_store
    app.include_router(checks_routes.router)
    checks_store.ensure_tables()
    if (n := checks_runner.seed_starter_checks()):
        print(f"Data checks: {n} starter checks added for the POC database")
    if checks_runner.start_background():
        print(f"Data checks scheduler: on (every {checks_runner.POLL_SECONDS} s)")
except Exception as e:
    print(f"!! Data checks not ready: {e}")

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
def index():
    """The dashboard. Existing alert links with dashboard fragments are redirected by landing.html."""
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
    return ds.data_version()


@app.get("/api/overview")
def overview():
    return {"overview": ds.overview(), "trust": ds.trust_score(), "pipeline": ds.pipeline_status(),
            "as_of": ds.as_of_date().isoformat(), "llm": os.getenv("LLM_PROVIDER", "none")}


@app.get("/api/incidents")
def incidents():
    return {"open": ds.incidents(), "resolved": ds.resolved_incidents()}


@app.get("/api/investigate/{incident_id}")
def investigate(incident_id: str):
    return ds.investigate(incident_id)


@app.get("/api/lineage")
def lineage():
    return ds.lineage()


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
        return {"ok": True, **batch_status.request_retry(batch_id, user["username"])}
    except ValueError as e:
        raise HTTPException(400, str(e))


class Approval(BaseModel):
    incident_id: str
    title: str = ""
    actions: list = []
    approved_by: str = "web user"
    note: str = ""


@app.post("/api/approve")
def approve(a: Approval, user: dict = Depends(estate_routes.admin_user)):
    if not a.actions:
        raise HTTPException(400, "select at least one action")
    ds.approve(a.incident_id, a.title, a.actions, a.approved_by, a.note)
    return {"ok": True}


class Question(BaseModel):
    question: str
    history: list = []


@app.post("/api/ask")
def ask(q: Question):
    return agent.ask(q.question, q.history)


if __name__ == "__main__":
    # laptop: 127.0.0.1 (only this computer). Container / cloud: WEB_HOST=0.0.0.0, and the platform's PORT if it sets one
    port = int(os.getenv("PORT") or os.getenv("WEB_PORT", "8000"))
    host = os.getenv("WEB_HOST", "127.0.0.1")
    print(f"DataTrust AI  ->  http://localhost:{port}   (listening on {host}; Ctrl+C to stop)")
    uvicorn.run(app, host=host, port=port, log_level=os.getenv("WEB_LOG_LEVEL", "warning"), proxy_headers=True,
                forwarded_allow_ips="*")
