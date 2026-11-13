"""
Run one check against its database, read-only, and say what was found.

Result: status PASS / WARN / FAIL / ERROR, observed (one number), expected (in words), message (one sentence),
details (the evidence: sample rows, duplicate keys, mismatches, schema differences) and sql (exactly what ran).
Every number comes from a query; nothing is estimated.
"""
from __future__ import annotations

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


def query(connection_id: int, database: str | None, sql: str, params=(), max_rows: int = 1000) -> list:
    """Rows as dicts. PostgreSQL: a read-only session. SQL Server: inside a transaction that is always rolled back."""
    src = _source(connection_id)
    kind = src.cfg["source_type"]
    if kind == "postgresql":
        with closing(src.connect(database)) as c:                    # read_only=True: default_transaction_read_only + 10 s timeout
            cur = c.execute(sql, params or None)
            return [dict(r) for r in cur.fetchmany(max_rows)] if cur.description else []
    if kind == "sqlserver":
        with closing(src.connect(database, read_only=False)) as c:  # autocommit off -> rollback below undoes anything
            try:
                cur = c.cursor()
                cur.execute(sql, params or ())
                while cur.description is None and cur.nextset():
                    pass
                if cur.description is None:
                    return []
                cols = [d[0] for d in cur.description]
                return [dict(zip(cols, r)) for r in cur.fetchmany(max_rows)]
            finally:
                c.rollback()
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


def _freshness(chk, d, t):
    col, limit = chk["config"]["column"], float(chk["config"].get("max_age_hours") or 24)
    sql = f"SELECT MAX({d.ident(col)}) AS latest FROM {t}{d.where(chk['config'].get('where'))}"
    latest = query(chk["connection_id"], chk["database"], sql)[0]["latest"]
    age = _hours_since(latest)
    expected = f"newest {col} at most {limit:g} h old"
    if latest is None:
        return dict(status="FAIL", observed=None, expected=expected, message=f"No rows: {col} has no value at all.", sql=sql)
    age = max(age, 0)
    status = "FAIL" if age > limit else "PASS"
    return dict(status=status, observed=round(age, 2), expected=expected, sql=sql, details={"latest": _plain(latest)},
                message=f"Newest {col} is {_plain(latest)}, {age:.1f} h ago" + (f" (limit {limit:g} h)." if status == "FAIL" else "."))


def _volume(chk, d, t):
    c = chk["config"]
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
