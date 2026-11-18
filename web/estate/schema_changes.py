"""
Schema changes (Data estate page): who created, changed or dropped which table or view in one source's database, when, and how.

The change log itself is the database's (dt_audit.ddl_log with change tracking on, or Snowflake's query history); this
module only makes it readable:
    * one source at a time: the source picked at the top of the page, i.e. a connection's own database
    * a period (the last N days) with real counts, instead of "the newest 200 rows"
    * DataTrust's own housekeeping is hidden unless asked for: every start adds missing columns to its own tables
      (portal, monitor, app, telemetry), which would otherwise bury the changes people made
    * repeats are grouped: the same statement on the same object by the same person in the same minute is one row
Failures are not repeated here: they are on Pipeline log, ADF runs and Detect. Only SQL Server's failed Agent jobs,
which no other page shows, are passed through.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from . import service, store

INTERNAL_SCHEMAS = {"portal", "monitor", "app", "telemetry", "dt_audit"}     # DataTrust's own tables
READ_LIMIT = 5000                                                            # rows read from the log per request
VERB = {"CREATE": ("Created", "ok"), "ALTER": ("Changed", "warn"), "DROP": ("Dropped", "bad"), "GRANT": ("Granted", "grey"),
        "REVOKE": ("Revoked", "grey"), "COMMENT": ("Commented", "grey"), "RENAME": ("Renamed", "warn"), "TRUNCATE": ("Emptied", "bad")}


def _when(v) -> datetime | None:
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=timezone.utc)
    try:
        d = datetime.fromisoformat(str(v))
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def internal(r: dict) -> bool:
    schema = (r.get("schema_name") or str(r.get("object_identity") or "").split(".")[0]).strip('"[]').lower()
    return schema in INTERNAL_SCHEMAS or str(r.get("application") or "").lower().startswith("datatrust")


def action(command_tag: str) -> dict:
    """'ALTER TABLE' -> Changed table (warn); 'DROP VIEW' -> Dropped view (bad)."""
    words = (command_tag or "").upper().split()
    verb, tone = VERB.get(words[0] if words else "", ((command_tag or "").capitalize(), "grey"))
    what = " ".join(words[1:]).lower()
    return {"label": f"{verb} {what}".strip(), "tone": tone, "verb": (words[0] if words else "").lower()}


def schema_changes(cid: int, database: str, days: int = 30, show_internal: bool = False, object_name: str | None = None,
                   username: str | None = None, now: datetime | None = None) -> dict:
    if not 1 <= int(days) <= 365:
        raise ValueError("days must be between 1 and 365")
    conn = store.get_connection(cid)
    own = (conn.get("database") or "").strip()
    if own and database.lower() != own.lower():
        raise ValueError(f"This source is the database {own}.")
    out = {"source": conn["name"], "database": database, "source_type": conn.get("source_type"), "tracking": None,
           "days": int(days), "rows": [], "counts": {}, "hidden_internal": 0, "jobs": [], "truncated": False, "unreachable": None}
    src = service.source(cid)
    try:
        out["tracking"] = tracking = src.tracking_enabled(database)
    except Exception as e:                                                   # noqa: BLE001  server down, network, login
        out["unreachable"] = str(e).splitlines()[0][:300]
        return out
    if not tracking:
        return out
    raw = src.changes(database, object_name or None, username or None, READ_LIMIT)
    out["truncated"] = len(raw) >= READ_LIMIT
    since = (now or datetime.now(timezone.utc)) - timedelta(days=int(days))
    groups: dict = {}
    for r in raw:
        at = _when(r.get("event_time"))
        if at is None or at < since:
            continue
        if internal(r) and not show_internal:
            out["hidden_internal"] += 1
            continue
        key = (at.strftime("%Y-%m-%d %H:%M"), r.get("username"), r.get("command_tag"), r.get("object_identity"))
        g = groups.get(key)
        if g:
            g["times"] += 1
            continue
        a = action(r.get("command_tag"))
        groups[key] = {"at": at.isoformat(sep=" ", timespec="seconds"), "who": r.get("username") or "",
                       "action": a["label"], "tone": a["tone"], "verb": a["verb"], "object_type": r.get("object_type") or "",
                       "object": r.get("object_identity") or "", "app": r.get("application") or "",
                       "client": r.get("client_addr") or "", "sql": (r.get("query") or "").strip(), "times": 1,
                       "datatrust": internal(r)}
    rows = sorted(groups.values(), key=lambda g: g["at"], reverse=True)
    out["rows"] = rows
    out["counts"] = {"changes": len(rows), "objects": len({g["object"] for g in rows}),
                     "dropped": sum(g["verb"] in ("drop", "truncate") for g in rows), "people": len({g["who"] for g in rows if g["who"]})}
    if conn.get("source_type") == "sqlserver":                               # failed Agent jobs: shown on no other page
        try:
            out["jobs"] = (src.failures(database) or {}).get("jobs") or []
        except Exception:                                                    # noqa: BLE001  no rights on msdb, Azure SQL
            out["jobs"] = []
    return out
