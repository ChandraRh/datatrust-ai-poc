"""
Incident fixes with a real approval: someone REQUESTS a fix, an admin APPROVES (or rejects) it, and only then does
anything change. Before this, the admin who pressed "Approve & apply" was both asker and approver.

    request(incident, by, note)    saves the exact proposal (autofix.proposal: rows, old -> new values, evidence) as
                                   PENDING; nothing changes in the data. One pending request per incident.
    decide(id, by, approve, note)  approve: four-eyes check (the requester may not approve, unless the built-in setting
                                   'Four-eyes approval for fixes' is off), then the proposal is worked out again and must
                                   be exactly the one requested (else STALE: the rows changed, request it again), then
                                   autofix.apply runs it in one transaction -> APPLIED (or FAILED with the error)
                                   reject: a reason is required -> REJECTED
    latest(incident)               the newest request of an incident, for the Impact page

Every step is in the audit log (FIX_REQUESTED / FIX_APPROVED / FIX_REJECTED); Audit & approvals lists the requests.
"""
from __future__ import annotations

import hashlib
import json

from psycopg.types.json import Jsonb

import appdb

DDL = """
CREATE SCHEMA IF NOT EXISTS app;
CREATE TABLE IF NOT EXISTS app.fix_requests (
    request_id    BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    incident_id   TEXT NOT NULL,
    title         TEXT,
    entity        TEXT,
    business_date TEXT,
    rows          INTEGER NOT NULL,
    proposal      JSONB NOT NULL,          -- what was asked for: every row with its old and new value and the evidence
    fingerprint   TEXT NOT NULL,           -- the proposal in one hash: approval must find exactly the same
    note          TEXT,
    status        TEXT NOT NULL DEFAULT 'PENDING',     -- PENDING / APPLIED / REJECTED / STALE / FAILED
    requested_by  TEXT NOT NULL,
    requested_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    decided_by    TEXT,
    decided_at    TIMESTAMPTZ,
    decision_note TEXT,
    result        JSONB
);
CREATE INDEX IF NOT EXISTS fix_requests_by_incident ON app.fix_requests (incident_id, request_id DESC);
CREATE UNIQUE INDEX IF NOT EXISTS fix_requests_one_pending ON app.fix_requests (incident_id) WHERE status = 'PENDING';
"""


def ensure_table() -> None:
    with appdb.connect() as conn:
        conn.execute(DDL)
        conn.commit()


def fingerprint(p: dict) -> str:
    rows = sorted((str(r.get("record_key")), str(r.get("column")), str(r.get("current")), str(r.get("proposed"))) for r in p["rows"])
    return hashlib.sha256(json.dumps([p.get("entity"), p.get("business_date"), rows]).encode()).hexdigest()


def four_eyes() -> bool:
    """On (default): the person who asked for a fix cannot approve it. Built-in checks -> 'Four-eyes approval for fixes'."""
    import builtin
    try:
        return bool(builtin.setting("all.four_eyes")["enabled"])
    except Exception:                                            # noqa: BLE001  no setting readable: the strict default
        return True


def _audit(by, action, target, detail):
    try:
        from estate import store
        store.audit(by, action, target, detail)
    except Exception:                                            # noqa: BLE001  the request itself is recorded either way
        pass


def _row(r: dict | None) -> dict | None:
    if not r:
        return None
    out = dict(r)
    for k in ("requested_at", "decided_at"):
        if out.get(k) is not None and not isinstance(out[k], str):
            out[k] = out[k].isoformat(sep=" ", timespec="seconds")
    return out


def get(request_id: int) -> dict:
    r = appdb.one("SELECT * FROM app.fix_requests WHERE request_id = %s", (request_id,))
    if not r:
        raise ValueError(f"fix request {request_id} not found")
    return _row(r)


def latest(incident_id: str) -> dict | None:
    if not appdb.table_exists("app.fix_requests"):
        return None
    return _row(appdb.one("SELECT * FROM app.fix_requests WHERE incident_id = %s ORDER BY request_id DESC LIMIT 1", (incident_id,)) or None)


def request(incident_id: str, by: str, note: str = "") -> dict:
    """Save the current proposal as a pending request. Nothing in the data changes."""
    import autofix
    pending = appdb.one("SELECT * FROM app.fix_requests WHERE incident_id = %s AND status = 'PENDING'", (incident_id,))
    if pending:
        return {**_row(pending), "already": True}
    p = autofix.proposal(incident_id)
    if not p.get("available") or not p.get("rows"):
        raise ValueError(p.get("reason") or "There is no automatic fix for this incident.")
    with appdb.connect() as conn:
        r = conn.execute("""INSERT INTO app.fix_requests (incident_id, title, entity, business_date, rows, proposal, fingerprint,
                                                          note, requested_by)
                            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING *""",
                         (incident_id, p.get("title"), p.get("entity"), str(p.get("business_date") or ""), len(p["rows"]),
                          Jsonb(json.loads(json.dumps(p, default=str))), fingerprint(p), (note or "").strip()[:500] or None,
                          by)).fetchone()
        conn.commit()
    _audit(by, "FIX_REQUESTED", incident_id, f"{len(p['rows'])} {p.get('entity')} row(s) of {p.get('business_date')}")
    _notify(r)
    return _row(r)


def _finish(request_id: int, status: str, by: str, note: str | None, result: dict | None = None) -> dict:
    with appdb.connect() as conn:
        r = conn.execute("""UPDATE app.fix_requests SET status = %s, decided_by = %s, decided_at = now(), decision_note = %s,
                                   result = %s
                            WHERE request_id = %s AND status = 'PENDING' RETURNING *""",
                         (status, by, note, Jsonb(result) if result is not None else None, request_id)).fetchone()
        conn.commit()
    if not r:
        raise ValueError("This request was decided by someone else in the meantime.")
    return _row(r)


def can_decide(req: dict, user: str, role: str) -> tuple[bool, str]:
    """(may this user approve it, why not)."""
    if role != "admin":
        return False, "An admin approves fixes."
    if req.get("status") != "PENDING":
        return False, "This request is already decided."
    if four_eyes() and (req.get("requested_by") or "").strip().lower() == (user or "").strip().lower():
        return False, ("You asked for this fix, so another admin must approve it (four-eyes). An admin can allow "
                       "self-approval under Data quality -> Built-in checks -> Four-eyes approval for fixes.")
    return True, ""


def decide(request_id: int, by: str, role: str, approve: bool, note: str = "") -> dict:
    req = get(request_id)
    note = (note or "").strip()[:500] or None
    if not approve:
        if req["status"] != "PENDING":
            raise ValueError("This request is already decided.")
        if role != "admin":
            raise ValueError("An admin rejects fixes.")
        if not note:
            raise ValueError("Say why the fix is rejected, so the requester knows what to do next.")
        out = _finish(request_id, "REJECTED", by, note)
        _audit(by, "FIX_REJECTED", req["incident_id"], note)
        return out
    ok, why = can_decide(req, by, role)
    if not ok:
        raise ValueError(why)
    import autofix
    now = autofix.proposal(req["incident_id"])                   # the data may have changed since the request
    if not now.get("available") or not now.get("rows") or fingerprint(now) != req["fingerprint"]:
        out = _finish(request_id, "STALE", by, "The rows changed after the request (e.g. a pipeline run); nothing was applied.")
        _audit(by, "FIX_STALE", req["incident_id"], "proposal changed since the request")
        raise StaleRequest(out)
    try:
        result = autofix.apply(req["incident_id"], by)
    except Exception as e:                                       # noqa: BLE001  record the failure, then report it
        _finish(request_id, "FAILED", by, note, {"error": f"{type(e).__name__}: {e}"[:500]})
        _audit(by, "FIX_FAILED", req["incident_id"], str(e)[:200])
        raise ValueError(f"The fix could not be applied: {e}") from e
    out = _finish(request_id, "APPLIED", by, note, json.loads(json.dumps(result, default=str)))
    _audit(by, "FIX_APPROVED", req["incident_id"], f"{result.get('fixed')} row(s) corrected; requested by {req['requested_by']}")
    return out


class StaleRequest(ValueError):
    def __init__(self, row: dict):
        super().__init__("The rows changed after this fix was requested (for example a pipeline run), so it was not "
                         "applied. Look at the new proposal and request it again.")
        self.row = row


def history_rows(limit: int = 2000) -> list:
    """For Audit & approvals: every request, newest first."""
    if not appdb.table_exists("app.fix_requests"):
        return []
    return [_row(r) for r in appdb.rows("SELECT * FROM app.fix_requests ORDER BY request_id DESC LIMIT %s", (limit,))]


def _notify(r: dict) -> None:
    """A 'fix waiting for approval' message in Google Chat, where alerts are on. Never fails the request."""
    try:
        import alerts
        if not alerts.webhook():
            return
        alerts.send({"text": f"*Fix waiting for approval* · {r['title'] or r['incident_id']}\n"
                             f"{r['rows']} {r['entity']} row(s) of {r['business_date']}, requested by {r['requested_by']}"
                             + (f": {r['note']}" if r.get("note") else "") +
                             "\nAn admin approves or rejects it on Impact & decide."})
    except Exception:                                            # noqa: BLE001
        pass
