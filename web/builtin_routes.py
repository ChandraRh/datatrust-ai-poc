"""/api/builtin/settings: read the built-in check settings of the selected source; change or reset one (admin)."""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from builtin import overview, reset, save, setting
from estate.routes import admin_user as _admin, current_user as _current

router = APIRouter(prefix="/api/builtin")




def _guard(fn, *a, **kw):
    try:
        return fn(*a, **kw)
    except ValueError as e:
        raise HTTPException(400, str(e))


class SettingBody(BaseModel):
    key: str
    enabled: Optional[bool] = None
    severity: Optional[str] = None
    params: dict = {}
    source_id: Optional[int] = None
    pipeline: Optional[str] = None


@router.get("/settings")
def get_settings(scope: Optional[str] = None, user: dict = Depends(_current)):
    return _guard(overview, scope)


@router.put("/settings")
def put_setting(body: SettingBody, user: dict = Depends(_admin)):
    v = _guard(save, body.key, body.enabled, body.severity, body.params, user["username"], body.source_id, body.pipeline)
    from estate import store as estate_store
    estate_store.audit(user["username"], "BUILTIN_CHECK_CHANGED", body.key + (f" ({body.pipeline})" if body.pipeline else ""),
                       f"enabled={v['enabled']} severity={v['severity']} " + " ".join(f"{k}={body.params[k]}" for k in body.params))
    return {"value": v}


@router.delete("/settings")
def delete_setting(key: str, source_id: Optional[int] = None, pipeline: Optional[str] = None, user: dict = Depends(_admin)):
    _guard(reset, key, source_id, pipeline)
    from estate import store as estate_store
    estate_store.audit(user["username"], "BUILTIN_CHECK_RESET", key + (f" ({pipeline})" if pipeline else ""), "back to default")
    return {"value": setting(key, source_id, pipeline)}
