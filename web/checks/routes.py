"""/api/estate/checks: list, history, add / edit / delete (admin), preview, run now, accept a schema change."""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from estate import store as estate_store
from estate.routes import admin_user, current_user

from . import engine, runner, store

router = APIRouter(prefix="/api/estate/checks")


class CheckBody(BaseModel):
    name: str = ""
    check_type: str
    connection_id: Optional[int] = None
    database: Optional[str] = None
    schema_name: Optional[str] = None
    table_name: Optional[str] = None
    config: dict = {}
    severity: str = "MEDIUM"
    schedule_minutes: int = 60
    enabled: bool = True
    description: Optional[str] = None


def guard(fn, *a, **kw):
    try:
        return fn(*a, **kw)
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.get("")
def list_checks(connection_id: Optional[int] = None, user: dict = Depends(current_user)):
    return {"checks": store.list_checks(connection_id), "summary": store.summary(),
            "scheduler": {"running": runner.state["running"], "every_seconds": runner.POLL_SECONDS, "last_error": runner.state["last_error"]}}


@router.get("/columns")
def columns(connection_id: int, schema: str, table: str, database: Optional[str] = None, user: dict = Depends(current_user)):
    return guard(engine.columns, connection_id, database or None, schema, table)


class SuggestBody(BaseModel):
    connection_id: int
    database: Optional[str] = None
    schema_name: str
    table_name: str


@router.post("/suggest")
def suggest(body: SuggestBody, user: dict = Depends(admin_user)):
    """Checks that fit one table, from its measured profile (AI-written when a model is configured). Nothing is saved."""
    import ai
    return guard(ai.suggest_checks, body.connection_id, body.database or None, body.schema_name, body.table_name)


@router.get("/{check_id}")
def detail(check_id: int, user: dict = Depends(current_user)):
    return {"check": guard(store.get_check, check_id), "results": store.history(check_id)}


@router.post("/preview")
def preview(body: CheckBody, user: dict = Depends(admin_user)):
    """Run an unsaved definition once and show what it would report. Nothing is stored."""
    chk = {**guard(store.validate, body.model_dump()), "check_id": None, "state": {}}
    r = engine.run(chk)
    r.pop("new_state", None)
    return r


@router.post("")
def add(body: CheckBody, user: dict = Depends(admin_user)):
    cid = guard(store.add_check, body.model_dump(), user["username"])
    estate_store.audit(user["username"], "CHECK_ADDED", body.name, body.check_type)
    return {"check_id": cid, "result": engine.run_and_save(cid, user["username"])}


@router.put("/{check_id}")
def update(check_id: int, body: CheckBody, user: dict = Depends(admin_user)):
    guard(store.update_check, check_id, body.model_dump())
    estate_store.audit(user["username"], "CHECK_UPDATED", body.name, body.check_type)
    return {"ok": True, "result": engine.run_and_save(check_id, user["username"])}


@router.delete("/{check_id}")
def delete(check_id: int, user: dict = Depends(admin_user)):
    chk = guard(store.get_check, check_id)
    store.delete_check(check_id)
    estate_store.audit(user["username"], "CHECK_REMOVED", chk["name"], chk["check_type"])
    return {"ok": True}


@router.post("/{check_id}/run")
def run_now(check_id: int, user: dict = Depends(current_user)):
    guard(store.get_check, check_id)
    return engine.run_and_save(check_id, user["username"])


@router.post("/{check_id}/accept-schema")
def accept_schema(check_id: int, user: dict = Depends(admin_user)):
    """The current columns become the accepted schema (after an intended change)."""
    chk = guard(store.get_check, check_id)
    if chk["check_type"] != "schema":
        raise HTTPException(400, "only schema checks have an accepted schema")
    cols = guard(engine.columns, chk["connection_id"], chk["database"], chk["schema_name"], chk["table_name"])
    store.set_state(check_id, {"columns": {c["name"]: c["type"] for c in cols}})
    estate_store.audit(user["username"], "SCHEMA_ACCEPTED", f"{chk['schema_name']}.{chk['table_name']}", f"{len(cols)} columns")
    return engine.run_and_save(check_id, user["username"])
