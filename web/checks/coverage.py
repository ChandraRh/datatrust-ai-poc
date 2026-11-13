"""
Coverage and "Start monitoring": which tables of a source are watched, and starter checks for the ones that are not.

    coverage(scope)           every table of the source's database with the kinds of check it has (database scopes), or
                              the tables a factory's Copy steps load and whether they are watched (ADF scopes)
    plan_table(...)           measures one table (read-only) and decides its starter checks
    start(...)                adds those checks (origin 'auto'); they run on the next scheduler round
    table_count(connection)   how many tables the database has (cached), for "trust score based on 40 of 55 tables"

Starter checks answer the three questions every table has: is new data still arriving (freshness), did a normal amount
arrive (volume), and did the columns change (schema). Limits come from what the table shows today, and a check that would
fail on the first run for a reason that is not a problem (a static table, an empty table) is not added: the reason is shown.
"""
from __future__ import annotations

import re
import threading
import time

import appdb

from . import engine, store

MAX_TABLES = 500                 # listed per database
MAX_START = 25                   # tables per Start monitoring request (the page sends batches)
TIME_TYPES = ("timestamp", "datetime", "date", "smalldatetime", "datetimeoffset")
TIME_HINT = ("loaded", "load_", "ingest", "insert", "created", "updated", "modified", "event", "_at", "_ts", "timestamp", "date")
STATIC_AFTER_DAYS = 7
DAILY_WINDOW_MIN_ROWS = 20       # rows in the last 24 h needed before "rows per day" is a meaningful volume
_count_cache: dict = {}


# ============================================================================ the tables of a database
def _conn(connection_id: int) -> dict:
    from estate import store as estate_store
    return estate_store.get_connection(connection_id)


def list_tables(connection_id: int, database: str | None = None) -> list:
    """[{schema, table, columns: [{name, type}]}] for every base table outside the system schemas."""
    d = engine._dialect(connection_id)
    out, by_key = [], {}
    for r in engine.query(connection_id, database, d.tables_sql(), max_rows=40000):
        key = (r["table_schema"], r["table_name"])
        if key not in by_key:
            if len(out) >= MAX_TABLES:
                continue
            by_key[key] = {"schema": key[0], "table": key[1], "columns": []}
            out.append(by_key[key])
        by_key[key]["columns"].append({"name": r["column_name"], "type": str(r["data_type"]).lower()})
    return out


def table_count(connection_id: int, ttl: int = 600) -> int | None:
    """Tables in the connection's database, cached for ttl seconds. Never blocks a page: when the number is missing or old
    it is counted in the background and the last known value (or None) is returned now."""
    hit = _count_cache.get(connection_id)
    if not hit or time.time() - hit[0] >= ttl:
        _count_cache[connection_id] = (time.time(), hit[1] if hit else None)      # claim: one count at a time

        def count():
            try:
                n = len(list_tables(connection_id))
            except Exception:                                          # noqa: BLE001  unreachable: no coverage number
                n = None
            _count_cache[connection_id] = (time.time(), n)
        threading.Thread(target=count, daemon=True, name=f"table-count-{connection_id}").start()
    return (_count_cache.get(connection_id) or (0, None))[1]


def _watched(connection_id: int) -> dict:
    """{(schema, table): {check_type: [check ids]}} of the enabled checks on this connection."""
    out: dict = {}
    if not appdb.table_exists("monitor.checks"):
        return out
    for r in appdb.rows("""SELECT check_id, check_type, schema_name, table_name, last_status FROM monitor.checks
                           WHERE connection_id = %s AND enabled AND table_name IS NOT NULL""", (connection_id,)):
        t = out.setdefault((r["schema_name"], r["table_name"]), {"types": {}, "statuses": []})
        t["types"].setdefault(r["check_type"], []).append(r["check_id"])
        t["statuses"].append(r["last_status"])
    return out


def _worst(statuses: list) -> str | None:
    return next((s for s in ("FAIL", "ERROR", "WARN", "PASS") if s in statuses), None)


# ============================================================================ coverage of one source
def coverage(scope: str | None) -> dict:
    import scopes
    s = scopes.resolve(scope)
    if s["kind"] == "adf":
        return {"scope": s, "kind": "adf", **adf_targets(s["id"])}
    c = _conn(s["id"])
    base = {"scope": s, "kind": "db", "connection": {"id": c["connection_id"], "name": c["name"], "type": c["source_type"],
                                                     "database": c["database"]}}
    try:
        tables = list_tables(s["id"])
    except Exception as e:                                             # noqa: BLE001
        return {**base, "tables": [], "error": f"Could not list the tables: {type(e).__name__}: {str(e)[:300]}"}
    _count_cache[s["id"]] = (time.time(), len(tables))
    watched = _watched(s["id"])
    rows = []
    for t in tables:
        w = watched.get((t["schema"], t["table"])) or {"types": {}, "statuses": []}
        rows.append({"schema": t["schema"], "table": t["table"], "columns": len(t["columns"]),
                     "has_time_column": any(_is_time(x["type"]) for x in t["columns"]),
                     "checks": {k: len(v) for k, v in w["types"].items()}, "status": _worst(w["statuses"]),
                     "monitored": bool(w["types"]), "starter_done": all(k in w["types"] for k in ("freshness", "volume", "schema"))})
    n = sum(r["monitored"] for r in rows)
    return {**base, "tables": rows, "totals": {"tables": len(rows), "monitored": n, "truncated": len(rows) >= MAX_TABLES}}


# ============================================================================ starter checks for one table
def _is_time(type_name: str) -> bool:
    t = type_name.lower()
    return any(x in t for x in TIME_TYPES) and "interval" not in t


def _is_date_only(type_name: str) -> bool:
    return type_name.lower() == "date"


def _rank(col: dict) -> tuple:
    """Best newest-data marker first: timestamps before dates, load/update names before other names."""
    name = col["name"].lower()
    return (_is_date_only(col["type"]), next((i for i, h in enumerate(TIME_HINT) if h in name), len(TIME_HINT)))


def plan_table(connection_id: int, database: str | None, schema: str, table: str, columns: list) -> dict:
    """Measures the table (one read-only query) and returns {checks: [...], skipped: [{check_type, why}], facts}."""
    d = engine._dialect(connection_id)
    t = d.table(schema, table)
    times = sorted([c for c in columns if _is_time(c["type"])], key=_rank)[:3]
    parts = ["COUNT(*) AS n_rows"]
    for i, c in enumerate(times):
        q = d.ident(c["name"])
        parts.append(f"MAX({q}) AS mx{i}")
        if not _is_date_only(c["type"]):
            parts.append(f"COUNT(CASE WHEN {d.since(c['name'], 24)} THEN 1 END) AS w{i}")
    row = engine.query(connection_id, database, f"SELECT {', '.join(parts)} FROM {t}")[0]
    n = int(row.get("n_rows") or 0)
    base = {"connection_id": connection_id, "database": database, "schema_name": schema, "table_name": table, "schedule_minutes": 60}
    label = f"{schema}.{table}"
    checks, skipped = [], []
    facts = {"rows": n, "time_column": None, "newest": None, "age_hours": None, "rows_24h": None}

    # the newest-data marker: the candidate with the most recent value
    best = None
    for i, c in enumerate(times):
        v = row.get(f"mx{i}")
        if v is None:
            continue
        try:
            age = max(engine._hours_since(v), 0)
        except ValueError:
            continue
        if best is None or age < best[1]:
            best = (c, age, v, row.get(f"w{i}"))
    if best:
        facts.update(time_column=best[0]["name"], newest=engine._plain(best[2]), age_hours=round(best[1], 1),
                     rows_24h=int(best[3]) if best[3] is not None else None)

    if n == 0:
        skipped.append({"check_type": "freshness", "why": "The table is empty, so there is no newest row to measure."})
        skipped.append({"check_type": "volume", "why": "The table is empty; a row-count rule would fail straight away. Add one once it loads."})
    else:
        # freshness: is new data still arriving?
        if not best:
            skipped.append({"check_type": "freshness", "why": "No date or time column, so DataTrust cannot tell when rows arrived. "
                                                             "The volume rule still watches the row count."})
        else:
            col, age = best[0], best[1]
            daily = 36 if _is_date_only(col["type"]) else 26
            if age <= daily:
                limit, how = daily, "it receives new rows every day"
            elif age <= STATIC_AFTER_DAYS * 24:
                limit, how = 8 * 24, "it loads less often than daily"
            else:
                limit = None
            if limit:
                checks.append({**base, "check_type": "freshness", "name": f"{label} is fresh", "severity": "HIGH",
                               "config": {"column": col["name"], "max_age_hours": limit},
                               "description": f"Started by Start monitoring: the newest {col['name']} was {age:.0f} h old and {how}, "
                                              f"so more than {limit} h without new rows means loading stopped."})
            else:
                skipped.append({"check_type": "freshness",
                                "why": f"The newest {col['name']} is {age / 24:.0f} days old: the table looks static or has already "
                                       f"stopped loading. Add a freshness rule by hand if it should still receive data."})
        # volume: did a normal amount arrive?
        win = best if best and not _is_date_only(best[0]["type"]) and (best[3] or 0) >= DAILY_WINDOW_MIN_ROWS else None
        if win:
            checks.append({**base, "check_type": "volume", "name": f"{label} rows per day", "severity": "MEDIUM",
                           "config": {"min_rows": 1, "max_drop_pct": 50, "window_column": win[0]["name"], "window_hours": 24},
                           "description": f"Started by Start monitoring: {int(win[3]):,} rows arrived in the last 24 h. Fails when a day "
                                          f"brings less than half the usual amount (a partial or failed load)."})
        else:
            checks.append({**base, "check_type": "volume", "name": f"{label} row count", "severity": "MEDIUM",
                           "config": {"min_rows": 1, "max_drop_pct": 50},
                           "description": f"Started by Start monitoring: the table has {n:,} rows. Fails when it suddenly holds less "
                                          f"than half the usual count (rows deleted, a table reloaded half-way)."})
    # schema: did the columns change? The first run records today's columns as the accepted schema.
    checks.append({**base, "check_type": "schema", "name": f"{label} columns unchanged", "severity": "HIGH", "config": {},
                   "description": f"Started by Start monitoring: records today's {len(columns)} columns and fails when one is dropped, "
                                  f"renamed or changes type."})
    return {"checks": checks, "skipped": skipped, "facts": facts}


def start(connection_id: int, tables: list, by: str) -> dict:
    """Adds the starter checks to each table that does not have that kind yet. tables: [{schema, table}]."""
    if not tables:
        raise ValueError("choose at least one table")
    if len(tables) > MAX_START:
        raise ValueError(f"at most {MAX_START} tables per request")
    c = _conn(connection_id)
    engine._dialect(connection_id)                                     # checks are not available for this database type -> error
    try:
        known = {(t["schema"], t["table"]): t for t in list_tables(connection_id)}
    except Exception as e:                                             # noqa: BLE001  unreachable database: say so
        raise ValueError(f"Could not read the tables of {c['name']}: {type(e).__name__}: {str(e)[:200]}") from e
    watched = _watched(connection_id)
    results = []
    for want in tables:
        key = (want.get("schema"), want.get("table"))
        item = {"schema": key[0], "table": key[1], "added": [], "kept": [], "skipped": []}
        results.append(item)
        if key not in known:
            item["error"] = "table not found"
            continue
        try:
            plan = plan_table(connection_id, None, key[0], key[1], known[key]["columns"])
        except Exception as e:                                         # noqa: BLE001  one table must not stop the others
            item["error"] = f"{type(e).__name__}: {str(e)[:200]}"
            continue
        have = (watched.get(key) or {}).get("types", {})
        for chk in plan["checks"]:
            if chk["check_type"] in have:
                item["kept"].append(chk["check_type"])                 # already watched this way: keep the user's rule
                continue
            cid = store.add_check({**chk, "name": chk["name"][:120]}, by, origin="auto")
            item["added"].append({"check_id": cid, "check_type": chk["check_type"], "name": chk["name"]})
        item["skipped"] = [s for s in plan["skipped"] if s["check_type"] not in have]
        item["facts"] = plan["facts"]
    _count_cache.pop(connection_id, None)
    return {"connection": c["name"], "results": results, "added": sum(len(r["added"]) for r in results)}


# ============================================================================ the tables an ADF factory loads
def _connection_for(asset: dict, cfg: dict, conns: list) -> dict | None:
    host = (asset.get("host") or "").lower()
    match = [c for c in conns if host and (c.get("host") or "").lower() == host]
    if not match and cfg.get("target_connection_id"):
        match = [c for c in conns if c["connection_id"] == cfg["target_connection_id"]]
    return match[0] if match else None


LOOKUP_REF = re.compile(r"activity\('([^']+)'\)\.output\.firstRow\.(\w+)")


def _runtime_tables(source_id: int, pipeline: str, schema, table) -> list:
    """A table name set at run time from a Lookup (metadata-driven pipelines: one Copy step loads many tables).
    Reads the Lookup's output in the recent runs and returns every distinct (schema, table) it named."""
    refs = {part: LOOKUP_REF.search(str(v or "")) for part, v in (("schema", schema), ("table", table))}
    if not any(refs.values()):
        return []
    acts = {m.group(1) for m in refs.values() if m}
    out = []
    for r in appdb.rows("""SELECT run_id, activity_name, output FROM telemetry.task_runs
                           WHERE source_id = %s AND pipeline_name = %s AND activity_name = ANY(%s) AND status = 'Succeeded'
                             AND run_start > now() - interval '30 days'
                           ORDER BY run_start DESC LIMIT 500""", (source_id, pipeline, list(acts))):
        first = ((r.get("output") or {}) if isinstance(r.get("output"), dict) else {}).get("firstRow") or {}
        got = {}
        for part, m in refs.items():
            value = (first.get(m.group(2)) if m and m.group(1) == r["activity_name"] else
                     None if m else (schema if part == "schema" else table))
            got[part] = value
        if got["schema"] and got["table"] and "@" not in str(got["schema"]) + str(got["table"]):
            pair = (str(got["schema"]), str(got["table"]))
            if pair not in out:
                out.append(pair)
    return out


def adf_targets(source_id: int) -> dict:
    """Every table a Copy step of this factory writes into, where it lives and whether DataTrust watches it between loads."""
    from adf import resolve
    from adf import store as adf_store
    from estate import store as estate_store
    src = adf_store.get_source(source_id)
    cfg = src.get("config") or {}
    defs = {"pipeline": {}, "dataset": {}, "linkedservice": {}}
    for r in appdb.rows("SELECT kind, name, body FROM telemetry.definitions WHERE source_id = %s AND NOT removed", (source_id,)):
        defs.setdefault(r["kind"], {})[r["name"]] = adf_store.loads(r["body"])
    conns = [c for c in estate_store.list_connections() if c["source_type"] in ("postgresql", "sqlserver")]
    latest = {r["pipeline_name"]: r for r in appdb.rows("""SELECT DISTINCT ON (pipeline_name) * FROM telemetry.pipeline_runs
                                                           WHERE source_id = %s ORDER BY pipeline_name, run_start DESC""", (source_id,))}
    out, seen, watched = [], set(), {}
    for pname, p in sorted(defs["pipeline"].items()):
        for a in resolve.flatten_activities((p.get("properties") or {}).get("activities")):
            if a.get("type") != "Copy":
                continue
            try:
                ctx = resolve.run_context(latest.get(pname), pname, cfg.get("factory_name"))
                t = resolve.copy_assets(a, defs["dataset"], defs["linkedservice"], ctx).get("target") or {}
            except Exception:                                          # noqa: BLE001  one odd definition must not hide the rest
                continue
            if t.get("kind") not in ("postgres", "postgresql", "sqlserver"):
                continue
            schema, table = t.get("schema"), t.get("table")
            dynamic = "@" in f"{schema or ''}{table or ''}"
            names = _runtime_tables(source_id, pname, schema, table) if dynamic else ([(schema, table)] if schema and table else [])
            c = _connection_for(t, cfg, conns)
            base = {"pipeline": pname, "activity": a["name"], "host": t.get("host"), "database": t.get("database"),
                    "connection": {"id": c["connection_id"], "name": c["name"]} if c else None, "monitored": False, "checks": {},
                    "from_lookup": dynamic}
            if not names:
                out.append({**base, "schema": None, "table": None,
                            "note": "The table name is decided at run time and no recent run named it, so DataTrust cannot list it yet."})
                continue
            for sch, tab in names:
                row = {**base, "schema": sch, "table": tab}
                if not c:
                    row["note"] = (f"No connection to {t.get('host') or 'this server'} yet: add it under Data estate → Connections "
                                   "(same host name as the ADF linked service) to watch this table.")
                else:
                    if c["connection_id"] not in watched:
                        watched[c["connection_id"]] = _watched(c["connection_id"])
                    w = watched[c["connection_id"]].get((sch, tab)) or {"types": {}, "statuses": []}
                    row.update(checks={k: len(v) for k, v in w["types"].items()}, monitored=bool(w["types"]),
                               status=_worst(w["statuses"]), starter_done=all(k in w["types"] for k in ("freshness", "volume", "schema")))
                key = (pname, sch, tab, (row["connection"] or {}).get("id"))
                if key in seen:
                    continue
                seen.add(key)
                out.append(row)
    n = sum(r["monitored"] for r in out)
    return {"factory": src["name"], "targets": out, "totals": {"tables": len(out), "monitored": n}}
