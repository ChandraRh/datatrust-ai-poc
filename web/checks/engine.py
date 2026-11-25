"""
Run one check against its database, read-only, and say what was found.

Result: status PASS / WARN / FAIL / ERROR, observed (one number), expected (in words), message (one sentence),
details (the evidence: sample rows, duplicate keys, mismatches, schema differences) and sql (exactly what ran).
Every number comes from a query; nothing is estimated.
"""
from __future__ import annotations

import os
import statistics
import time
from contextlib import closing
from datetime import date, datetime, timezone
from decimal import Decimal

from . import dialects, store

SAMPLE = 20
RECON_MAX_ROWS = 5000
OPS = {"<": lambda a, b: a < b, "<=": lambda a, b: a <= b, "=": lambda a, b: a == b, "!=": lambda a, b: a != b,
       ">=": lambda a, b: a >= b, ">": lambda a, b: a > b}


# ============================================================================ running SQL on a connection
def _source(connection_id: int):
    from estate import service
    return service.source(connection_id)


def _plain(v):
    if isinstance(v, Decimal):
        return float(v)
    if isinstance(v, (datetime, date)):
        return v.isoformat(sep=" ") if isinstance(v, datetime) else v.isoformat()
    if isinstance(v, (bytes, bytearray, memoryview)):
        return "<binary>"
    return v


QUERY_SECONDS = max(5, int(os.getenv("CHECKS_QUERY_SECONDS", "60")))   # one time limit for every query a rule runs


class TooSlow(ValueError):
    pass


def _too_slow() -> TooSlow:
    return TooSlow(f"the query took longer than {QUERY_SECONDS} s (the time limit for rules) and was stopped on the database. "
                   "Make it lighter: add a filter (where), count only recent rows (volume: last 24 h), or index the column it "
                   "reads. The limit is CHECKS_QUERY_SECONDS.")


def _is_timeout(e: Exception) -> bool:
    s = f"{type(e).__name__} {e}".lower()
    return ("querycanceled" in s or "statement timeout" in s or "hyt00" in s or "query timeout expired" in s
            or "000630" in s or "reached its statement or warehouse timeout" in s)


def query(connection_id: int, database: str | None, sql: str, params=(), max_rows: int = 1000) -> list:
    """Rows as dicts. PostgreSQL: a read-only session. SQL Server and Snowflake: inside a transaction that is always rolled back.
    Either way the query is stopped on the database after QUERY_SECONDS."""
    src = _source(connection_id)
    kind = src.cfg["source_type"]
    if kind == "postgresql":
        with closing(src.connect(database)) as c:                    # read_only=True: default_transaction_read_only
            c.execute(f"SET statement_timeout = '{QUERY_SECONDS}s'")
            try:
                cur = c.execute(sql, params or None)
            except Exception as e:                                   # noqa: BLE001
                raise _too_slow() if _is_timeout(e) else e
            return [dict(r) for r in cur.fetchmany(max_rows)] if cur.description else []
    if kind == "sqlserver":
        with closing(src.connect(database, read_only=False)) as c:  # autocommit off -> rollback below undoes anything
            c.timeout = QUERY_SECONDS
            try:
                cur = c.cursor()
                try:
                    cur.execute(sql, params or ())
                except Exception as e:                               # noqa: BLE001
                    raise _too_slow() if _is_timeout(e) else e
                while cur.description is None and cur.nextset():
                    pass
                if cur.description is None:
                    return []
                cols = [d[0] for d in cur.description]
                return [dict(zip(cols, r)) for r in cur.fetchmany(max_rows)]
            finally:
                c.rollback()
    if kind == "snowflake":
        import snowflake.connector
        from estate.snowflake_connector import lower_keys
        c = src.connect(database, read_only=False, seconds=QUERY_SECONDS)   # Snowflake stops the statement after QUERY_SECONDS
        try:
            cur = c.cursor(snowflake.connector.DictCursor)
            try:
                cur.execute(sql, params or None)
            except Exception as e:                                   # noqa: BLE001
                raise _too_slow() if _is_timeout(e) else e
            return [lower_keys(r) for r in cur.fetchmany(max_rows)] if cur.description else []
        finally:
            try:
                c.rollback()                                         # autocommit off: nothing a query did is kept
            finally:
                c.close()
    raise ValueError(f"checks are not available for {kind} yet")


def _dialect(connection_id: int):
    from estate import store as estate_store
    return dialects.for_type(estate_store.get_connection(connection_id)["source_type"])


def columns(connection_id: int, database: str | None, schema: str, table: str) -> list:
    d = _dialect(connection_id)
    return [{"name": r["column_name"], "type": r["data_type"], "nullable": str(r["is_nullable"]).upper() == "YES"}
            for r in query(connection_id, database, d.columns_sql(), (schema, table))]


# ============================================================================ the checks
def _hours_since(v) -> float | None:
    if v is None:
        return None
    if isinstance(v, str):
        v = datetime.fromisoformat(v)
    if isinstance(v, datetime):
        now = datetime.now(timezone.utc) if v.tzinfo else datetime.now()
        return (now - v).total_seconds() / 3600
    if isinstance(v, date):                                          # a date column: data for that whole day is in
        return (datetime.now() - datetime(v.year, v.month, v.day)).total_seconds() / 3600 - 24
    raise ValueError(f"column value {v!r} is not a date or time")


# ---------------------------------------------------------------- how often a table loads (freshness that learns)
RHYTHM_DAYS = 60            # history read to learn the rhythm
RELEARN_HOURS = 24          # an adaptive freshness rule re-learns once a day
MIN_LOAD_DAYS = 3           # fewer days with new rows: too little to know the rhythm


def _as_date(v) -> date | None:
    if v is None:
        return None
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    return datetime.fromisoformat(str(v)[:10]).date()


def load_days(connection_id: int, database: str | None, d, t: str, column: str) -> list:
    """The days of the last RHYTHM_DAYS that received rows (by the column's own value), oldest first. One grouped query."""
    q = d.ident(column)
    sql = f"SELECT CAST({q} AS DATE) AS day FROM {t} WHERE {d.since(column, RHYTHM_DAYS * 24)} GROUP BY CAST({q} AS DATE)"
    today = date.today()
    days = {_as_date(r["day"]) for r in query(connection_id, database, sql, max_rows=RHYTHM_DAYS + 10)}
    return sorted(x for x in days if x and x <= today)                    # a date in the future is not a load


def rhythm(days: list, date_only: bool = False) -> dict | None:
    """How often the table receives rows, from the days that did, and the freshness limit that fits it: the longest
    usual gap between loads (90th percentile, so one odd gap does not decide) + 15% margin (at least 2 h), + 12 h for a
    date-only column (a date says nothing about the time of day). None when there are fewer than MIN_LOAD_DAYS days."""
    days = sorted(set(days))
    if len(days) < MIN_LOAD_DAYS:
        return None
    gaps = sorted((b - a).days for a, b in zip(days, days[1:]))
    median = statistics.median(gaps)
    longest = gaps[min(len(gaps) - 1, round(0.9 * (len(gaps) - 1)))]
    limit = int(-(-(longest * 24 + max(2.0, 0.15 * longest * 24) + (12 if date_only else 0)) // 1))
    weekdays_only = len(days) >= 10 and all(x.weekday() < 5 for x in days)
    pattern = ("every day" if longest <= 1 else
               "on weekdays (the limit covers the weekend)" if weekdays_only and median <= 1 else
               f"about every {median:g} days" if median > 1 else f"most days, with gaps up to {longest} days")
    return {"load_days": len(days), "first": days[0].isoformat(), "last": days[-1].isoformat(), "median_gap_days": median,
            "longest_gap_days": longest, "pattern": pattern, "limit_hours": limit}


def learn(connection_id: int, database: str | None, d, t: str, column: str, date_only: bool = False) -> dict:
    """The rhythm as kept in a rule's state: what was learned, and when."""
    days = load_days(connection_id, database, d, t, column)
    r = rhythm(days, date_only) or {"load_days": len(days), "too_few": True}
    return {**r, "window_days": RHYTHM_DAYS, "learned_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}


def _freshness(chk, d, t):
    c = chk["config"]
    col, limit = c["column"], float(c.get("max_age_hours") or 24)
    learned, new_state = None, None
    if c.get("adaptive"):                       # the limit follows how often the table really loads, re-learned daily
        state = dict(chk.get("state") or {})
        learned = state.get("rhythm")
        stale = not learned or (_hours_since(learned.get("learned_at")) or 1e9) >= RELEARN_HOURS
        if stale:
            try:
                learned = learn(chk["connection_id"], chk["database"], d, t, col, bool(c.get("date_only")))
                new_state = {**state, "rhythm": learned}
            except Exception:                   # noqa: BLE001  keep the last rhythm (or the fixed limit) and measure anyway
                pass
        if learned and learned.get("limit_hours"):
            limit = float(learned["limit_hours"])
    sql = f"SELECT MAX({d.ident(col)}) AS latest FROM {t}{d.where(c.get('where'))}"
    latest = query(chk["connection_id"], chk["database"], sql)[0]["latest"]
    age = _hours_since(latest)
    how = f", learned: it loads {learned['pattern']}" if learned and learned.get("pattern") else ""
    expected = f"newest {col} at most {limit:g} h old{how}"
    extra = {"new_state": new_state} if new_state else {}
    if latest is None:
        return dict(status="FAIL", observed=None, expected=expected, message=f"No rows: {col} has no value at all.", sql=sql, **extra)
    age = max(age, 0)
    status = "FAIL" if age > limit else "PASS"
    return dict(status=status, observed=round(age, 2), expected=expected, sql=sql,
                details={"latest": _plain(latest), "limit_hours": limit, "rhythm": learned},
                message=f"Newest {col} is {_plain(latest)}, {age:.1f} h ago" + (f" (limit {limit:g} h)." if status == "FAIL" else "."),
                **extra)


USUAL_LOADS = 14            # a load is compared with the median of this many earlier loads


def _volume_per_load(chk, d, t):
    """The latest load compared with earlier loads, whatever the rhythm (daily, weekdays, weekly): rows are counted per day
    over the last RHYTHM_DAYS, the latest day that received rows is the load checked, and 'usual' is the median of the load
    days before it. Days without a load are not compared, so they never fail. A load still arriving (rows in the last hour,
    or today on a date-only column) is skipped for the previous complete one."""
    c = chk["config"]
    col = c["window_column"]
    q = d.ident(col)
    extra = (c.get("where") or "").strip()
    where = " AND ".join(f"({w})" for w in (d.since(col, RHYTHM_DAYS * 24), extra) if w)
    sql = f"SELECT CAST({q} AS DATE) AS day, COUNT(*) AS n FROM {t}{d.where(where)} GROUP BY CAST({q} AS DATE)"
    today = date.today()
    per_day = {}
    for r in query(chk["connection_id"], chk["database"], sql, max_rows=RHYTHM_DAYS + 10):
        day = _as_date(r["day"])
        if day and day <= today:
            per_day[day] = int(r["n"])
    days = sorted(per_day)
    min_rows = int(c.get("min_rows") if c.get("min_rows") not in (None, "") else 1)
    drop = float(c.get("max_drop_pct") or 50)
    growth = float(c["max_growth_pct"]) if c.get("max_growth_pct") not in (None, "") else None
    learned = rhythm(days, bool(c.get("date_only")))
    how = f" (it loads {learned['pattern']})" if learned else ""
    if not days:
        return dict(status="FAIL", observed=0, expected=f"at least {min_rows:,} rows per load", sql=sql, details={"rows": 0},
                    message=f"No rows arrived in the last {RHYTHM_DAYS} days ({col}).")
    target, note = days[-1], ""
    if target == today and len(days) > 1:                            # today's load may still be arriving
        arriving = bool(c.get("date_only"))
        if not arriving:
            arriving = int(query(chk["connection_id"], chk["database"],
                                 f"SELECT COUNT(*) AS n FROM {t}{d.where(d.since(col, 1))}")[0]["n"]) > 0
        if arriving:
            target, note = days[-2], " Today's load is still arriving, so the previous complete one was checked."
    n = per_day[target]
    earlier = [per_day[x] for x in days if x < target][-USUAL_LOADS:]
    median = statistics.median(earlier) if len(earlier) >= 3 else None
    expected = f"at least {min_rows:,} rows per load" + (f", not below {100 - drop:g}% of the usual load ({median:,.0f})" if median else "") + how
    details = {"rows": n, "usual": median, "history_points": len(earlier), "load_day": target.isoformat(), "rhythm": learned}
    label = f"the load of {target.isoformat()}"
    if n < min_rows:
        return dict(status="FAIL", observed=n, expected=expected, details=details, sql=sql,
                    message=f"{label} has {n:,} rows, fewer than the minimum of {min_rows:,}.{note}")
    if median and n < median * (1 - drop / 100):
        return dict(status="FAIL", observed=n, expected=expected, details=details, sql=sql,
                    message=f"{label} has {n:,} rows: {100 * (1 - n / median):.0f}% fewer than the usual load ({median:,.0f}).{note}")
    if median and growth is not None and n > median * (1 + growth / 100):
        return dict(status="WARN", observed=n, expected=expected, details=details, sql=sql,
                    message=f"{label} has {n:,} rows: {100 * (n / median - 1):.0f}% more than the usual load ({median:,.0f}).{note}")
    usual = f"; the usual load is {median:,.0f}" if median else " (the usual size is known after 3 earlier loads)"
    return dict(status="PASS", observed=n, expected=expected, details=details, sql=sql, message=f"{label} has {n:,} rows{usual}.{note}")


def _volume(chk, d, t):
    c = chk["config"]
    if c.get("per_load") and c.get("window_column"):
        return _volume_per_load(chk, d, t)
    # window_column + window_hours: count only rows of the last N hours (the size of each load) instead of the whole table
    window = d.since(c["window_column"], int(c.get("window_hours") or 24)) if c.get("window_column") else ""
    where = " AND ".join(f"({w})" for w in (window, (c.get("where") or "").strip()) if w)
    sql = f"SELECT COUNT(*) AS n FROM {t}{d.where(where)}"
    n = int(query(chk["connection_id"], chk["database"], sql)[0]["n"])
    min_rows = int(c.get("min_rows") if c.get("min_rows") not in (None, "") else 1)
    drop = float(c.get("max_drop_pct") or 50)
    growth = float(c["max_growth_pct"]) if c.get("max_growth_pct") not in (None, "") else None
    usual = store.usual_values(chk["check_id"]) if chk.get("check_id") else []
    median = statistics.median(usual) if len(usual) >= 3 else None
    rows = f"rows with {c['window_column']} in the last {int(c.get('window_hours') or 24)} h" if window else "rows"
    expected = f"at least {min_rows:,} {rows}" + (f", not below {100 - drop:g}% of usual ({median:,.0f})" if median else "")
    details = {"rows": n, "usual": median, "history_points": len(usual)}
    if n < min_rows:
        return dict(status="FAIL", observed=n, expected=expected, details=details, sql=sql,
                    message=f"{n:,} {rows}, fewer than the minimum of {min_rows:,}.")
    if median and n < median * (1 - drop / 100):
        return dict(status="FAIL", observed=n, expected=expected, details=details, sql=sql,
                    message=f"{n:,} {rows}: {100 * (1 - n / median):.0f}% fewer than usual ({median:,.0f}).")
    if median and growth is not None and n > median * (1 + growth / 100):
        return dict(status="WARN", observed=n, expected=expected, details=details, sql=sql,
                    message=f"{n:,} {rows}: {100 * (n / median - 1):.0f}% more than usual ({median:,.0f}).")
    note = "" if median else " (the usual volume is learned after 3 runs)"
    return dict(status="PASS", observed=n, expected=expected, details=details, sql=sql, message=f"{n:,} {rows}{note}.")


def _null_rate(chk, d, t):
    c = chk["config"]
    col, limit = c["column"], float(c.get("max_null_pct") or 0)
    sql = f"SELECT COUNT(*) AS total, COUNT({d.ident(col)}) AS filled FROM {t}{d.where(c.get('where'))}"
    r = query(chk["connection_id"], chk["database"], sql)[0]
    total, nulls = int(r["total"]), int(r["total"]) - int(r["filled"])
    pct = 100.0 * nulls / total if total else 0.0
    expected = f"at most {limit:g}% empty" if limit else "no empty values"
    status = "FAIL" if pct > limit else "PASS"
    return dict(status=status, observed=round(pct, 4), expected=expected, sql=sql, details={"rows": total, "empty": nulls},
                message=f"{nulls:,} of {total:,} rows have no {col} ({pct:.2f}%).")


def _uniqueness(chk, d, t):
    c = chk["config"]
    cols = ", ".join(d.ident(x) for x in c["columns"])
    grouped = f"SELECT {cols}, COUNT(*) AS copies FROM {t}{d.where(c.get('where'))} GROUP BY {cols} HAVING COUNT(*) > 1"
    sql = f"SELECT COUNT(*) AS dup_keys, COALESCE(SUM(copies), 0) AS dup_rows FROM ({grouped}) AS dups"
    r = query(chk["connection_id"], chk["database"], sql)[0]
    keys, extra = int(r["dup_keys"]), int(r["dup_rows"]) - int(r["dup_keys"])
    label = " + ".join(c["columns"])
    if not keys:
        return dict(status="PASS", observed=0, expected=f"{label} unique", sql=sql, message=f"Every {label} appears once.")
    sample = [{k: _plain(v) for k, v in row.items()} for row in
              query(chk["connection_id"], chk["database"], d.limit(grouped, SAMPLE), max_rows=SAMPLE)]
    return dict(status="FAIL", observed=keys, expected=f"{label} unique", sql=sql, details={"duplicates": sample, "extra_rows": extra},
                message=f"{keys:,} {label} value(s) appear more than once ({extra:,} extra row(s)).")


def _schema(chk, d, t):
    cols = columns(chk["connection_id"], chk["database"], chk["schema_name"], chk["table_name"])
    sql = f"{d.columns_sql()}  -- with {chk['schema_name']}, {chk['table_name']}"
    now = {c["name"]: c["type"] for c in cols}
    base = (chk.get("state") or {}).get("columns")
    if not cols:
        return dict(status="FAIL", observed=None, expected="table exists", sql=sql,
                    message=f"{chk['schema_name']}.{chk['table_name']} was not found (dropped or renamed, or no access).")
    if base is None:
        return dict(status="PASS", observed=len(now), expected="same columns as the accepted schema", sql=sql,
                    details={"columns": now, "baseline_recorded": True}, new_state={"columns": now},
                    message=f"{len(now)} columns recorded as the accepted schema.")
    removed = [c for c in base if c not in now]
    added = [c for c in now if c not in base]
    changed = [{"column": c, "was": base[c], "now": now[c]} for c in now if c in base and base[c] != now[c]]
    details = {"removed": removed, "added": added, "type_changed": changed, "columns": now}
    if removed or changed:
        parts = ([f"removed {', '.join(removed)}"] if removed else []) + \
                ([f"type changed: {', '.join(x['column'] + ' ' + x['was'] + ' -> ' + x['now'] for x in changed)}"] if changed else [])
        return dict(status="FAIL", observed=len(removed) + len(changed), expected="same columns as the accepted schema", sql=sql,
                    details=details, message="Breaking change: " + "; ".join(parts) + ".")
    if added:
        return dict(status="WARN", observed=len(added), expected="same columns as the accepted schema", sql=sql, details=details,
                    message=f"New column(s): {', '.join(added)}. Accept the schema if this is expected.")
    return dict(status="PASS", observed=0, expected="same columns as the accepted schema", sql=sql, details={"columns": now},
                message=f"All {len(now)} columns unchanged.")


def _num(v):
    if v is None:
        return None
    if isinstance(v, bool):
        return float(v)
    if isinstance(v, (int, float, Decimal)):
        return float(v)
    try:
        return float(str(v))
    except ValueError:
        return None


def _custom(chk, d, t):
    c = chk["config"]
    sql = dialects.clean_select(c["sql"])
    rows = query(chk["connection_id"], chk["database"], d.limit(sql, 1000), max_rows=1000)
    sample = [{k: _plain(v) for k, v in r.items()} for r in rows[:SAMPLE]]
    if (c.get("mode") or "no_rows") == "no_rows":
        status = "FAIL" if rows else "PASS"
        return dict(status=status, observed=len(rows), expected="no rows returned", sql=sql, details={"rows": sample},
                    message=(f"{len(rows):,}{'+' if len(rows) == 1000 else ''} row(s) returned: these break the rule."
                             if rows else "No rows returned."))
    op, threshold = c.get("op") or "=", _num(c.get("threshold"))
    if op not in OPS or threshold is None:
        raise ValueError("set the comparison (e.g. = 0) for the value the query returns")
    value = _num(next(iter(rows[0].values()))) if rows and rows[0] else None
    expected = f"value {op} {threshold:g}"
    if value is None:
        return dict(status="FAIL", observed=None, expected=expected, sql=sql, details={"rows": sample},
                    message="The query returned no number (no rows, or the first column is empty or not numeric).")
    ok = OPS[op](value, threshold)
    return dict(status="PASS" if ok else "FAIL", observed=value, expected=expected, sql=sql, details={"rows": sample},
                message=f"The query returned {value:,.4g}" + ("." if ok else f", expected {op} {threshold:g}."))


def _side(spec: dict) -> tuple:
    sql = dialects.clean_select(spec["sql"])
    d = _dialect(spec["connection_id"])
    rows = query(spec["connection_id"], spec.get("database") or None, d.limit(sql, RECON_MAX_ROWS + 1), max_rows=RECON_MAX_ROWS + 1)
    if len(rows) > RECON_MAX_ROWS:
        raise ValueError(f"a reconciliation query may return at most {RECON_MAX_ROWS:,} rows: group or filter it")
    cols = list(rows[0].keys()) if rows else []
    out = {}
    for r in rows:
        vals = list(r.values())
        out[str(_plain(vals[0]))] = [_num(v) for v in vals[1:]]
    return sql, cols, out


def _reconciliation(chk, d, t):
    c = chk["config"]
    tol = float(c.get("tolerance") or 0)
    lsql, lcols, left = _side(c["left"])
    rsql, rcols, right = _side(c["right"])
    lname, rname = c["left"].get("label") or "left", c["right"].get("label") or "right"
    keys = sorted(set(left) | (set() if (c.get("match") or "both") == "left" else set(right)))
    measures = [x for x in lcols[1:]] or ["value"]
    mismatches = []
    for k in keys:
        a, b = left.get(k), right.get(k)
        if a is None or b is None:
            mismatches.append({"key": k, "problem": f"missing in {lname if a is None else rname}", lname: a, rname: b})
            continue
        diffs = {}
        for i, m in enumerate(measures):
            x = a[i] if i < len(a) else None
            y = b[i] if i < len(b) else None
            if x is None or y is None or abs(x - y) > tol:
                diffs[m] = {lname: x, rname: y, "difference": None if x is None or y is None else round(x - y, 6)}
        if diffs:
            mismatches.append({"key": k, "problem": "values differ", "differences": diffs})
    sql = f"-- {lname}\n{lsql}\n\n-- {rname}\n{rsql}"
    details = {"compared_keys": len(keys), "mismatches": mismatches[:50], "key_column": lcols[0] if lcols else None,
               "measures": measures, "left_label": lname, "right_label": rname}
    if not keys:
        return dict(status="WARN", observed=0, expected=f"{lname} = {rname}", sql=sql, details=details,
                    message="Both queries returned no rows: nothing to compare.")
    if mismatches:
        return dict(status="FAIL", observed=len(mismatches), expected=f"{lname} = {rname}", sql=sql, details=details,
                    message=f"{len(mismatches)} of {len(keys)} {details['key_column'] or 'key'}(s) differ between {lname} and {rname}.")
    return dict(status="PASS", observed=0, expected=f"{lname} = {rname}", sql=sql, details=details,
                message=f"All {len(keys)} {details['key_column'] or 'key'}(s) match.")


RUN = {"freshness": _freshness, "volume": _volume, "null_rate": _null_rate, "uniqueness": _uniqueness, "schema": _schema,
       "custom_sql": _custom, "reconciliation": _reconciliation}


def run(chk: dict) -> dict:
    """chk: a monitor.checks row (or an unsaved definition for preview). Never raises: problems become status ERROR."""
    started = time.monotonic()
    try:
        if chk["check_type"] == "pipeline":                         # a factory pipeline's run history, not a query on a database
            from . import pipeline_rules
            r = pipeline_rules.evaluate(chk)
            r["duration_ms"] = int((time.monotonic() - started) * 1000)
            r["details"] = _jsonable(r.get("details"))
            return r
        d = _dialect(chk["connection_id"] if chk["check_type"] != "reconciliation" else chk["config"]["left"]["connection_id"])
        t = d.table(chk["schema_name"], chk["table_name"]) if chk.get("table_name") else None
        r = RUN[chk["check_type"]](chk, d, t)
    except Exception as e:                                          # noqa: BLE001  unreachable source, bad SQL, missing column ...
        msg = str(e).strip().splitlines()[0] if str(e).strip() else type(e).__name__
        r = dict(status="ERROR", observed=None, expected=None, message=f"Could not run the check: {msg}", sql=None)
    r["duration_ms"] = int((time.monotonic() - started) * 1000)
    if r.get("details") is not None:
        r["details"] = _jsonable(r["details"])
    return r


def _jsonable(v):
    if isinstance(v, dict):
        return {str(k): _jsonable(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_jsonable(x) for x in v]
    return _plain(v)


def run_and_save(check_id: int, by: str) -> dict:
    chk = store.get_check(check_id)
    r = run(chk)
    if r.get("new_state"):
        store.set_state(check_id, r.pop("new_state"))
    store.save_result(check_id, r, by)
    return r
