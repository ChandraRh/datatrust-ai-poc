"""
/api/estate/history/* — Audit & approvals page (admins only): every approval decision, with filters and CSV export.
Read only.
"""
from __future__ import annotations

from datetime import date
from typing import Optional

from fastapi import APIRouter, Depends
from fastapi.responses import Response

import history
from estate.routes import admin_user, guard

router = APIRouter(prefix="/api/estate/history")

APPROVAL_COLUMNS = [("kind_label", "Type"), ("ref", "Reference"), ("what", "What"), ("decision_label", "Decision"),
                    ("outcome", "Outcome"), ("status", "Status code"), ("requested_by", "Asked by"), ("requested_at", "Asked at"),
                    ("decided_by", "Decided by"), ("decided_at", "Decided at"), ("self_approved", "Self-approved"), ("result_label", "Incident now"),
                    ("detail", "Details")]


def _csv(text: str, name: str) -> Response:
    return Response(text, media_type="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="datatrust-{name}-{date.today()}.csv"'})


@router.get("/approvals")
def approvals(kind: Optional[str] = None, decision: Optional[str] = None, person: Optional[str] = None,
              date_from: Optional[str] = None, date_to: Optional[str] = None, q: Optional[str] = None,
              user: dict = Depends(admin_user)):
    return guard(history.approvals, kind, decision, person, date_from, date_to, q)


@router.get("/approvals.csv")
def approvals_csv(kind: Optional[str] = None, decision: Optional[str] = None, person: Optional[str] = None,
                  date_from: Optional[str] = None, date_to: Optional[str] = None, q: Optional[str] = None,
                  user: dict = Depends(admin_user)):
    rows = guard(history.approvals, kind, decision, person, date_from, date_to, q)["rows"]
    return _csv(history.to_csv(rows, APPROVAL_COLUMNS), "approvals")
