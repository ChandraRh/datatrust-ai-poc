"""
Schema-change guard ("blast radius").

When someone drops, renames or changes the type of a table / view / column that the pipeline uses, the guard
  1. finds the change in the who-changed-what log (dt_audit.ddl_log, installed by "Enable tracking" in the portal),
  2. works out what breaks downstream: the pipeline step that reads it, the silver table it feeds, every view that
     reads it (also views on views), and which of them use the changed column,
  3. raises an incident with the owners from Connect, so the right people are contacted at once.
Safe changes (ADD COLUMN, CREATE ..., the pipeline's own staging tables) are ignored.
An incident goes away when someone acknowledges it (Impact page -> Approve), and then shows as resolved on Verify.

blast_radius(conn, schema, name, column=None) is also used by the catalog's "What breaks if this changes?" button.
"""
import re

import data_service as ds

WATCHED_SCHEMAS = ("bronze", "silver", "gold")
WINDOW_DAYS = 14
BREAKING = [
    (r"\bdrop\s+column\b", "column dropped"),
    (r"\brename\s+column\b|\brename\s+[\w\"]+\s+to\b", "column renamed"),
    (r"\balter\s+column\s+[\w\"]+\s+(set\s+data\s+)?type\b", "column type changed"),
    (r"\brename\s+to\b", "renamed"),
    (r"\bset\s+schema\b", "moved to another schema"),
]


def _entity_of(table: str):
    """bronze.payments / silver.payments -> ('payments', role in the pipeline)."""
    for entity, (_, bronze_t, silver_t, _, _) in ds.ENTITIES.items():
        if table == bronze_t:
            return entity, "bronze"
        if table == silver_t:
            return entity, "silver"
    return None, None


def blast_radius(conn, schema: str, name: str, column: str = None, with_pipeline: bool = True) -> dict:
    """Everything downstream of schema.name (and, if given, of that column). conn: a psycopg dict_row connection.
    with_pipeline=False for databases other than the pipeline's own (only the catalog's view dependencies count)."""
    full = f"{schema}.{name}"
    downstream, seen = [], set()

    def add(obj, kind, how, uses_column=None):
        if obj not in seen:
            seen.add(obj)
            downstream.append({"object": obj, "kind": kind, "how": how, "uses_column": uses_column})

    entity, role = _entity_of(full) if with_pipeline else (None, None)
    if entity and role == "bronze":
        silver_t = ds.ENTITIES[entity][2]
        col_used = None
        if column:                                   # does the silver table carry a column of that name?
            col_used = bool(conn.execute("""SELECT 1 FROM information_schema.columns
                                            WHERE table_schema || '.' || table_name = %s AND column_name = %s""",
                                         (silver_t, column)).fetchone())
        add(f"pipeline step PL_SILVER_{entity.upper()}", "pipeline", f"reads {full} on every run", col_used)
        add(silver_t, "silver table", f"loaded from {full} by the silver layer", col_used)
        start = [] if col_used is False else [silver_t]             # column not carried to silver: nothing further breaks
    elif entity and role == "silver":
        add(f"pipeline step PL_SILVER_{entity.upper()}", "pipeline", f"writes {full} on every run", True if column else None)
        start = [full]
    else:
        start = [full]
    if schema == "gold" and with_pipeline:                   # gold is what people and tools read
        add("reports, dashboards and the DataTrust AI agent", "consumers", "gold is the reporting layer", None)

    # every view that reads the object, then views on those views (recursive), with column usage where known
    frontier = list(start)
    while frontier:
        obj = frontier.pop(0)
        s, n = obj.split(".", 1)
        for v in conn.execute("""SELECT DISTINCT view_schema || '.' || view_name AS v FROM information_schema.view_table_usage
                                 WHERE table_schema = %s AND table_name = %s ORDER BY 1""", (s, n)).fetchall():
            uses = None
            if column and obj == full:
                uses = bool(conn.execute("""SELECT 1 FROM information_schema.view_column_usage
                                            WHERE view_schema || '.' || view_name = %s AND table_schema = %s
                                              AND table_name = %s AND column_name = %s""", (v["v"], s, n, column)).fetchone())
            elif column and obj != full:
                uses = None
            kind = "gold view" if v["v"].startswith("gold.") else "view"
            if v["v"] not in seen:
                add(v["v"], kind, f"reads {obj}", uses)
                frontier.append(v["v"])
    return {"object": full, "column": column, "downstream": downstream,
            "gold_views": [d["object"] for d in downstream if d["kind"] == "gold view"],
            "breaks_pipeline": any(d["kind"] == "pipeline" for d in downstream)}


def _owners(objects: list) -> list:
    try:
        from estate import store
        return store.match_people(" ".join(objects))
    except Exception:
        return []


def incidents(approvals: dict) -> list:
    """Breaking schema changes of the last WINDOW_DAYS days on bronze / silver / gold (not yet acknowledged)."""
    if not ds.table_exists("dt_audit.ddl_log"):
        return []
    events = ds.rows("""
        SELECT log_id, event_time, username, client_addr, application, command_tag, object_type, schema_name,
               object_identity, query
        FROM dt_audit.ddl_log
        WHERE event_time > now() - make_interval(days => %(d)s)
          AND command_tag IN ('ALTER TABLE', 'DROP TABLE', 'DROP VIEW', 'DROP MATERIALIZED VIEW', 'ALTER VIEW')
          AND schema_name = ANY(%(s)s)
          AND object_identity NOT LIKE 'silver.stg\\_%%'                -- the pipeline's own staging tables
        ORDER BY log_id""", {"d": WINDOW_DAYS, "s": list(WATCHED_SCHEMAS)})
    found, seen_keys = [], set()
    with ds.connect() as conn:
        acknowledged = set()
        for e in events:
            iid = f"SCHEMA-{e['log_id']}"
            q = (e["query"] or "").lower()
            ident = (e["object_identity"] or "").replace('"', "")
            if e["command_tag"].startswith("DROP"):
                change, column = f"{e['object_type']} dropped", None
                schema, name = ident.split(".", 1) if "." in ident else (e["schema_name"], ident)
            else:
                change = next((label for pattern, label in BREAKING if re.search(pattern, q)), None)
                if not change:
                    continue                                        # ADD COLUMN, defaults, comments ...: not breaking
                orig = re.search(r"\balter\s+(?:table|view)\s+(?:if\s+exists\s+)?(?:only\s+)?([\w\".]+)", q)
                if orig and change in ("renamed", "moved to another schema"):
                    ident = orig.group(1).replace('"', "")              # the name it HAD (the pipeline still uses it)
                    if "." not in ident:
                        ident = f"{e['schema_name']}.{ident}"
                schema, name = ident.split(".", 1) if "." in ident else (e["schema_name"], ident)
                m = re.search(r"\b(?:drop|rename|alter)\s+column\s+(?:if\s+exists\s+)?\"?([\w]+)\"?", q)
                column = m.group(1) if m else None
                name = name.split(".")[0]
            key = (schema, name, column, change, (e["query"] or "").strip(), str(e["event_time"])[:19])
            if key in seen_keys:                                    # one statement can log the table and the column
                continue
            seen_keys.add(key)
            if iid in approvals:                                    # acknowledged -> shows as resolved on Verify
                continue
            radius = blast_radius(conn, schema, name, column)
            hit = [d for d in radius["downstream"] if d["uses_column"] is not False]
            owners = _owners([radius["object"]] + [d["object"] for d in hit])
            if not hit:
                severity = "LOW"                                    # e.g. a raw column nobody downstream uses
            elif radius["breaks_pipeline"] and e["command_tag"].startswith("DROP"):
                severity = "CRITICAL"
            elif any(d["kind"] in ("pipeline", "gold view", "consumers") for d in hit):
                severity = "HIGH"
            else:
                severity = "MEDIUM"
            what = f"{radius['object']}" + (f".{column}" if column else "")
            found.append({
                "id": iid, "severity": severity, "type": "SCHEMA_CHANGE",
                "title": f"Schema change: {what} {change} by {e['username']}",
                "summary": (f"{e['command_tag']} at {str(e['event_time'])[:16]} from {e['application'] or e['client_addr'] or 'unknown client'}. "
                            + (f"Breaks: {', '.join(d['object'] for d in hit[:6])}." if hit else "Nothing downstream reads it.")
                            + (f" Owners: {', '.join(o['name'] for o in owners)}." if owners else "")),
                "business_date": None,
                "metrics": {"object": radius["object"], "column": column, "change": change, "who": e["username"],
                            "when": e["event_time"], "client": e["client_addr"], "application": e["application"],
                            "query": e["query"], "downstream": radius["downstream"], "gold_views": radius["gold_views"],
                            "owners": [{"name": o["name"], "email": o["email"]} for o in owners]},
            })
    return found


def investigate(inc: dict) -> dict:
    """Evidence for a SCHEMA_CHANGE incident (used by data_service.investigate)."""
    m = inc["metrics"]
    checks = [
        {"ok": False, "text": f"{m['change'].capitalize()}: {m['object']}{'.' + m['column'] if m['column'] else ''}",
         "source": "dt_audit.ddl_log (who-changed-what)"},
        {"ok": True, "text": f"Changed by {m['who']} at {str(m['when'])[:19]} from {m['application'] or m['client'] or 'unknown client'}",
         "source": "dt_audit.ddl_log"},
        {"ok": not m["downstream"], "text": f"{len(m['downstream'])} downstream object(s) depend on it", "source": "information_schema.view_table_usage + pipeline lineage"},
        {"ok": bool(m["owners"]), "text": ("Owners: " + ", ".join(o["name"] for o in m["owners"])) if m["owners"] else "No owner recorded in Connect",
         "source": "portal.people"},
    ]
    chain = [f"{m['who']} ran: {(m['query'] or '')[:120]}"] + [f"{d['object']} ({d['kind']}): {d['how']}" for d in m["downstream"][:8]]
    root = (f"{m['object']} was changed ({m['change']}). Everything downstream that reads it can fail or show wrong data "
            f"on the next pipeline run until the change is reverted or the pipeline is updated.")
    sample = [{"downstream": d["object"], "kind": d["kind"], "how": d["how"],
               "uses_changed_column": "yes" if d["uses_column"] else "no" if d["uses_column"] is False else "–"} for d in m["downstream"]]
    return {"checks": checks, "chain": chain, "root_cause": root, "sample": sample}
