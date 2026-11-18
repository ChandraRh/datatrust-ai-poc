"""
Access page, "Who has access": a read-only review of one source's database. For every login and role: which schemas it can
read, which it can change, what it owns, and the risks worth a look:

    superuser      can do anything on the server (PostgreSQL superuser, SQL Server sysadmin / dbo / db_owner)
    writes_gold    can change the gold schema, which the reports read (only the pipeline should)
    no_expiry      a PostgreSQL login whose password never expires
    PUBLIC         rights granted to everyone: every login on the server has them

Built only from the connectors' own read-only queries (roles, role_access); nothing is changed. Rights a role gets
through a group role it belongs to are not expanded: the group is listed with its own rights, and "member of" links them.
"""
from __future__ import annotations

from . import service, store

MAX_ROLES = 60
READ = {"SELECT"}
WRITE = {"INSERT", "UPDATE", "DELETE", "TRUNCATE", "ALTER", "CONTROL", "TAKE OWNERSHIP"}
REPORT_SCHEMAS = {"gold"}
# built-in roles that grant rights on every schema: SQL Server's fixed database roles, PostgreSQL's predefined data roles
FIXED = {"db_owner": ("all", "all", True), "db_datareader": ("all", None, False), "db_datawriter": (None, "all", False),
         "db_ddladmin": (None, "all", False), "pg_read_all_data": ("all", None, False), "pg_write_all_data": (None, "all", False)}


def _privs(text) -> set:
    return {p.strip().upper() for p in str(text or "").split(",") if p.strip() and "(DENY)" not in p.upper()}


def review(cid: int, database: str) -> dict:
    conn = store.get_connection(cid)
    own = (conn.get("database") or "").strip()
    if own and database.lower() != own.lower():
        raise ValueError(f"This source is the database {own}.")
    src = service.source(cid)
    if not hasattr(src, "role_access"):
        return {"available": False, "note": f"Reviewing access is not available for {conn.get('source_type')} sources yet.",
                "people": [], "public": [], "counts": {}}
    roles = src.roles(database)
    out, public, truncated = [], {}, len(roles) > MAX_ROLES
    for r in roles[:MAX_ROLES]:
        a = src.role_access(database, r["name"])
        reads, writes = set(), set()
        for o in a.get("objects") or []:
            p = _privs(o.get("privileges"))
            if o.get("via_public"):                    # rights everyone has: listed once, under PUBLIC
                public.setdefault(o["schema"], set()).update(p)
                continue
            if p & READ:
                reads.add(o["schema"])
            if p & WRITE:
                writes.add(o["schema"])
        for s in a.get("schemas") or []:
            if _privs(s.get("privileges")) & {"CONTROL", "ALTER"}:
                writes.add(s["schema"])
        owns = sorted({o["schema"] for o in a.get("owns") or []})
        writes |= set(owns)                            # an owner can change what it owns
        superuser = bool(r.get("superuser"))
        for g in r.get("member_of") or []:             # SQL Server fixed roles: rights on every schema
            if g in FIXED:
                rd, wr, su = FIXED[g]
                if rd: reads.add("(all)")
                if wr: writes.add("(all)")
                superuser = superuser or su
        reads |= writes                                # whoever can change a schema can read it
        risks = []
        if superuser:
            risks.append("superuser")
        if not superuser and (writes & REPORT_SCHEMAS or "(all)" in writes):    # a superuser already says it all
            risks.append("writes_gold")
        if r.get("can_login") and conn.get("source_type") == "postgresql" and not r.get("valid_until") and not superuser:
            risks.append("no_expiry")
        out.append({"name": r["name"], "kind": "login" if r.get("can_login") else "group role", "superuser": superuser,
                    "member_of": r.get("member_of") or [], "reads": sorted(reads), "writes": sorted(writes), "owns": owns,
                    "can_connect": bool((a.get("database") or {}).get("connect")), "risks": risks})
    order = {"superuser": 0, "writes_gold": 1, "no_expiry": 2}
    out.sort(key=lambda x: (min([order[k] for k in x["risks"]] or [9]), x["kind"] != "login", x["name"].lower()))
    pub = [{"schema": s, "privileges": sorted(p)} for s, p in sorted(public.items())]
    return {"available": True, "source": conn["name"], "database": database, "people": out, "public": pub, "truncated": truncated,
            "counts": {"logins": sum(x["kind"] == "login" for x in out), "superusers": sum(x["superuser"] for x in out),
                       "write_gold": sum("writes_gold" in x["risks"] for x in out),
                       "public_schemas": len(pub), "no_expiry": sum("no_expiry" in x["risks"] for x in out)}}
