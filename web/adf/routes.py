"""
/api/estate/adf/*   the Azure Data Factory pages' API (portal login required; connecting/changing a factory needs admin)
/api/adf/hooks/{id} called BY an ADF pipeline (Web activity) when it finishes, so DataTrust syncs at once instead of
                    waiting for the next poll. Needs the factory's webhook token; the body is not trusted - it only
                    asks for a sync, and the data itself is always read from Azure.
"""
from __future__ import annotations

import threading
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel

from estate import store as estate_store
from estate.routes import admin_user, current_user

from . import collector, insights, store
from .client import AdfClient

router = APIRouter(prefix="/api/estate/adf")
hooks = APIRouter(prefix="/api/adf")


def guard(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except ValueError as e:
        raise HTTPException(400, str(e))


class SourceBody(BaseModel):
    name: str = ""
    config: dict
    enabled: bool = True


def _hide(rows: list, user: dict) -> list:
    if user["role"] != "admin":
        for r in rows:
            for k in ("client_secret_env", "storage_connection_env", "tenant_id", "client_id"):
                (r.get("config") or {}).pop(k, None)
    return rows


@router.get("/sources")
def sources(user: dict = Depends(current_user)):
    return _hide(insights.sources(), user)


@router.get("/draft")
def get_draft(user: dict = Depends(admin_user)):
    """The Connect ADF form as the admin last saved it (survives restarts), or null."""
    return store.get_draft()


@router.put("/draft")
def save_draft(body: SourceBody, user: dict = Depends(admin_user)):
    return {"saved": store.save_draft(body.name, body.config, user["username"])}


@router.delete("/draft")
def clear_draft(user: dict = Depends(admin_user)):
    store.clear_draft()
    return {"ok": True}


@router.post("/test")
def test(body: SourceBody, user: dict = Depends(admin_user)):
    """Try the settings step by step and say exactly which step fails (sign-in, factory, pipelines, run history)."""
    store.save_draft(body.name, body.config, user["username"])          # testing keeps what was typed
    steps = []
    try:
        cfg = store.validate_config(body.config)
    except ValueError as e:
        return {"ok": False, "steps": [{"step": "Settings", "ok": False, "detail": str(e)}]}
    try:
        client = AdfClient(cfg)
        client._bearer()
        steps.append({"step": "Sign in to Azure", "ok": True, "detail": "token received"
                      + (f" for app {cfg['client_id']}" if cfg["auth_mode"] == "service_principal" else " (default credential)")})
        f = client.factory()
        steps.append({"step": "Factory", "ok": True, "detail": f"{f.get('name')} in {f.get('location')}"})
        pipes = client.pipelines()
        steps.append({"step": "Read pipelines", "ok": True, "detail": f"{len(pipes)} pipeline(s): "
                      + ", ".join(p["name"] for p in pipes[:8]) + (" …" if len(pipes) > 8 else "")})
        from datetime import datetime, timedelta, timezone
        now = datetime.now(timezone.utc)
        runs = client.pipeline_runs(now - timedelta(days=cfg["lookback_days"]), now)
        steps.append({"step": "Read run history", "ok": True, "detail": f"{len(runs)} run(s) in the last {cfg['lookback_days']} day(s)"})
        if runs:
            acts = client.activity_runs(runs[-1]["runId"], now - timedelta(days=cfg["lookback_days"] + 1), now + timedelta(days=1))
            steps.append({"step": "Read activity runs", "ok": True, "detail": f"{len(acts)} step(s) in the newest run"})
        return {"ok": True, "steps": steps}
    except Exception as e:                                               # noqa: BLE001
        steps.append({"step": "Error", "ok": False, "detail": collector.explain_azure_error(e, cfg)})
        return {"ok": False, "steps": steps}


@router.post("/sources")
def add_source(body: SourceBody, user: dict = Depends(admin_user)):
    sid = guard(store.add_source, body.name, body.config, user["username"])
    store.clear_draft()                                                  # connected: the form starts empty again
    estate_store.audit(user["username"], "ADF_CONNECTED", body.name or body.config.get("factory_name", ""),
                       f"{body.config.get('resource_group')}/{body.config.get('factory_name')}")
    _sync_now(sid, user["username"])
    return {"source_id": sid}


@router.put("/sources/{source_id}")
def update_source(source_id: int, body: SourceBody, user: dict = Depends(admin_user)):
    guard(store.get_source, source_id)
    guard(store.update_source, source_id, body.name, body.config, body.enabled)
    estate_store.audit(user["username"], "ADF_UPDATED", str(source_id), body.name)
    return {"ok": True}


@router.delete("/sources/{source_id}")
def delete_source(source_id: int, user: dict = Depends(admin_user)):
    src = guard(store.get_source, source_id)
    store.delete_source(source_id)
    estate_store.audit(user["username"], "ADF_REMOVED", src["name"], "connection and its collected history removed")
    return {"ok": True}


def _sync_now(source_id: int, by: str):
    threading.Thread(target=lambda: collector.sync(source_id, f"manual:{by}", force_definitions=True), daemon=True).start()


@router.post("/sources/{source_id}/sync")
def sync_now(source_id: int, user: dict = Depends(current_user)):
    src = guard(store.get_source, source_id)
    if not src["enabled"]:
        raise HTTPException(400, "this connection is paused")
    _sync_now(source_id, user["username"])
    return {"ok": True}


@router.get("/sources/{source_id}/sync-log")
def sync_log(source_id: int, user: dict = Depends(current_user)):
    return insights.sync_log(source_id)


@router.post("/sources/{source_id}/webhook-token")
def webhook_token(source_id: int, request: Request, user: dict = Depends(admin_user)):
    guard(store.get_source, source_id)
    token = store.new_webhook_token(source_id)
    estate_store.audit(user["username"], "ADF_WEBHOOK_TOKEN", str(source_id), "new token issued (old one stops working)")
    import os
    base = (os.getenv("DATATRUST_PUBLIC_URL") or str(request.base_url)).rstrip("/")
    return {"token": token, "url": f"{base}/api/adf/hooks/{source_id}"}


@router.get("/overview")
def overview(source_id: Optional[int] = None, user: dict = Depends(current_user)):
    return insights.overview(source_id)


@router.get("/issues")
def issues(source_id: Optional[int] = None, user: dict = Depends(current_user)):
    return insights.issues(source_id)


@router.get("/runs")
def runs(source_id: Optional[int] = None, pipeline: Optional[str] = None, status: Optional[str] = None, days: int = 30,
         q: Optional[str] = None, user: dict = Depends(current_user)):
    return insights.runs(source_id, pipeline or None, status or None, min(max(days, 1), 45), 300, q or None)


@router.get("/runs/{run_id}")
def run_detail(run_id: str, user: dict = Depends(current_user)):
    return guard(insights.run_detail, run_id)


@router.get("/pipelines/{source_id}/{name}")
def pipeline_detail(source_id: int, name: str, user: dict = Depends(current_user)):
    return guard(insights.pipeline_detail, source_id, name)


@router.get("/lineage/{source_id}")
def lineage(source_id: int, user: dict = Depends(current_user)):
    return insights.lineage(source_id)


@router.get("/version")
def version(user: dict = Depends(current_user)):
    return {"signature": insights.changed_signature()}


# ---------------------------------------------------------------- called by ADF
@hooks.post("/hooks/{source_id}", status_code=202)
async def adf_hook(source_id: int, request: Request, x_datatrust_token: str = Header(default="")):
    token = x_datatrust_token or request.query_params.get("token", "")
    if not store.check_webhook_token(source_id, token):
        raise HTTPException(401, "invalid token")
    store.request_sync(source_id)                     # the collector loop picks it up within 15 s
    return {"ok": True, "message": "sync requested"}
