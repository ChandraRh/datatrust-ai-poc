"""
Follow-up checks on Investigate: after an incident's fixed evidence, questions that narrow down WHY it happened,
on any connected database. Everything runs read-only on the source, each query stopped after SECONDS.

Step 1  plan(incident_id)   a fixed plan per kind of problem (no AI):
          volume       rows per day (when did it drop), hour by hour against the same day a week before, which group dropped
          freshness    the days that received rows (when did loads stop)
          null_rate    empty values per day (when did they start), which group sends them
          uniqueness   copies inside one load, or the same key loaded again on a later day
          schema       who reads the table (they may break)
          POC rules    is this new (same rule, last 30 business dates), which batch and file sent the rows, other rules that day
          every kind   upstream in the lineage map: the tables it is loaded from and the pipeline that writes it, with their
                       checks, open incidents and the pipeline's last run
Step 2  dig(incident_id)    on request only: the AI model proposes up to MAX_QUERIES read-only queries, DataTrust checks them
          (one SELECT, only the incident's tables, no write, at most ROWS rows, SECONDS) and runs them, then the model reads
          the results. Text values are replaced by tokens ([v1], [v2] ...) before the model sees anything and are put back
          for display; numbers and dates are sent as they are. Its answer never replaces the fixed evidence.
"""
from __future__ import annotations

import json
import re
import statistics
import threading
import time
from collections import defaultdict
from datetime import date, datetime, timedelta

import appdb

SECONDS = 15                  # each follow-up query is stopped on the database after this
DAYS = 14                     # history read for "when did it start"
CACHE_SECONDS = 300           # the plan is run again at most every 5 minutes per incident (Refresh forces it)
MAX_QUERIES = 3
ROWS = 50
TIME_HINT = ("loaded_at", "ingested", "updated_at", "modified", "inserted", "created_at", "event_time", "_ts", "timestamp",
             "business_date", "_date", "date")
GROUP_HINT = ("source", "system", "channel", "region", "country", "store", "branch", "partner", "vendor", "supplier", "provider",
              "platform", "site", "origin", "file", "batch", "segment", "method", "category", "type", "status")
_cache: dict = {}
_lock = threading.Lock()


# ============================================================================ what the incident is about
def _target(incident_id: str, scope: str | None = None) -> dict:
    """kind: check (a rule on a database), poc (a built-in POC pipeline incident), adf / other (no database to ask)."""
    import data_service as ds
    import scopes
    scope = scope or scopes.scope_of_incident(incident_id)
    s = scopes.resolve(scope)
    if s["kind"] != "db":
        return {"kind": s["kind"], "scope": s}
    from estate import store as estate_store
    conn = estate_store.get_connection(s["id"])
    if s["poc"] and not incident_id.startswith(f"db{s['id']}:"):
        inc = next((i for i in ds.incidents() if i["id"] == incident_id), None)
        return {"kind": "poc" if inc else "gone", "scope": s, "incident": inc, "connection_id": s["id"], "database": None, "conn": conn}
    inc = next((i for i in scopes._incidents(s) if i["id"] == incident_id), None)
    if not inc:
        return {"kind": "gone", "scope": s}
    if inc.get("source") != "check":
        return {"kind": "other", "scope": s, "incident": inc}
    from checks import store
    chk = store.get_check(inc["metrics"]["check_id"])
    last = (store.history(chk["check_id"], 1) or [{}])[0]
    return {"kind": "check", "scope": s, "incident": inc, "check": chk, "last": last, "connection_id": chk["connection_id"],
            "database": chk.get("database") or None, "conn": conn}


def _q(t: dict, sql: str, params=(), max_rows: int = 500) -> list:
    from checks import engine
    return engine.query(t["connection_id"], t.get("database"), sql, params, max_rows=max_rows, seconds=SECONDS)


def _d(t: dict):
    from checks import engine
    return engine._dialect(t["connection_id"])


def _plain(v):
    from checks import engine
    return engine._plain(v)


def _day(v) -> date | None:
    from checks import engine
    return engine._as_date(v)


def _n(v) -> str:
    return f"{int(v):,}" if v is not None else "–"


def _columns(t: dict, schema: str, table: str) -> list:
    from checks import engine
    return engine.columns(t["connection_id"], t.get("database"), schema, table)


def _time_column(t: dict, cols: list) -> tuple[str | None, bool]:
    """The column that says when a row arrived: the rule's own, another rule's on the same table, or the likeliest by name."""
    chk = t["check"]
    c = chk.get("config") or {}
    if chk["check_type"] == "volume" and c.get("window_column"):
        return c["window_column"], bool(c.get("date_only"))
    if chk["check_type"] == "freshness" and c.get("column"):
        return c["column"], bool(c.get("date_only"))
    from checks import store
    for o in store.list_checks(chk["connection_id"]):
        oc = o.get("config") or {}
        if (o["schema_name"], o["table_name"]) == (chk["schema_name"], chk["table_name"]) and o["check_type"] == "freshness" and oc.get("column"):
            return oc["column"], bool(oc.get("date_only"))
    times = [x for x in cols if any(k in x["type"].lower() for k in ("timestamp", "datetime", "date"))]
    if not times:
        return None, False
    best = sorted(times, key=lambda x: next((i for i, h in enumerate(TIME_HINT) if h in x["name"].lower()), len(TIME_HINT)))[0]
    return best["name"], best["type"].lower() == "date"


def _group_columns(cols: list, skip: set, most: int = 3) -> list:
    """Columns that split rows into a few groups (source system, region, channel, batch ...), by their names."""
    texty = [x for x in cols if x["name"] not in skip and any(k in x["type"].lower() for k in ("char", "text", "string"))]
    out = []
    for h in GROUP_HINT:
        for x in texty:
            n = x["name"].lower()
            if x["name"] not in out and (n == h or n.startswith(h + "_") or n.endswith("_" + h) or (h in n and len(n) <= len(h) + 6)):
                out.append(x["name"])
    return out[:most]


def _best(results: list, spread_text: str) -> dict | None:
    """Of the same question asked per group column, the answer that points at one group; else one 'spread' answer."""
    results = [r for r in results if r]
    if not results:
        return None
    best = max(results, key=lambda r: r["_score"])
    if best["_score"] > 0:
        return {k: v for k, v in best.items() if not k.startswith("_")}
    names = ", ".join(r["_column"] for r in results)
    return {**{k: v for k, v in results[0].items() if not k.startswith("_")}, "text": spread_text.format(names=names)}


def _step(title: str, fn) -> dict | None:
    """One follow-up: what was asked, what was found (ok: True fact, False a problem found elsewhere, None not answered)."""
    try:
        r = fn()
    except Exception as e:                                            # noqa: BLE001  evidence, never a failure of the page
        msg = str(e).strip().splitlines()[0] if str(e).strip() else type(e).__name__
        return {"title": title, "ok": None, "text": f"Could not be answered: {msg[:200]}", "source": "", "sql": None}
    return {"title": title, **r} if r else None


# ============================================================================ step 1: the plan per kind of rule
def _per_day(t, d, tbl, col, extra_select: str = "", days: int = DAYS) -> tuple[str, dict]:
    q = d.ident(col)
    sql = (f"SELECT CAST({q} AS DATE) AS day, COUNT(*) AS n{extra_select} FROM {tbl} "
           f"WHERE {d.since(col, days * 24)} GROUP BY CAST({q} AS DATE)")
    out = {}
    for r in _q(t, sql, max_rows=days + 40):
        day = _day(r["day"])
        if day and day <= date.today():
            out[day] = r
    return sql, out


def _volume_trend(t, d, tbl, col, load_day):
    sql, per = _per_day(t, d, tbl, col, days=28)
    if not per:
        return {"ok": True, "text": f"No rows at all in the last 28 days ({col}).", "source": tbl, "sql": sql}
    days = sorted(per)
    target = load_day if load_day in per else days[-1]
    n = int(per[target]["n"])
    earlier = [int(per[x]["n"]) for x in days if x < target]
    usual = statistics.median(earlier) if len(earlier) >= 3 else None
    rows = [{"day": x.isoformat(), "rows": int(per[x]["n"])} for x in days[-10:]]
    if not usual:
        return {"ok": True, "text": f"The load of {target} has {_n(n)} rows; too few earlier loads to know the usual size.",
                "source": tbl, "sql": sql, "rows": rows}
    low = [x for x in days if x <= target and int(per[x]["n"]) < usual * 0.5]
    start = target
    for x in reversed([x for x in days if x <= target]):
        if int(per[x]["n"]) < usual * 0.5:
            start = x
        else:
            break
    if n >= usual * 0.5:
        text = f"Rows per load over 28 days: usually about {usual:,.0f}; the load of {target} has {_n(n)}."
    elif start == target:
        text = (f"Rows per load over 28 days: usually about {usual:,.0f}. The drop is new: only the load of {target} is low "
                f"({_n(n)} rows); the loads before it were normal.")
    else:
        text = (f"Rows per load over 28 days: usually about {usual:,.0f}. Low since {start} ({len([x for x in low if x >= start])} "
                f"load(s) in a row), {_n(n)} rows on {target}: this has been going on, not a one-off.")
    return {"ok": True, "text": text, "source": tbl, "sql": sql, "rows": rows, "_days": days, "_target": target}


def _hourly(t, d, tbl, col, day: date):
    q = d.ident(col)
    week = day - timedelta(days=7)
    sql = (f"SELECT CAST({q} AS DATE) AS day, {d.hour(col)} AS hr, COUNT(*) AS n FROM {tbl} "
           f"WHERE CAST({q} AS DATE) IN ({d.param}, {d.param}) GROUP BY CAST({q} AS DATE), {d.hour(col)}")
    got = defaultdict(dict)
    for r in _q(t, sql, (day, week), max_rows=60):
        got[_day(r["day"])][int(r["hr"])] = int(r["n"])

    def say(x):
        h = got.get(x) or {}
        return (f"{x}: {_n(sum(h.values()))} rows in {len(h)} hour(s), last at {max(h):02d}:00" if h else f"{x}: no rows")
    a, b = got.get(day) or {}, got.get(week) or {}
    if not b:
        return {"ok": True, "text": f"Hour by hour: {say(day)}; the same day a week before had no rows to compare.", "source": tbl, "sql": sql}
    stopped = a and max(a) < max(b) - 1
    missing = sorted(set(b) - set(a))
    text = f"Hour by hour: {say(day)}; a week before, {say(week)}."
    if not a:
        text += " Nothing arrived at all that day."
    elif stopped:
        text += f" The load stopped early: nothing after {max(a):02d}:00, while a week before rows kept coming until {max(b):02d}:00."
    elif missing:
        text += f" Hours with no rows this time: {', '.join(f'{h:02d}:00' for h in missing[:6])}{' …' if len(missing) > 6 else ''}."
    else:
        text += " Rows came in the same hours, just fewer of them: the source sent less, the load did not stop."
    rows = [{"hour": f"{h:02d}:00", str(day): a.get(h, 0), str(week): b.get(h, 0)} for h in sorted(set(a) | set(b))]
    return {"ok": True, "text": text, "source": tbl, "sql": sql, "rows": rows}


def _volume_groups(t, d, tbl, col, grp, day: date, before: date):
    q, g = d.ident(col), d.ident(grp)
    sql = (f"SELECT {g} AS grp, CAST({q} AS DATE) AS day, COUNT(*) AS n FROM {tbl} "
           f"WHERE CAST({q} AS DATE) IN ({d.param}, {d.param}) GROUP BY {g}, CAST({q} AS DATE)")
    rows = _q(t, sql, (day, before), max_rows=401)
    if len(rows) > 400:
        return None                                                   # too many values to be a grouping
    now, prev = defaultdict(int), defaultdict(int)
    for r in rows:
        (now if _day(r["day"]) == day else prev)[_plain(r["grp"])] += int(r["n"])
    groups = set(now) | set(prev)
    if len(groups) < 2:
        return None
    change = sorted(((now[k] - prev[k], k) for k in groups), key=lambda x: x[0])
    lost = -sum(x for x, _ in change if x < 0)
    worst_drop, worst = change[0]
    table = [{grp: k, str(before): prev[k], str(day): now[k]} for _, k in change[:8]]
    gone = [k for k in groups if prev[k] > 0 and now[k] == 0]
    score = 0
    if gone:
        names = ", ".join(f"{grp} = {k} ({_n(prev[k])} rows on {before})" for k in sorted(gone, key=lambda k: -prev[k])[:3])
        rest = [k for k in groups if k not in gone]
        text = f"By {grp}: nothing at all arrived on {day} from {names}."
        if rest:
            fewer = sum(now[k] for k in rest) < 0.8 * sum(prev[k] for k in rest)
            text += (" The other values sent fewer rows too: two problems at once." if fewer
                     else " The other values sent about as usual: look at that sender first.")
        score = 3
    elif lost and -worst_drop >= 0.6 * lost:
        text = (f"By {grp}: the drop is in {grp} = {worst}: {_n(now[worst])} rows on {day} against {_n(prev[worst])} on "
                f"{before}. The other values are about as usual.")
        score = 2
    else:
        text = f"By {grp}: the drop is spread over many {grp} values, not one: the whole source sent less."
    return {"ok": True, "text": text, "source": tbl, "sql": sql, "rows": table, "_score": score, "_column": grp}


def _freshness_days(t, d, tbl, col, date_only):
    from checks import engine
    sql, per = _per_day(t, d, tbl, col, days=60)
    days = sorted(per)
    if not days:
        return {"ok": True, "text": f"No rows at all in the last 60 days ({col}): the table is not being loaded.", "source": tbl, "sql": sql}
    learned = engine.rhythm(days, date_only)
    last = days[-1]
    since = (date.today() - last).days
    before = ", ".join(f"{x} ({_n(per[x]['n'])})" for x in reversed(days[-5:-1]))
    how = f" It usually loads {learned['pattern']}." if learned else ""
    text = (f"Last rows arrived on {last} ({_n(per[last]['n'])} rows), {since} day(s) ago.{how}"
            + (f" Loads before that: {before}." if before else ""))
    if learned and since > learned["longest_gap_days"]:
        text += f" The gap is longer than any gap in the last 60 days ({learned['longest_gap_days']} day(s)): loads stopped."
    rows = [{"day": x.isoformat(), "rows": int(per[x]["n"])} for x in days[-10:]]
    return {"ok": True, "text": text, "source": tbl, "sql": sql, "rows": rows}


def _empties_per_day(t, d, tbl, col, tc):
    sql, per = _per_day(t, d, tbl, tc, extra_select=f", COUNT({d.ident(col)}) AS filled")
    days = sorted(per)
    if not days:
        return None
    empty = {x: int(per[x]["n"]) - int(per[x]["filled"]) for x in days}
    rows = [{"day": x.isoformat(), "rows": int(per[x]["n"]), f"empty {col}": empty[x]} for x in days[-10:]]
    bad = [x for x in days if empty[x]]
    if not bad:
        return {"ok": True, "text": f"No empty {col} in rows of the last {DAYS} days (by {tc}): the empties are older rows.",
                "source": tbl, "sql": sql, "rows": rows}
    clean_before = [x for x in days if x < bad[0]]
    if clean_before and all(empty[x] for x in days if x >= bad[0]):
        text = (f"Empty {col} started on {bad[0]} (by {tc}): none in the {len(clean_before)} day(s) before, "
                f"and every day since has some ({_n(sum(empty.values()))} in all).")
    elif len(bad) == 1:
        text = f"Empty {col} only on {bad[0]} ({_n(empty[bad[0]])} rows, by {tc}): one load, not a lasting change."
    elif len(bad) == len(days):
        text = f"Empty {col} on every day of the last {DAYS} (by {tc}): a long-standing gap in the source, not a new break."
    else:
        text = f"Empty {col} on {len(bad)} of the last {len(days)} load days (by {tc}), starting {bad[0]}: it comes and goes."
    return {"ok": True, "text": text, "source": tbl, "sql": sql, "rows": rows}


def _empties_by_group(t, d, tbl, col, grp, tc):
    where = f" WHERE {d.since(tc, DAYS * 24)}" if tc else ""
    sql = (f"SELECT {d.ident(grp)} AS grp, COUNT(*) AS n, COUNT(*) - COUNT({d.ident(col)}) AS empty FROM {tbl}{where} "
           f"GROUP BY {d.ident(grp)}")
    rows = _q(t, sql, max_rows=201)
    if len(rows) > 200 or len(rows) < 2:
        return None
    total = sum(int(r["empty"]) for r in rows)
    if not total:
        return None
    rows.sort(key=lambda r: -int(r["empty"]))
    top = rows[0]
    share = 100 * int(top["empty"]) / total
    span = f" in the last {DAYS} days" if tc else ""
    table = [{grp: _plain(r["grp"]), "rows": int(r["n"]), f"empty {col}": int(r["empty"])} for r in rows[:8]]
    if share >= 60:
        text = (f"By {grp}: {share:.0f}% of the empty {col}{span} come from {grp} = {_plain(top['grp'])} "
                f"({_n(top['empty'])} of {_n(total)}): look at that sender first.")
    else:
        text = f"By {grp}: the empty {col}{span} come from many {grp} values, not one sender."
    return {"ok": True, "text": text, "source": tbl, "sql": sql, "rows": table, "_score": share if share >= 60 else 0, "_column": grp}


def _duplicates_when(t, d, tbl, keys, tc):
    k = ", ".join(d.ident(x) for x in keys)
    sql = (f"SELECT {k}, COUNT(*) AS copies, COUNT(DISTINCT CAST({d.ident(tc)} AS DATE)) AS days, "
           f"MIN({d.ident(tc)}) AS first_seen, MAX({d.ident(tc)}) AS last_seen FROM {tbl} GROUP BY {k} HAVING COUNT(*) > 1")
    rows = _q(t, sql, max_rows=500)
    if not rows:
        return None
    same = [r for r in rows if int(r["days"]) <= 1]
    later = [r for r in rows if int(r["days"]) > 1]
    first = min((_day(r["last_seen"]) for r in rows if r["last_seen"] is not None), default=None)
    label = " + ".join(keys)
    more = " (first 500 looked at)" if len(rows) == 500 else ""
    if len(same) >= 0.8 * len(rows):
        text = (f"{_n(len(rows))} duplicated {label}{more}: {_n(len(same))} were copied inside one day's load. A load ran twice, "
                "or the source sent the same rows twice.")
    elif len(later) >= 0.8 * len(rows):
        text = (f"{_n(len(rows))} duplicated {label}{more}: {_n(len(later))} came again on a later day. The load inserts rows "
                "it should update (no upsert on the key).")
    else:
        text = (f"{_n(len(rows))} duplicated {label}{more}: {_n(len(same))} inside one load, {_n(len(later))} again on a later day: "
                "both a repeated load and missing upserts.")
    if first:
        text += f" The earliest second copy is from {first} (by {tc})."
    table = [{**{x: _plain(r[x]) for x in keys}, "copies": int(r["copies"]), "days": int(r["days"]),
              "first seen": _plain(r["first_seen"]), "last seen": _plain(r["last_seen"])} for r in rows[:8]]
    return {"ok": True, "text": text, "source": tbl, "sql": sql, "rows": table}


def _check_plan(t: dict) -> list:
    chk, last = t["check"], t["last"]
    kind = chk["check_type"]
    if not chk.get("table_name"):
        return []
    d = _d(t)
    tbl = d.table(chk["schema_name"], chk["table_name"])
    cols = _columns(t, chk["schema_name"], chk["table_name"])
    cfg = chk.get("config") or {}
    tc, date_only = _time_column(t, cols)
    ctype = {x["name"]: x["type"].lower() for x in cols}
    steps = []
    if kind == "volume" and tc:
        load_day = _day((last.get("details") or {}).get("load_day")) or None
        trend = {}

        def vt():
            trend.update(_volume_trend(t, d, tbl, tc, load_day) or {})
            return {k: v for k, v in trend.items() if not k.startswith("_")}
        steps.append(_step("When did the drop start?", vt))
        target = trend.get("_target") or load_day
        if target and not date_only and "date" != ctype.get(tc):
            steps.append(_step("Did the load stop part way?", lambda: _hourly(t, d, tbl, tc, target)))
        grps = _group_columns(cols, {tc})
        earlier = [x for x in trend.get("_days") or [] if target and x < target]
        if grps and target and earlier:
            steps.append(_step(f"Which {' / '.join(grps)} lost rows?", lambda: _best(
                [_volume_groups(t, d, tbl, tc, g, target, earlier[-1]) for g in grps],
                "By {names}: the drop is spread over every value, not one sender: the whole source sent less.")))
    elif kind == "freshness" and tc:
        steps.append(_step("When did loads stop?", lambda: _freshness_days(t, d, tbl, tc, date_only)))
    elif kind == "null_rate":
        col = cfg.get("column")
        if tc and col:
            steps.append(_step(f"When did empty {col} start?", lambda: _empties_per_day(t, d, tbl, col, tc)))
        grps = _group_columns(cols, {col, tc})
        if grps and col:
            steps.append(_step(f"Which {' / '.join(grps)} sends the empty {col}?", lambda: _best(
                [_empties_by_group(t, d, tbl, col, g, tc) for g in grps],
                "By {names}: the empty " + col + " come from many values, not one sender.")))
    elif kind == "uniqueness" and tc and cfg.get("columns") and tc not in cfg["columns"]:
        steps.append(_step("One load twice, or the same key again later?", lambda: _duplicates_when(t, d, tbl, cfg["columns"], tc)))
    plain = f"{chk['schema_name']}.{chk['table_name']}"
    steps = [{**x, "source": plain if x.get("source") == tbl else x.get("source")} if x else x for x in steps]
    conn = t["conn"]
    system = conn["source_type"]
    steps += _lineage_steps(system, conn["host"], chk.get("database") or conn["database"], chk["schema_name"], chk["table_name"],
                            readers=kind == "schema")
    return steps


# ---------------------------------------------------------------- POC pipeline incidents
def _poc_plan(t: dict) -> list:
    import data_service as ds
    inc = t["incident"]
    m = inc.get("metrics") or {}
    if inc["type"] == "REJECTED_ROWS":
        entity, rules, day = m.get("entity"), [m.get("rule_name")], _day(m.get("business_date"))
    elif inc["type"] == "SETTLEMENT_MISMATCH":
        entity, day = "payments", _day(inc.get("business_date"))
        rules = [x.strip() for x in str((inc.get("rejected_explained") or {}).get("rules") or "").split(",") if x.strip()]
    else:                                                             # data loss, duplicates, schema change: the map only
        entity, rules, day = m.get("entity"), [], None
        if not entity and "-" in inc["id"]:
            entity = inc["id"].split("-", 1)[1].split("-")[0]
        if entity not in ds.ENTITIES:
            entity = None
    layer = "bronze" if inc["type"] in ("DATA_LOSS", "DUPLICATES") else "silver"
    steps = []
    if entity and rules and day:
        label = " / ".join(rules)

        def history():
            sql = ("SELECT business_date, COUNT(*) AS n FROM silver.rejected_records WHERE entity = %s AND rule_name = ANY(%s) "
                   "AND business_date BETWEEN %s AND %s GROUP BY business_date ORDER BY business_date")
            rows = _q(t, sql, (entity, rules, day - timedelta(days=30), day))
            other = [r for r in rows if _day(r["business_date"]) != day]
            table = [{"business date": _plain(r["business_date"]), "rejected": int(r["n"])} for r in rows[-10:]]
            if not other:
                text = f"New: {label} rejected no {entity} rows in the 30 business dates before {day}. Something changed in this delivery."
            else:
                dates = ", ".join(f"{_plain(r['business_date'])} ({_n(r['n'])})" for r in other[-4:])
                text = (f"Not new: {label} also rejected {entity} rows on {len(other)} of the 30 business dates before {day}, "
                        f"latest {dates}. The source keeps sending such rows: fix it at the source, not row by row.")
            return {"ok": True, "text": text, "source": "silver.rejected_records", "sql": sql, "rows": table}
        steps.append(_step("Is this new?", history))

        def batches():
            sql = ("SELECT batch_id, COUNT(*) AS n FROM silver.rejected_records WHERE entity = %s AND rule_name = ANY(%s) "
                   "AND business_date = %s GROUP BY batch_id")
            rows = _q(t, sql, (entity, rules, day))
            if not rows:
                return None
            bronze = (ds.ENTITIES.get(entity) or (None, f"bronze.{entity}"))[1]
            ids = [r["batch_id"] for r in rows if r["batch_id"] is not None]
            files = {}
            if ids and re.fullmatch(r"[a-z_]+\.[a-z_]+", bronze or ""):
                sql2 = (f"SELECT batch_id, MIN(source_file) AS source_file, COUNT(*) AS n FROM {bronze} "
                        "WHERE batch_id = ANY(%s) GROUP BY batch_id")
                files = {r["batch_id"]: r for r in _q(t, sql2, ([str(i) for i in ids],))}
                sql += ";\n" + sql2
            table = [{"batch": _plain(r["batch_id"]), "rejected": int(r["n"]), "file": (files.get(r["batch_id"]) or {}).get("source_file"),
                      "rows in batch": (files.get(r["batch_id"]) or {}).get("n")} for r in rows]
            top = max(rows, key=lambda r: int(r["n"]))
            f = files.get(top["batch_id"]) or {}
            where = f" (file {f['source_file']}, {_n(f['n'])} rows in that batch)" if f.get("source_file") else ""
            if len(rows) == 1:
                n = int(top["n"])
                lead = "It came" if n == 1 else "Both came" if n == 2 else f"All {_n(n)} came"
                text = f"{lead} in one batch, {_plain(top['batch_id'])}{where}: send that file back to its owner."
            else:
                text = f"They came in {len(rows)} batches; most ({_n(top['n'])}) in {_plain(top['batch_id'])}{where}."
            return {"ok": True, "text": text, "source": f"silver.rejected_records / {bronze}", "sql": sql, "rows": table}
        steps.append(_step("Which batch and file sent them?", batches))

        def same_day():
            sql = ("SELECT entity, rule_name, COUNT(*) AS n FROM silver.rejected_records WHERE business_date = %s "
                   "GROUP BY entity, rule_name ORDER BY n DESC")
            rows = _q(t, sql, (day,))
            others = [r for r in rows if not (r["entity"] == entity and r["rule_name"] in rules)]
            table = [{"entity": r["entity"], "rule": r["rule_name"], "rejected": int(r["n"])} for r in rows[:8]]
            if not others:
                text = f"Only {label} on {entity} rejected rows on {day}: the problem is in this one field, not the whole delivery."
                return {"ok": True, "text": text, "source": "silver.rejected_records", "sql": sql, "rows": table}
            names = ", ".join(f"{r['entity']} {r['rule_name']} ({_n(r['n'])})" for r in others[:4])
            text = f"Other rules also rejected rows on {day}: {names}. The whole delivery for that day may be off: check with the source."
            return {"ok": False, "text": text, "source": "silver.rejected_records", "sql": sql, "rows": table}
        steps.append(_step("Did other rules fail that day?", same_day))
    conn = t["conn"]
    if entity:
        steps += _lineage_steps("postgresql", conn["host"], conn["database"], layer, entity)
    return steps


# ---------------------------------------------------------------- the lineage map: upstream, the pipeline that loads it, readers
def _lineage_graph():
    from lineage import graph
    import scopes
    if not appdb.table_exists("app.lineage_snapshots"):
        return None
    snaps = appdb.rows("SELECT scope, name, built_at, seconds, ok, error, graph FROM app.lineage_snapshots WHERE graph IS NOT NULL")
    if not snaps:
        return None
    all_scopes = scopes.list_scopes()
    nodes, edges = graph.merge(snaps)
    graph._attach_owner_by_location(nodes, all_scopes)
    return nodes, edges, all_scopes


def neighbours(system, host, database, schema, table) -> dict | None:
    """From the lineage map: the datasets this table is loaded from (2 steps up), the jobs that write it, and who reads it.
    None when there is no map yet; known=False when the table is not on it."""
    from lineage import graph, model
    g = _lineage_graph()
    if g is None:
        return None
    nodes, edges, all_scopes = g
    nid = model.dataset_id(system, host, database, schema, table)
    if nid not in nodes:
        return {"known": False}
    into, out = defaultdict(list), defaultdict(list)
    for e in edges:
        if e["kind"] == "flow":
            into[e["to"]].append(e["from"])
            out[e["from"]].append(e["to"])
    data = ("table", "view", "file")
    writers, inputs, seen, frontier = [], [], {nid}, [nid]
    for hop in (1, 2):
        nxt = []
        for cur in frontier:
            for p in into[cur]:
                if p in seen or nodes[p].get("control"):
                    continue
                seen.add(p)
                if nodes[p]["kind"] in data:
                    inputs.append(p)
                    nxt.append(p)
                    continue
                if hop == 1:
                    writers.append(p)
                for q in into[p]:
                    if q not in seen and nodes[q]["kind"] in data and not nodes[q].get("control"):
                        seen.add(q)
                        inputs.append(q)
                        nxt.append(q)
        frontier = nxt
    readers = [x for x in out[nid] if not nodes[x].get("control")]
    sub = {k: dict(nodes[k]) for k in set(writers + inputs + readers)}
    graph.health(sub, all_scopes)
    return {"known": True, "writers": [sub[x] for x in writers], "inputs": [sub[x] for x in inputs], "readers": [sub[x] for x in readers]}


def _problem(n: dict) -> str | None:
    if n.get("incidents"):
        return n["incidents"][0]["title"]
    bad = [c for c in n.get("checks") or [] if c["status"] in ("FAIL", "ERROR", "WARN")]
    return f"rule '{bad[0]['name']}' {bad[0]['status'].lower()}" if bad else None


def _lineage_steps(system, host, database, schema, table, readers: bool = False) -> list:
    name = f"{schema}.{table}"
    try:
        nb = neighbours(system, host, database, schema, table)
    except Exception as e:                                            # noqa: BLE001
        return [{"title": "Does the problem start upstream?", "ok": None, "text": f"The lineage map could not be read: {str(e)[:160]}",
                 "source": "Lineage map", "sql": None}]
    if nb is None:
        return [{"title": "Does the problem start upstream?", "ok": None,
                 "text": f"The lineage map has not been read yet, so what feeds {name} is unknown. Open Explain → Lineage map once.",
                 "source": "Lineage map", "sql": None}]
    if not nb.get("known"):
        return [{"title": "Does the problem start upstream?", "ok": None,
                 "text": f"Nothing on the lineage map loads {name}: it is loaded by a tool or job DataTrust does not read "
                         "(connect that tool, e.g. its Data Factory, to see it here).", "source": "Lineage map", "sql": None}]
    steps = []
    ins = nb["inputs"]
    if ins:
        hurt = [(n, _problem(n)) for n in ins if _problem(n)]
        unwatched = [n["label"] for n in ins if not n.get("checks") and n["kind"] != "file"]
        if hurt:
            first = "; ".join(f"{n['label']}: {p}" for n, p in hurt[:3])
            text = f"Yes, upstream has a problem too: {first}. Fix it there first; {name} only shows what it was given."
            ok = False
        else:
            text = (f"{name} is loaded from {', '.join(n['label'] for n in ins[:5])}{' …' if len(ins) > 5 else ''}: "
                    f"{'it has no' if len(ins) == 1 else 'none of them has a'} failing rule or open incident, so the problem most "
                    "likely starts at this table or its load.")
            if unwatched:
                text += f" Not watched by any rule: {', '.join(unwatched[:4])}."
            ok = True
        steps.append({"title": "Does the problem start upstream?", "ok": ok, "text": text, "source": "Lineage map + rules and incidents",
                      "sql": None, "rows": [{"upstream": n["label"], "kind": n["kind"], "source": n.get("source_name") or n.get("system"),
                                             "status": _problem(n) or ("watched, OK" if n.get("checks") else "not watched")} for n in ins[:10]]})
    else:
        steps.append({"title": "Does the problem start upstream?", "ok": None, "source": "Lineage map", "sql": None,
                      "text": f"The lineage map knows nothing that loads {name} (it may be loaded by a tool DataTrust does not read)."})
    for w in nb["writers"][:2]:
        steps.append(_writer_step(w, name))
    if readers:
        rd = nb["readers"]
        steps.append({"title": f"Who reads {name}?", "ok": False if rd else True, "source": "Lineage map", "sql": None,
                      "text": (f"{len(rd)} thing(s) read {name} and may break on the change: "
                               f"{', '.join(r['kind'] + ' ' + r['label'] for r in rd[:6])}{' …' if len(rd) > 6 else ''}."
                               if rd else f"Nothing on the lineage map reads {name}.")})
    return [s for s in steps if s]


def _writer_step(w: dict, name: str) -> dict:
    title = "Did the job that loads it run?"
    nid = w["id"]
    if nid.startswith("job:adf:"):
        _, _, sid, pipeline = nid.split(":", 3)
        try:
            from adf import insights
            runs = insights.runs(source_id=int(sid), pipeline=w["label"], days=7, limit=5)
        except Exception as e:                                        # noqa: BLE001
            return {"title": title, "ok": None, "text": f"Pipeline {w['label']} loads {name}; its runs could not be read: {str(e)[:120]}",
                    "source": "Azure Data Factory runs", "sql": None}
        if not runs:
            return {"title": title, "ok": False, "source": "Azure Data Factory runs", "sql": None,
                    "text": f"Pipeline {w['label']} loads {name} but has not run in the last 7 days."}
        r = runs[0]
        when = str(r.get("run_start") or "")[:16]
        fails = sum(1 for x in runs if x["status"] == "Failed")
        if r["status"] == "Failed":
            text = f"Pipeline {w['label']} loads {name}; its last run ({when}) failed" + (f": {r['cause']}" if r.get("cause") else "") + \
                   (f" - {r['message']}" if r.get("message") else "") + ". Start there: open it on ADF runs."
            ok = False
        else:
            text = f"Pipeline {w['label']} loads {name}; its last run ({when}) {str(r['status']).lower()}" + \
                   (f", {_n(r['rows_written'])} rows written" if r.get("rows_written") is not None else "") + \
                   (f". {fails} of its last {len(runs)} runs failed." if fails else ".")
            ok = True
        return {"title": title, "ok": ok, "text": text, "source": "Azure Data Factory runs", "sql": None,
                "rows": [{"run start": str(x.get("run_start") or "")[:16], "status": x["status"],
                          "rows written": x.get("rows_written"), "cause": x.get("cause")} for x in runs]}
    p = _problem(w)
    return {"title": title, "ok": False if p else True, "source": "Lineage map + incidents", "sql": None,
            "text": f"{w['kind'].capitalize()} {w['label']} loads {name}" + (f"; it has a problem: {p}." if p else "; no open incident about it.")}


# ============================================================================ step 1 entry point
def plan(incident_id: str, scope: str | None = None, refresh: bool = False) -> dict:
    key = f"{scope or ''}|{incident_id}"
    with _lock:
        hit = _cache.get(key)
    if hit and not refresh and time.time() - hit[0] < CACHE_SECONDS:
        return hit[1]
    t0 = time.time()
    t = _target(incident_id, scope)
    if t["kind"] == "gone":
        return {"found": False, "steps": []}
    if t["kind"] == "check":
        steps = _check_plan(t)
    elif t["kind"] == "poc":
        steps = _poc_plan(t)
    else:
        return {"found": True, "steps": [], "note": "Follow-up checks need a database: this incident comes from a Data Factory "
                "(its run, error and cause are above)." if t["kind"] == "adf" else "No follow-up checks for this kind of incident."}
    out = {"found": True, "steps": [s for s in steps if s], "seconds": round(time.time() - t0, 1),
           "ran_at": datetime.now().isoformat(timespec="seconds")}
    with _lock:
        _cache[key] = (time.time(), out)
    return out


def cached(incident_id: str) -> list:
    """The follow-up findings already worked out for an incident (any scope), for the AI explanation. Nothing is run."""
    with _lock:
        hits = [v for k, v in _cache.items() if k.endswith("|" + incident_id) and time.time() - v[0] < CACHE_SECONDS]
    return [{"question": s["title"], "finding": s["text"], "source": s.get("source")} for s in (hits[0][1]["steps"] if hits else [])]


# ============================================================================ step 2: dig deeper with AI
_DATEISH = re.compile(r"^\d{4}-\d{2}-\d{2}([ T]\d{2}:\d{2}(:\d{2}(\.\d+)?)?([+-]\d{2}:?\d{2}|Z)?)?$")
SAFE_CALLS = {"date_trunc", "to_char", "to_date", "date_part", "extract", "coalesce", "nullif", "length", "len", "lower", "upper",
              "trim", "ltrim", "rtrim", "left", "right", "substring", "substr", "split_part", "position", "strpos", "charindex",
              "regexp_replace", "regexp_like", "dateadd", "datediff", "datepart", "datename", "eomonth", "getdate", "sysdatetime",
              "current_timestamp", "now", "convert", "try_cast", "try_convert", "iff", "iif", "isnull", "nvl", "format", "round",
              "abs", "floor", "ceiling", "ceil", "percentile_cont", "median", "approx_count_distinct", "count_big", "string_agg",
              "listagg", "hour", "minute", "day", "month", "year", "dayofweek", "to_timestamp", "date"}


class Masker:
    """Text values -> [v1], [v2] ... (the same value always gets the same token); numbers, dates and booleans stay."""

    def __init__(self):
        self.to_token: dict = {}

    def value(self, v):
        if v is None or isinstance(v, (bool, int, float)):
            return v
        s = str(v)
        if _DATEISH.match(s) or re.fullmatch(r"-?\d+(\.\d+)?", s):
            return s
        if s not in self.to_token:
            self.to_token[s] = f"[v{len(self.to_token) + 1}]"
        return self.to_token[s]

    def rows(self, rows: list) -> list:
        return [{k: self.value(v) for k, v in r.items()} for r in rows or []]

    def unmask(self, obj):
        back = {v: k for k, v in self.to_token.items()}
        if isinstance(obj, str):
            return re.sub(r"\[v\d+\]", lambda m: back.get(m.group(0), m.group(0)), obj)
        if isinstance(obj, list):
            return [self.unmask(x) for x in obj]
        if isinstance(obj, dict):
            return {k: self.unmask(v) for k, v in obj.items()}
        return obj


def _allowed_tables(t: dict) -> list:
    """The tables the AI may query: the incident's table, the tables it is loaded from on the same database, and for a
    POC incident the rejected rows and the entity's bronze and silver tables."""
    out = []
    if t["kind"] == "poc":
        import data_service as ds
        m = t["incident"].get("metrics") or {}
        entity = m.get("entity") or ("payments" if t["incident"]["type"] == "SETTLEMENT_MISMATCH" else None)
        out = [("silver", "rejected_records")]
        if entity:
            spec = ds.ENTITIES.get(entity) or ()
            out += [tuple(x.split(".", 1)) for x in spec[1:3] if isinstance(x, str) and "." in x] or [("bronze", entity), ("silver", entity)]
        conn, schema, table = t["conn"], "silver", entity
    else:
        chk = t["check"]
        if chk.get("table_name"):
            out.append((chk["schema_name"], chk["table_name"]))
        if chk["check_type"] in ("custom_sql",):
            from lineage import sql as lsql
            out += [x for x in lsql.analyze((chk.get("config") or {}).get("sql") or "", _sqlglot_dialect(t))["reads"] if x[0]]
        conn, schema, table = t["conn"], chk.get("schema_name"), chk.get("table_name")
    if table:
        try:
            nb = neighbours(conn["source_type"], conn["host"], t.get("database") or conn["database"], schema, table) or {}
            for n in nb.get("inputs") or []:
                if n["kind"] in ("table", "view") and n.get("scope") == f"db:{t['connection_id']}" and \
                        str(n.get("database") or "").lower() == str(t.get("database") or conn["database"]).lower():
                    out.append((n["schema"], n["name"]))
        except Exception:                                             # noqa: BLE001  the map is a bonus here
            pass
    seen, uniq = set(), []
    for s, n in out:
        k = (str(s).lower(), str(n).lower())
        if k not in seen:
            seen.add(k)
            uniq.append((s, n))
    return uniq[:6]


def _sqlglot_dialect(t: dict) -> str:
    from lineage import sql as lsql
    return lsql.DIALECT.get(t["conn"]["source_type"], "postgres")


def validate(sql: str, allowed: list, dialect: str) -> str:
    """One read-only SELECT on the allowed tables only, no writes, no user functions. Returns the cleaned SQL or raises."""
    from checks import dialects
    from lineage import sql as lsql
    text = dialects.clean_select(sql)
    a = lsql.analyze(text, dialect)
    if a["writes"]:
        raise ValueError("it writes to a table")
    names = {(s.lower(), n.lower()) for s, n in allowed}
    bare = {n for _, n in names}
    for s, n in a["reads"]:
        if (s and (s, n) not in names) or (not s and n not in bare):
            raise ValueError(f"it reads {(s + '.') if s else ''}{n}, which is not one of this incident's tables")
    for s, f in a["calls"]:
        if s or f not in SAFE_CALLS:
            raise ValueError(f"it calls {(s + '.') if s else ''}{f}(), which is not a plain built-in function")
    if not a["reads"]:
        raise ValueError("it reads no table")
    return text


def _context(t: dict, masker: Masker, steps: list) -> dict:
    """What the model is told: the incident, the rule, the tables it may query (columns only) and the follow-up findings,
    their result rows masked. No row of the data is sent unmasked."""
    inc = t["incident"]
    allowed = _allowed_tables(t)
    tables = []
    for s, n in allowed:
        try:
            cols = [{"name": c["name"], "type": c["type"]} for c in _columns(t, s, n)[:60]]
        except Exception:                                             # noqa: BLE001
            cols = []
        if cols:
            tables.append({"table": f"{s}.{n}", "columns": cols})
    rule = None
    if t["kind"] == "check":
        chk, last = t["check"], t["last"]
        rule = {"type": chk["check_type"], "name": chk["name"], "config": {k: v for k, v in (chk.get("config") or {}).items() if k != "sql"},
                "expected": last.get("expected"), "result": last.get("message"), "observed": last.get("observed")}
    metrics = {k: v for k, v in (inc.get("metrics") or {}).items() if k not in ("evidence",)}
    return {"database_type": t["conn"]["source_type"], "today": date.today().isoformat(),
            "incident": {"title": inc["title"], "type": inc["type"], "business_date": inc.get("business_date"),
                         "metrics": {k: masker.value(v) if isinstance(v, str) and k not in ("entity", "rule_name", "check_type", "status") else v
                                     for k, v in metrics.items()}},
            "rule": rule, "tables": tables,
            "follow_ups_already_run": [{"question": s["title"], "result_rows": masker.rows(s.get("rows") or [])[:12]} for s in steps]}


PLAN_SYSTEM = (
    "You are DataTrust AI, investigating why a data incident happened. Fixed follow-up checks already ran (follow_ups_already_run). "
    f"Propose at most {MAX_QUERIES} NEW read-only SQL queries that would most help find the cause, beyond what those checks answered. "
    "Rules: one SELECT (or WITH ... SELECT) each, in the given database_type's SQL dialect; use ONLY the tables and columns listed "
    "(schema-qualified names); aggregate (COUNT, MIN, MAX, GROUP BY by day or by a category) so each query returns at most "
    f"{ROWS} rows; never SELECT * and never list personal or free-text columns row by row; filter to recent data when a time column "
    "exists; no functions you are not sure exist in that dialect. Text values in the evidence appear as tokens like [v1]: you may "
    "not put a token in SQL. Return JSON {\"queries\": [{\"question\": what it answers, in plain words, \"why\": one sentence, "
    "\"sql\": the query}]}. An empty list is fine when nothing useful is left to ask.")
READ_SYSTEM = (
    "You are DataTrust AI. You proposed queries to find why a data incident happened; here are their results. Text values appear "
    "as tokens like [v1]: use the tokens as they are, never guess what they stand for. Use ONLY these results and the fixed "
    "follow-up findings; copy numbers exactly; say plainly when the results do not show the cause. Return JSON {\"findings\": "
    "[{\"question\": as asked, \"answer\": one or two sentences}], \"conclusion\": 2-3 sentences on what the results show about "
    "the cause, \"confidence\": \"high\"|\"medium\"|\"low\", \"next_steps\": [1-3 concrete steps]}. Confidence is high only when a "
    "result shows the cause directly.")


def dig(incident_id: str, scope: str | None = None, refresh: bool = False, cached_only: bool = False) -> dict:
    """Step 2. cached_only: show the last answer for this evidence without asking the model (the page's first load)."""
    import llm
    t = _target(incident_id, scope)
    if t["kind"] not in ("check", "poc"):
        return {"available": False, "reason": "AI follow-up queries need a database to ask: this incident comes from a Data Factory."
                if t["kind"] == "adf" else "AI follow-up queries are not available for this incident."}
    if t["kind"] == "check" and not t["check"].get("table_name") and t["check"]["check_type"] != "custom_sql":
        return {"available": False, "reason": "This rule is not about one table, so there is nothing to query."}
    if llm.provider() == "none":
        return {"available": False, "reason": "No AI model is configured (LLM_PROVIDER in .env)."}
    masker = Masker()
    steps = plan(incident_id, scope).get("steps") or []
    ctx = _context(t, masker, steps)
    if not ctx["tables"]:
        return {"available": False, "reason": "The incident's tables could not be read, so there is nothing the AI could query."}
    prompt = "Evidence (JSON):\n" + json.dumps(ctx, default=str)[:20000]
    if cached_only and not refresh:
        proposed = llm.cached("dig-plan", incident_id, ctx)
        if not proposed:
            return {"available": True, "not_generated": True}
    else:
        try:
            proposed = llm.grounded("dig-plan", incident_id, ctx, PLAN_SYSTEM, prompt, refresh=refresh)
        except llm.Unavailable as e:
            return {"available": False, "reason": str(e)}
    allowed = _allowed_tables(t)
    dia = _sqlglot_dialect(t)
    d = _d(t)
    ran = []
    for q in (proposed.get("queries") or [])[:MAX_QUERIES]:
        item = {"question": str(q.get("question") or "")[:300], "why": str(q.get("why") or "")[:300], "sql": str(q.get("sql") or "")}
        try:
            clean = validate(item["sql"], allowed, dia)
        except ValueError as e:
            ran.append({**item, "refused": f"Not run: {e}."})
            continue
        try:
            rows = _q(t, d.limit(clean, ROWS), max_rows=ROWS)
            item["rows"] = [{k: _plain(v) for k, v in r.items()} for r in rows]
        except Exception as e:                                        # noqa: BLE001
            msg = str(e).strip().splitlines()[0] if str(e).strip() else type(e).__name__
            item["error"] = f"The database refused it: {msg[:200]}"
        ran.append(item)
    out = {"available": True, "queries": ran, "tables": [f"{s}.{n}" for s, n in allowed], "model": proposed.get("model"),
           "generated_at": proposed.get("generated_at"), "cached": bool(proposed.get("cached"))}
    results = [{"question": x["question"], "sql": x["sql"], "rows": masker.rows(x.get("rows") or []),
                "error": x.get("error") or x.get("refused")} for x in ran]
    if not any(x.get("rows") is not None for x in ran):
        out["reading"] = None
        return out
    ev = {"incident": ctx["incident"], "rule": ctx["rule"], "follow_ups": [{"question": s["title"]} for s in steps], "results": results}
    rp = "Evidence (JSON):\n" + json.dumps(ev, default=str)[:24000]
    try:
        reading = (llm.cached("dig-read", incident_id, ev) if cached_only and not refresh
                   else llm.grounded("dig-read", incident_id, ev, READ_SYSTEM, rp, refresh=refresh))
    except llm.Unavailable as e:
        reading, out["reading_error"] = None, str(e)
    out["reading"] = masker.unmask({k: v for k, v in reading.items() if k not in ("generated_at",)}) if reading else None
    out["reading_stale"] = bool(cached_only and not refresh and not reading)    # the data moved since the model read it
    out["masked_values"] = len(masker.to_token)
    return out
