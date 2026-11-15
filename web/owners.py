"""
Who owns an incident, the same way for every source.

A person from the people directory (Connect, portal.people) is made owner of
    a whole source          scope "db:3" / "adf:3", target ""
    one table or pipeline   scope "db:3", target "sales.orders"  /  scope "adf:3", target "PL_01_File_Monitoring"
The most specific owner wins (the table or pipeline, else the whole source). Only when nobody is set explicitly does the old
guess apply: the "data they own" words in Connect matched against the incident's text; it is marked as a guess.
Stored in DataTrust's own database (portal.ownership).
"""
from __future__ import annotations

import re

import appdb

DDL = """
CREATE TABLE IF NOT EXISTS portal.ownership (
    scope      TEXT NOT NULL,
    target     TEXT NOT NULL DEFAULT '',
    person_id  INTEGER NOT NULL REFERENCES portal.people (person_id) ON DELETE CASCADE,
    set_by     TEXT,
    set_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (scope, target, person_id)
);
"""


def ensure_tables() -> None:
    with appdb.connect() as conn:
        conn.execute(DDL)
        conn.commit()


def target_of(inc: dict) -> tuple[str | None, str]:
    """(what in its source the incident is about, what kind of thing that is): a pipeline, a table, or nothing specific."""
    m = inc.get("metrics") or {}
    if str(inc.get("id", "")).startswith("adf:"):
        p = (inc.get("adf") or {}).get("pipeline") or m.get("pipeline")
        return (p, "pipeline") if p else (None, "")
    t = m.get("target")
    if t and re.match(r"^[\w$#@]+\.[\w$#@]+$", str(t)):
        return t, "table"
    if m.get("entity"):                                      # the POC pipeline's tables
        return f"silver.{m['entity']}", "table"
    return None, ""


def _assignments() -> list:
    if not appdb.table_exists("portal.ownership"):
        return []
    return appdb.rows("""SELECT o.scope, o.target, o.set_by, o.set_at, p.person_id, p.name, p.email, p.chat_link, p.team, p.title, p.phone
                         FROM portal.ownership o JOIN portal.people p USING (person_id)
                         WHERE p.active ORDER BY o.scope, o.target, lower(p.name)""")


def _person(a: dict, why: str, guess: bool = False) -> dict:
    return {"person_id": a["person_id"], "name": a["name"], "email": a["email"], "chat_link": a.get("chat_link"),
            "team": a.get("team"), "phone": a.get("phone"), "why": why, "guess": guess}


def for_incident(inc: dict, scope: str, assignments: list | None = None) -> list:
    rows = _assignments() if assignments is None else assignments
    target, kind = target_of(inc)
    mine = [a for a in rows if a["scope"] == scope]
    if target:
        specific = [a for a in mine if a["target"] and a["target"].lower() == target.lower()]
        if specific:
            return [_person(a, f"owns {kind} {target}") for a in specific]
    whole = [a for a in mine if not a["target"]]
    if whole:
        return [_person(a, "owns this source") for a in whole]
    try:                                                     # nobody set: the old guess from Connect's "data they own"
        from estate import store as estate_store
        return [_person(p, "named in Connect for " + ", ".join(p["matched"]), True)
                for p in estate_store.match_people(f"{inc.get('title', '')} {inc.get('summary', '')}")[:3]]
    except Exception:                                        # noqa: BLE001
        return []


def attach(found: list, scope: str) -> None:
    """Every incident gets 'owners' (one read of the ownership table for the whole list)."""
    rows = _assignments()
    for i in found:
        i["owners"] = for_incident(i, scope, rows)
        i["owner_target"] = dict(zip(("target", "kind"), target_of(i)))


def set_owner(scope: str, target: str, person_id: int, by: str) -> None:
    scope, target = (scope or "").strip(), (target or "").strip()
    if not re.match(r"^(db|adf):\d+$", scope):
        raise ValueError("scope must be a source key such as db:3 or adf:3")
    if len(target) > 200:
        raise ValueError("table or pipeline name too long")
    if not appdb.one("SELECT 1 AS ok FROM portal.people WHERE person_id = %s AND active", (person_id,)):
        raise ValueError("that person is not in the people directory (Connect)")
    with appdb.connect() as conn:
        conn.execute("""INSERT INTO portal.ownership (scope, target, person_id, set_by) VALUES (%s, %s, %s, %s)
                        ON CONFLICT (scope, target, person_id) DO UPDATE SET set_by = EXCLUDED.set_by, set_at = now()""",
                     (scope, target, person_id, by))
        conn.commit()


def remove_owner(scope: str, target: str, person_id: int) -> None:
    with appdb.connect() as conn:
        conn.execute("DELETE FROM portal.ownership WHERE scope = %s AND target = %s AND person_id = %s",
                     (scope, (target or "").strip(), person_id))
        conn.commit()


def list_for(scope: str) -> list:
    return [a for a in _assignments() if a["scope"] == scope]
