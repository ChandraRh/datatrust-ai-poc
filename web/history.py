"""
Audit & approvals (admin page): every decision made in DataTrust, in one place.

    approvals()   access requests (portal.access_requests), re-insert requests for rejected rows (audit.dq_reprocess, in the
                  monitored POC database) and approved fixes for incidents (app.remediation_log), as one list, newest first

Read only. Each source is read on its own, so one missing table (e.g. DQ not set up) never hides the others.
"""
from __future__ import annotations

import csv
import io
import json
from datetime import date, datetime

KINDS = {"access": "Access request", "reinsert": "Re-insert rejected rows", "remediation": "Incident fix"}
ACCESS_LABEL = {"GRANT": "Grant", "REVOKE": "Revoke", "CREATE_USER": "Add team member", "CREATE_ROLE": "Create role",
                "GRANT_ROLE": "Add to role", "REVOKE_ROLE": "Remove from role", "DROP_PRINCIPAL": "Remove user / role"}
# what a status means on this page: decided (approved / rejected / failed) or still waiting
DECISION = {"PENDING": "pending", "APPLIED": "approved", "APPROVED": "approved", "QUEUED": "approved", "CLOSED": "closed",
            "REJECTED": "rejected", "FAILED": "failed"}
LIMIT = 2000                                    # rows read per source; the page filters further


def _ts(v) -> str | None:
    return v.isoformat(sep=" ", timespec="seconds") if isinstance(v, (datetime, date)) else (str(v) if v else None)


def _access() -> list:
    from estate import store
    with store.db() as conn:
        rows = conn.execute("""SELECT r.request_id, r.request_type, r.grantee, r.database, r.reason, r.status, r.error,
                                      r.requested_by, r.requested_at, r.decided_by, r.decided_at, c.name AS connection
                               FROM portal.access_requests r LEFT JOIN portal.connections c USING (connection_id)
                               ORDER BY r.request_id DESC LIMIT %s""", (LIMIT,)).fetchall()
    out = []
    for r in rows:
        where = " · ".join(x for x in (r["connection"], r["database"]) if x)
        out.append({"kind": "access", "ref": f"#{r['request_id']}",
                    "what": f"{ACCESS_LABEL.get(r['request_type'], r['request_type'])}: {r['grantee']}" + (f" ({where})" if where else ""),
                    "detail": " · ".join(x for x in (r["reason"], r["error"]) if x),
                    "status": r["status"], "requested_by": r["requested_by"], "requested_at": _ts(r["requested_at"]),
                    "decided_by": r["decided_by"], "decided_at": _ts(r["decided_at"]), "link": "estate-access"})
    return out


def _reinsert() -> list:
    import data_service as ds
    if not ds.table_exists("audit.dq_reprocess"):
        return []
    rows = ds.rows("""SELECT request_id, entity, items, changes, note, status, result, requested_by, requested_at, decided_by, decided_at
                      FROM audit.dq_reprocess ORDER BY request_id DESC LIMIT %(n)s""", {"n": LIMIT})
    out = []
    for r in rows:
        items = r["items"] if isinstance(r["items"], list) else json.loads(r["items"] or "[]")
        changes = r["changes"] if isinstance(r["changes"], dict) else json.loads(r["changes"] or "{}")
        change = ", ".join(f"{k} → {v}" for k, v in changes.items())
        out.append({"kind": "reinsert", "ref": f"#{r['request_id']}",
                    "what": f"Re-insert {len(items)} {r['entity']} record(s)" + (f" with {change}" if change else ""),
                    "detail": " · ".join(x for x in (r["note"], r["result"]) if x),
                    "status": r["status"], "requested_by": r["requested_by"], "requested_at": _ts(r["requested_at"]),
                    "decided_by": r["decided_by"], "decided_at": _ts(r["decided_at"]), "link": "estate-dq-rejected"})
    return out


def _remediation() -> list:
    import appdb
    if not appdb.table_exists("app.remediation_log"):
        return []
    rows = appdb.rows("""SELECT remediation_id, incident_id, incident_title, actions, approved_by, note, status, approved_at
                         FROM app.remediation_log ORDER BY remediation_id DESC LIMIT %s""", (LIMIT,))
    # an approved fix has no separate request: the admin who approved it is also who asked for it
    return [{"kind": "remediation", "ref": r["incident_id"], "what": r["incident_title"] or r["incident_id"],
             "detail": " · ".join(x for x in (r["actions"], r["note"]) if x),
             "status": r["status"] or "APPROVED", "requested_by": r["approved_by"], "requested_at": _ts(r["approved_at"]),
             "decided_by": r["approved_by"], "decided_at": _ts(r["approved_at"]), "link": "verify"} for r in rows]


def approvals(kind: str = None, decision: str = None, person: str = None, date_from: str = None, date_to: str = None,
              q: str = None) -> dict:
    """All decisions, newest first. decision: pending / approved / rejected / failed / closed. person matches who asked
    or who decided. Dates compare with when it was decided (or asked, while pending)."""
    wanted = [k.strip() for k in (kind or "").split(",") if k.strip()]          # one kind, or several: "access,reinsert"
    if any(k not in KINDS for k in wanted):
        raise ValueError(f"kind must be one of {', '.join(KINDS)}")
    rows, errors = [], []
    for k, fn in (("access", _access), ("reinsert", _reinsert), ("remediation", _remediation)):
        if wanted and k not in wanted:
            continue
        try:
            rows += fn()
        except Exception as e:                                           # noqa: BLE001  one source never hides the others
            errors.append(f"{KINDS[k]}: {type(e).__name__}: {str(e).splitlines()[0][:200]}")
    for r in rows:
        r["kind_label"] = KINDS[r["kind"]]
        r["decision"] = DECISION.get(r["status"], (r["status"] or "").lower())
        r["at"] = r["decided_at"] or r["requested_at"]
    p, needle = (person or "").strip().lower(), (q or "").strip().lower()
    rows = [r for r in rows
            if (not decision or r["decision"] == decision)
            and (not p or p in (r["requested_by"] or "").lower() or p in (r["decided_by"] or "").lower())
            and (not date_from or (r["at"] or "")[:10] >= date_from)
            and (not date_to or (r["at"] or "")[:10] <= date_to)
            and (not needle or needle in " ".join(str(r[k] or "") for k in ("ref", "what", "detail")).lower())]
    rows.sort(key=lambda r: r["at"] or "", reverse=True)
    counts = {d: sum(r["decision"] == d for r in rows) for d in ("pending", "approved", "rejected", "failed")}
    return {"rows": rows, "counts": counts, "errors": errors}


def to_csv(rows: list, columns: list) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(columns)
    for r in rows:
        w.writerow(["" if r.get(c) is None else r.get(c) for c in columns])
    return buf.getvalue()
