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
            "REJECTED": "rejected", "FAILED": "failed", "STALE": "closed"}
DECISION_LABEL = {"pending": "Waiting", "approved": "Approved", "rejected": "Rejected", "failed": "Failed", "closed": "Closed"}
# what happened after the decision, in plain words: the stored status means something different per kind
OUTCOME = {("access", "APPLIED"): "done in the database", ("access", "FAILED"): "the database refused it",
           ("reinsert", "APPLIED"): "rows loaded", ("reinsert", "QUEUED"): "rows load on the next pipeline run",
           ("reinsert", "CLOSED"): "nothing left to do", ("reinsert", "FAILED"): "the rows could not be loaded",
           ("remediation", "APPLIED"): "the data was fixed", ("remediation", "APPROVED"): "plan approved; people do the steps",
           ("remediation", "REJECTED"): "not applied", ("remediation", "FAILED"): "could not be applied",
           ("remediation", "STALE"): "the rows changed before approval; request it again"}
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
    state = appdb.table_exists("app.incident_state")
    rows = appdb.rows(f"""SELECT l.remediation_id, l.incident_id, l.incident_title, l.actions, l.approved_by, l.note, l.status,
                                 l.approved_at, {"s.incident_id AS tracked, s.resolved_at, s.resolution" if state else "NULL AS tracked, NULL AS resolved_at, NULL AS resolution"}
                          FROM app.remediation_log l
                          {"LEFT JOIN app.incident_state s ON s.incident_id = l.incident_id" if state else ""}
                          ORDER BY l.remediation_id DESC LIMIT %s""", (LIMIT,))
    # requested fixes (fix_requests.py): asked by one person, decided by an admin. Their applied fix also wrote a
    # remediation_log line; that line is left out here so each fix is listed once.
    import fix_requests
    fixes = fix_requests.history_rows(LIMIT)
    # one fix can resolve several incidents (the rejected rows and that date's revenue gap): each wrote a log line
    applied = {(iid, (f["decided_by"] or "").lower()) for f in fixes if f["status"] == "APPLIED"
               for iid in [f["incident_id"], *((f.get("proposal") or {}).get("resolves") or [])]}
    states = {s["incident_id"]: s for s in appdb.rows(
        "SELECT incident_id, resolved_at, resolution FROM app.incident_state WHERE incident_id = ANY(%s)",
        ([f["incident_id"] for f in fixes],))} if fixes and state else {}
    out = []
    for f in fixes:
        st = states.get(f["incident_id"])
        res = f.get("result") or {}
        out.append({"kind": "remediation", "ref": f["incident_id"], "from_request": True,
                    "what": f"Fix {f['rows']} {f['entity']} row(s) of {f['business_date']}: {f['title'] or f['incident_id']}",
                    "detail": " · ".join(x for x in (f.get("note"), f.get("decision_note"), res.get("error")) if x),
                    "status": f["status"], "requested_by": f["requested_by"], "requested_at": f["requested_at"],
                    "decided_by": f["decided_by"], "decided_at": f["decided_at"],
                    "link": f"impact={f['incident_id']}" if f["status"] == "PENDING" else "verify",
                    "result": _result(st and st["incident_id"], st and st["resolved_at"], st and st["resolution"])
                    if f["status"] == "APPLIED" else None})
    # an incident fix approved before requests existed (or a remediation plan): the admin who approved it also asked
    out += [{"kind": "remediation", "ref": r["incident_id"], "what": r["incident_title"] or r["incident_id"],
             "detail": " · ".join(x for x in (r["actions"], r["note"]) if x),
             "status": r["status"] or "APPROVED", "requested_by": r["approved_by"], "requested_at": _ts(r["approved_at"]),
             "decided_by": r["approved_by"], "decided_at": _ts(r["approved_at"]), "link": "verify",
             "result": _result(r["tracked"], r["resolved_at"], r["resolution"])} for r in rows
            if not (r["status"] == "APPLIED" and (r["incident_id"], (r["approved_by"] or "").lower()) in applied)]
    return out


def _result(tracked, resolved_at, reason) -> dict:
    """What became of the incident after the fix was approved: still open, or resolved and why (web/resolution.py)."""
    if not tracked:                                  # approved before DataTrust kept incident life cycles
        return {"state": "unknown", "label": "Not tracked"}
    if not resolved_at:
        return {"state": "open", "label": "Still open"}
    if reason == "superseded":                       # the same failure continues in a newer run, under a new id
        return {"state": "open", "label": "Still failing in a newer run"}
    import resolution
    name, ok = resolution.label(reason)
    return {"state": "verified" if ok else "unverified", "label": name, "at": _ts(resolved_at)}


def approvals(kind: str = None, decision: str = None, person: str = None, date_from: str = None, date_to: str = None,
              q: str = None) -> dict:
    """The decision log: every decision of every kind, newest first, in one shape.

    kind      access / reinsert / remediation, or several "access,reinsert" (the page's tabs); empty = all
    decision  pending / approved / rejected / failed / closed
    person    matches who asked or who decided;  dates compare with when it was decided (or asked, while pending)

    Returns the filtered rows, plus what the page shows around them, all read in one pass:
      totals    summary tiles: everything on record, whatever the filters
      by_kind   the tab counts: the other filters applied, but not the kind (so each tab says what it would show)
      waiting   every request still waiting for a decision, whatever the filters"""
    wanted = [k.strip() for k in (kind or "").split(",") if k.strip()]
    if any(k not in KINDS for k in wanted):
        raise ValueError(f"kind must be one of {', '.join(KINDS)}")
    rows, errors = [], []
    for k, fn in (("access", _access), ("reinsert", _reinsert), ("remediation", _remediation)):
        try:
            rows += fn()
        except Exception as e:                                           # noqa: BLE001  one source never hides the others
            errors.append(f"{KINDS[k]}: {type(e).__name__}: {str(e).splitlines()[0][:200]}")
    for r in rows:
        r["kind_label"] = KINDS[r["kind"]]
        r["decision"] = DECISION.get(r["status"], (r["status"] or "").lower())
        r["decision_label"] = DECISION_LABEL.get(r["decision"], (r["status"] or "").capitalize())
        r["outcome"] = OUTCOME.get((r["kind"], r["status"]), "")
        r["result_label"] = (r.get("result") or {}).get("label", "")     # incident fixes: what became of the incident
        r["at"] = r["decided_at"] or r["requested_at"]
        # four-eyes check: the same person asked and decided. Incident fixes count only when they were requested
        # (fix_requests.py); an older direct approval had no separate request.
        asked, decided = (r["requested_by"] or "").strip().lower(), (r["decided_by"] or "").strip().lower()
        r["self_approved"] = (r["kind"] != "remediation" or bool(r.get("from_request"))) and r["decision"] == "approved"             and bool(asked) and asked == decided
    # totals for the summary tiles: everything read, before the filters below narrow the list
    totals = {d: sum(r["decision"] == d for r in rows) for d in ("pending", "approved", "rejected", "failed")}
    totals["self_approved"] = sum(r["self_approved"] for r in rows)
    totals["pending_by_kind"] = {k: sum(r["decision"] == "pending" and r["kind"] == k for r in rows) for k in KINDS}
    waiting = sorted((r for r in rows if r["decision"] == "pending"), key=lambda r: r["requested_at"] or "")   # oldest first
    p, needle = (person or "").strip().lower(), (q or "").strip().lower()
    rows = [r for r in rows
            if (not decision or r["decision"] == decision)
            and (not p or p in (r["requested_by"] or "").lower() or p in (r["decided_by"] or "").lower())
            and (not date_from or (r["at"] or "")[:10] >= date_from)
            and (not date_to or (r["at"] or "")[:10] <= date_to)
            and (not needle or needle in " ".join(str(r[k] or "") for k in ("ref", "what", "detail")).lower())]
    by_kind = {k: sum(r["kind"] == k for r in rows) for k in KINDS}
    rows = [r for r in rows if not wanted or r["kind"] in wanted]
    rows.sort(key=lambda r: r["at"] or "", reverse=True)
    counts = {d: sum(r["decision"] == d for r in rows) for d in ("pending", "approved", "rejected", "failed")}
    return {"rows": rows, "counts": counts, "totals": totals, "by_kind": by_kind, "waiting": waiting[:50], "errors": errors}


def to_csv(rows: list, columns: list) -> str:
    """columns: (field, header) pairs, so the file has headers people can read."""
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow([label for _, label in columns])
    for r in rows:
        w.writerow(["" if r.get(c) is None else ("yes" if r.get(c) is True else "" if r.get(c) is False else r.get(c))
                    for c, _ in columns])
    return buf.getvalue()
