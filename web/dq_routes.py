"""
/api/estate/dq/* — DQ Rule Studio + Rejected records. Portal login needed (raw records can hold personal data);
changing rules and approving re-inserts need the admin role. Anyone logged in can test, draft and request.
"""
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel

import dq
from estate.routes import admin_user, current_user, guard

router = APIRouter(prefix="/api/estate/dq")


class Rule(BaseModel):
    rule_id: Optional[int] = None
    entity: str
    rule_name: str = ""
    rule_type: str
    column_name: str
    params: dict = {}
    action: str = "REJECT"
    description: str = ""
    active: bool = True


class Draft(BaseModel):
    entity: str
    text: str


class Items(BaseModel):
    entity: str
    items: list
    changes: dict = {}
    note: str = ""


def _filters(date_from, date_to, entity, rule, q, status, limit):
    return {"date_from": date_from, "date_to": date_to, "entity": entity, "rule": rule, "q": q, "status": status, "limit": limit}


# ---------------------------------------------------------------- rules
@router.get("/rules")
def rules(user: dict = Depends(current_user)):
    return guard(dq.rules_page)


@router.post("/rules")
def save_rule(body: Rule, user: dict = Depends(admin_user)):
    return guard(dq.save_rule, body.model_dump(), user["username"])


@router.post("/rules/{rule_id}/{state}")
def toggle_rule(rule_id: int, state: str, user: dict = Depends(admin_user)):
    if state not in ("on", "off"):
        raise HTTPException(400, "use on or off")
    return guard(dq.toggle_rule, rule_id, state == "on", user["username"])


@router.delete("/rules/{rule_id}")
def delete_rule(rule_id: int, user: dict = Depends(admin_user)):
    return guard(dq.delete_rule, rule_id, user["username"])


@router.post("/rules-test")
def test_rule(body: Rule, user: dict = Depends(current_user)):
    return guard(dq.test_rule, body.model_dump())


@router.post("/rules-draft")
def draft_rule(body: Draft, user: dict = Depends(current_user)):
    return guard(dq.draft_rule, body.entity, body.text)


@router.get("/rules-suggest")
def suggest(entity: str, user: dict = Depends(current_user)):
    return guard(dq.suggest, entity)


# ---------------------------------------------------------------- rejected records
@router.get("/rejected")
def rejected(date_from: Optional[str] = None, date_to: Optional[str] = None, entity: Optional[str] = None,
             rule: Optional[str] = None, q: Optional[str] = None, status: Optional[str] = None, limit: int = 200,
             user: dict = Depends(current_user)):
    return guard(dq.rejected, _filters(date_from, date_to, entity, rule, q, status, limit))


@router.get("/rejected-summary")
def rejected_summary(date_from: Optional[str] = None, date_to: Optional[str] = None, user: dict = Depends(current_user)):
    return guard(dq.summary, {"date_from": date_from, "date_to": date_to})


@router.get("/rejected.csv")
def rejected_csv(date_from: Optional[str] = None, date_to: Optional[str] = None, entity: Optional[str] = None,
                 rule: Optional[str] = None, q: Optional[str] = None, status: Optional[str] = None,
                 user: dict = Depends(current_user)):
    text = guard(dq.export_csv, _filters(date_from, date_to, entity, rule, q, status, 2000))
    name = f"rejected_{entity or 'all'}_{date_from or 'all'}_{date_to or 'all'}.csv"
    return Response(text, media_type="text/csv", headers={"Content-Disposition": f'attachment; filename="{name}"'})


@router.get("/record")
def record(entity: str, key: str, business_date: Optional[str] = None, user: dict = Depends(current_user)):
    return guard(dq.record, entity, key, business_date)


@router.get("/recheck")
def recheck(entity: str, user: dict = Depends(current_user)):
    return guard(dq.recheck, entity)


@router.post("/reprocess/preview")
def reprocess_preview(body: Items, user: dict = Depends(current_user)):
    return guard(dq.preview, body.entity, body.items, body.changes)


@router.post("/reprocess")
def reprocess_request(body: Items, user: dict = Depends(current_user)):
    return guard(dq.request_reprocess, body.entity, body.items, body.changes, body.note, user["username"])


@router.get("/reprocess")
def reprocess_list(status: Optional[str] = None, user: dict = Depends(current_user)):
    return guard(dq.requests_list, status)


@router.post("/reprocess/{request_id}/{decision}")
def reprocess_decide(request_id: int, decision: str, user: dict = Depends(admin_user)):
    return guard(dq.decide, request_id, decision, user["username"])


@router.post("/review")
def review(body: Items, user: dict = Depends(admin_user)):
    return guard(dq.review, body.entity, body.items, body.note, user["username"])
