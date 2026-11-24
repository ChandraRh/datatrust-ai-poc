"""
Pipeline rules: your own conditions on a Data Factory pipeline (or one of its steps), checked against the run history
DataTrust already reads from the factory (telemetry.pipeline_runs / task_runs). No connection to the factory is opened.

A pipeline rule is a monitor.checks row with check_type 'pipeline' and config:
    {"source_id": 3, "pipeline": "PL_01_File_Monitoring", "activity": null | "CP_Copy_File_To_Bronze",
     "kind": <below>, ...its settings}

    kind           settings                                               fails when
    finishes_by    time "HH:MM", days [0..6] (Mon = 0), utc_offset_min    no successful run ended by that time on the
                                                                          latest required day that has passed
    success_rate   days (7), min_pct (95)                                 fewer runs succeeded than min_pct
    last_run       -                                                      the latest finished run did not succeed
    rows           metric rows_written | rows_read, mode range |          the latest successful run moved too few / too many
                   average, min, max | pct (30), days (14)                rows (average: compared with the runs before it)
    duration       max_minutes                                            the latest finished run took longer
    not_stuck      max_minutes                                            a run has been in progress longer than that

evaluate() returns the same result shape as every other rule (status, observed, expected, message, details), so the
rule page, history, incidents, Verify and the trust score treat it like any data check.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

import appdb

KINDS = {
    "finishes_by": ("Finishes on time", "freshness"),
    "success_rate": ("Success rate", "runs"),
    "last_run": ("Latest run succeeds", "runs"),
    "rows": ("Rows moved", "volume"),
    "duration": ("Run time", "runs"),
    "not_stuck": ("Not stuck", "runs"),
}
FINISHED = ("Succeeded", "Failed", "Cancelled")
RUNNING = ("InProgress", "Queued", "Canceling")
DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
HISTORY_DAYS = 30


def subject(kind: str) -> str:
    return KINDS.get(kind, ("", "runs"))[1]


# ============================================================================ definition
def validate_config(c: dict) -> dict:
    """The rule's settings, checked and with defaults. Raises ValueError with a sentence a person can act on."""
    if not str(c.get("source_id") or "").isdigit():
        raise ValueError("choose the data factory")
    pipeline = (c.get("pipeline") or "").strip()
    if not pipeline:
        raise ValueError("choose the pipeline")
    kind = c.get("kind")
    if kind not in KINDS:
        raise ValueError(f"choose what to check: {', '.join(KINDS)}")
    out = {"source_id": int(c["source_id"]), "pipeline": pipeline, "activity": (c.get("activity") or "").strip() or None, "kind": kind}

    def num(key, default, lo, hi, label):
        v = c.get(key, default)
        try:
            v = float(v)
        except (TypeError, ValueError):
            raise ValueError(f"{label} must be a number")
        if not lo <= v <= hi:
            raise ValueError(f"{label} must be between {lo:g} and {hi:g}")
        return int(v) if float(v).is_integer() else v

    if kind == "finishes_by":
        t = str(c.get("time") or "").strip()
        if not re.fullmatch(r"([01]?\d|2[0-3]):[0-5]\d", t):
            raise ValueError("the time must look like 09:00")
        days = sorted({int(d) for d in (c.get("days") if c.get("days") is not None else range(7)) if str(d).isdigit() and 0 <= int(d) <= 6})
        if not days:
            raise ValueError("choose at least one day")
        out.update(time=t.zfill(5), days=days, utc_offset_min=num("utc_offset_min", 0, -720, 840, "the time zone offset"))
    elif kind == "success_rate":
        out.update(days=num("days", 7, 1, 90, "the period (days)"), min_pct=num("min_pct", 95, 1, 100, "the minimum success rate"))
    elif kind == "rows":
        out["metric"] = c.get("metric") if c.get("metric") in ("rows_written", "rows_read") else "rows_written"
        out["mode"] = c.get("mode") if c.get("mode") in ("range", "average") else "average"
        if out["mode"] == "range":
            lo = c.get("min")
            hi = c.get("max")
            if lo in (None, "") and hi in (None, ""):
                raise ValueError("give a minimum, a maximum or both")
            out["min"] = num("min", 0, 0, 1e13, "the minimum rows") if lo not in (None, "") else None
            out["max"] = num("max", 0, 0, 1e13, "the maximum rows") if hi not in (None, "") else None
            if out["min"] is not None and out["max"] is not None and out["min"] > out["max"]:
                raise ValueError("the minimum is larger than the maximum")
        else:
            out.update(pct=num("pct", 30, 1, 1000, "the allowed difference (%)"), days=num("days", 14, 1, 90, "the period (days)"))
    elif kind in ("duration", "not_stuck"):
        out["max_minutes"] = num("max_minutes", 60, 1, 10080, "the limit in minutes")
    return out


def describe(c: dict) -> str:
    """The rule in words, e.g. 'PL_01 succeeds by 09:00 (UTC+05:30) on Mon-Fri'."""
    what = f"{c.get('pipeline')}" + (f" step {c['activity']}" if c.get("activity") else "")
    k = c.get("kind")
    if k == "finishes_by":
        return f"{what} succeeds by {c['time']} ({_tz_label(c.get('utc_offset_min', 0))}) on {_days_label(c.get('days') or [])}"
    if k == "success_rate":
        return f"at least {c['min_pct']}% of {what}'s runs succeed over {c['days']} days"
    if k == "last_run":
        return f"the latest run of {what} succeeds"
    if k == "rows":
        m = "rows read" if c.get("metric") == "rows_read" else "rows written"
        if c.get("mode") == "range":
            bounds = " and ".join(x for x in (f"at least {c['min']:,}" if c.get("min") is not None else "",
                                               f"at most {c['max']:,}" if c.get("max") is not None else "") if x)
            return f"{m} by {what}'s latest successful run: {bounds}"
        return f"{m} by {what}'s latest successful run within ±{c['pct']}% of the average of the previous {c['days']} days"
    if k == "duration":
        return f"{what} takes at most {c['max_minutes']} min"
    if k == "not_stuck":
        return f"no run of {what} stays in progress longer than {c['max_minutes']} min"
    return what


def _tz_label(m) -> str:
    m = int(m or 0)
    return f"UTC{'+' if m >= 0 else '-'}{abs(m) // 60:02d}:{abs(m) % 60:02d}"


def _days_label(days: list) -> str:
    days = sorted(days)
    if days == list(range(7)):
        return "every day"
    if days == list(range(5)):
        return "Mon-Fri"
    return ", ".join(DAYS[d] for d in days)


# ============================================================================ the run history
def _runs(c: dict, days: int = HISTORY_DAYS) -> list:
    """Runs of the pipeline (or of its step) in the last `days`, newest first, with the rows each moved."""
    if c.get("activity"):
        rows = appdb.rows("""SELECT run_id, status, run_start, run_end, duration_ms, rows_written, rows_read
                             FROM telemetry.task_runs
                             WHERE source_id = %s AND pipeline_name = %s AND activity_name = %s
                               AND run_start > now() - make_interval(days => %s)
                             ORDER BY run_start DESC LIMIT 2000""", (c["source_id"], c["pipeline"], c["activity"], days))
    else:
        rows = appdb.rows("""SELECT p.run_id, p.status, p.run_start, p.run_end, p.duration_ms,
                                    t.rows_written, t.rows_read
                             FROM telemetry.pipeline_runs p
                             LEFT JOIN LATERAL (SELECT sum(rows_written) AS rows_written, sum(rows_read) AS rows_read
                                                FROM telemetry.task_runs k
                                                WHERE k.source_id = p.source_id AND k.run_id = p.run_id
                                                  AND k.activity_type IN ('Copy', 'ExecuteDataFlow')) t ON true
                             WHERE p.source_id = %s AND p.pipeline_name = %s AND p.run_start > now() - make_interval(days => %s)
                             ORDER BY p.run_start DESC LIMIT 2000""", (c["source_id"], c["pipeline"], days))
    for r in rows:
        for k in ("run_start", "run_end"):
            r[k] = _dt(r[k])
    return rows


def _dt(v):
    """Any timestamp -> UTC. The database answers in its own time zone (e.g. +05:30); the rule's local times are
    computed as UTC + the rule's offset, so everything starts from UTC."""
    if v is None:
        return None
    d = v if isinstance(v, datetime) else datetime.fromisoformat(str(v))
    return (d if d.tzinfo else d.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)


def _evidence(runs: list, n: int = 10) -> list:
    return [{"run_id": r["run_id"], "status": r["status"], "started": r["run_start"].strftime("%Y-%m-%d %H:%M") if r["run_start"] else None,
             "ended": r["run_end"].strftime("%Y-%m-%d %H:%M") if r["run_end"] else None,
             "minutes": round((r["duration_ms"] or 0) / 60000, 1) if r["duration_ms"] is not None else None,
             "rows_written": r.get("rows_written"), "rows_read": r.get("rows_read")} for r in runs[:n]]


def _result(status, observed, expected, message, runs, **details) -> dict:
    return {"status": status, "observed": observed, "expected": expected, "message": message, "sql": None,
            "details": {"rows": _evidence(runs), **details}}


# ============================================================================ evaluation
BACKTEST_DAYS = 14


def evaluate(chk: dict, now: datetime | None = None) -> dict:
    """The rule's result now. An unsaved rule (preview, suggestion) also gets a replay of the last BACKTEST_DAYS days."""
    c = validate_config(chk.get("config") or {})
    now = now or datetime.now(timezone.utc)
    period = c.get("days") if c["kind"] in ("success_rate", "rows") else 0      # finishes_by: "days" are weekdays
    runs = _runs(c, max(HISTORY_DAYS, int(period or 0)) + BACKTEST_DAYS + 1)
    r = _at(c, runs, now)
    if chk.get("check_id") is None:
        r["details"]["backtest"] = backtest(c, runs, now)
    return r


def _at(c: dict, runs: list, t: datetime) -> dict:
    """The rule as it would have stood at time t: only runs that had started by then count."""
    what = c["pipeline"] + (f" step {c['activity']}" if c.get("activity") else "")
    seen = [r for r in runs if r["run_start"] and r["run_start"] <= t]
    return {"finishes_by": _finishes_by, "success_rate": _success_rate, "last_run": _last_run, "rows": _rows,
            "duration": _duration, "not_stuck": _not_stuck}[c["kind"]](c, seen, t, what)


def backtest(c: dict, runs: list, now: datetime, days: int = BACKTEST_DAYS) -> dict:
    """At the end of each of the last `days` days (the rule's time zone): pass, warning or fail. 'Would it have been
    too strict, or would it have caught the bad days?' before the rule is saved."""
    off = timedelta(minutes=int(c.get("utc_offset_min") or 0))
    today = (now + off).date()
    out = []
    for i in range(days, 0, -1):
        d = today - timedelta(days=i)
        end = datetime(d.year, d.month, d.day, tzinfo=timezone.utc) + timedelta(days=1) - off - timedelta(seconds=1)
        out.append({"day": d.isoformat(), "status": _at(c, runs, end)["status"]})
    return {"days": days, "fail": sum(x["status"] == "FAIL" for x in out), "warn": sum(x["status"] == "WARN" for x in out),
            "by_day": out}


def _finishes_by(c, runs, now, what):
    off = timedelta(minutes=int(c.get("utc_offset_min") or 0))
    hh, mm = (int(x) for x in c["time"].split(":"))
    local_now = now + off
    expected = f"a successful run by {c['time']} ({_tz_label(c.get('utc_offset_min'))}) on {_days_label(c['days'])}"
    for back in range(0, 8):                                          # the latest required day whose deadline has passed
        day = (local_now - timedelta(days=back)).date()
        if day.weekday() not in c["days"]:
            continue
        deadline = datetime(day.year, day.month, day.day, hh, mm, tzinfo=timezone.utc) - off
        if deadline > now:
            continue
        start = datetime(day.year, day.month, day.day, tzinfo=timezone.utc) - off
        ok = [r for r in runs if r["status"] == "Succeeded" and r["run_end"] and start <= r["run_end"] <= deadline]
        label = f"{DAYS[day.weekday()]} {day.isoformat()}"
        if ok:
            first = min(ok, key=lambda r: r["run_end"])
            done = (first["run_end"] + off).strftime("%H:%M")
            return _result("PASS", round((first["run_end"] - start).total_seconds() / 3600, 2), expected,
                           f"{what} succeeded at {done} on {label}, before {c['time']}.", runs, day=label)
        late = [r for r in runs if r["status"] == "Succeeded" and r["run_end"] and r["run_end"] > deadline
                and r["run_end"] < deadline + timedelta(days=1)]
        extra = f" It succeeded later, at {(min(late, key=lambda r: r['run_end'])['run_end'] + off).strftime('%H:%M')}." if late else ""
        return _result("FAIL", None, expected, f"No successful run of {what} by {c['time']} on {label}.{extra}", runs, day=label)
    return _result("PASS", None, expected, f"No required day has passed its deadline yet ({_days_label(c['days'])}).", runs)


def _success_rate(c, runs, now, what):
    since = now - timedelta(days=c["days"])
    done = [r for r in _ended(runs, now) if r["run_start"] >= since]
    expected = f"at least {c['min_pct']}% of runs succeed over {c['days']} days"
    if not done:
        return _result("WARN", None, expected, f"{what} has not finished any run in the last {c['days']} days, so there is nothing to measure.", runs)
    ok = sum(r["status"] == "Succeeded" for r in done)
    pct = round(100 * ok / len(done), 1)
    status = "PASS" if pct >= c["min_pct"] else "FAIL"
    return _result(status, pct, expected, f"{ok} of {len(done)} runs of {what} succeeded in {c['days']} days ({pct:g}%).", done)


def _ended(runs, now):
    return [r for r in runs if r["status"] in FINISHED and (r["run_end"] is None or r["run_end"] <= now)]


def _last_run(c, runs, now, what):
    done = _ended(runs, now)
    expected = "the latest finished run succeeded"
    if not done:
        return _result("WARN", None, expected, f"{what} has no finished run in the last {HISTORY_DAYS} days.", runs)
    r = done[0]
    when = r["run_start"].strftime("%Y-%m-%d %H:%M UTC") if r["run_start"] else "?"
    if r["status"] == "Succeeded":
        return _result("PASS", 1, expected, f"The latest run of {what} ({when}) succeeded.", runs)
    return _result("FAIL", 0, expected, f"The latest run of {what} ({when}) ended {r['status'].lower()}.", runs, run_id=r["run_id"])


def _rows(c, runs, now, what):
    m = c["metric"]
    label = "rows read" if m == "rows_read" else "rows written"
    ok = [r for r in _ended(runs, now) if r["status"] == "Succeeded" and r.get(m) is not None]
    if not ok:
        return _result("WARN", None, describe(c), f"No successful run of {what} recorded {label} in the last {HISTORY_DAYS} days.", runs)
    latest = ok[0]
    v = float(latest[m])
    if c["mode"] == "range":
        lo, hi = c.get("min"), c.get("max")
        bad = (lo is not None and v < lo) or (hi is not None and v > hi)
        exp = " and ".join(x for x in (f"at least {lo:,}" if lo is not None else "", f"at most {hi:,}" if hi is not None else "") if x)
        return _result("FAIL" if bad else "PASS", v, f"{label}: {exp}", f"The latest successful run of {what} had {v:,.0f} {label}"
                       + (f", outside {exp}." if bad else "."), runs, run_id=latest["run_id"])
    since = now - timedelta(days=c["days"])
    before = [float(r[m]) for r in ok[1:] if r["run_start"] and r["run_start"] >= since]
    exp = f"within ±{c['pct']}% of the average of the previous {c['days']} days"
    if len(before) < 3:
        return _result("WARN", v, exp, f"Only {len(before)} earlier successful run(s) in {c['days']} days: too few to know what is usual "
                       f"for {what} (the latest had {v:,.0f} {label}).", runs)
    avg = sum(before) / len(before)
    diff = 0 if avg == 0 and v == 0 else (100.0 if avg == 0 else 100 * (v - avg) / avg)
    bad = abs(diff) > c["pct"]
    return _result("FAIL" if bad else "PASS", v, exp, f"The latest successful run of {what} had {v:,.0f} {label}; the average of the "
                   f"previous {len(before)} was {avg:,.0f} ({diff:+.0f}%).", runs, average=round(avg, 1), difference_pct=round(diff, 1),
                   run_id=latest["run_id"])


def _duration(c, runs, now, what):
    done = [r for r in _ended(runs, now) if r["duration_ms"] is not None]
    expected = f"at most {c['max_minutes']} min"
    if not done:
        return _result("WARN", None, expected, f"{what} has no finished run in the last {HISTORY_DAYS} days.", runs)
    mins = round(done[0]["duration_ms"] / 60000, 1)
    bad = mins > c["max_minutes"]
    return _result("FAIL" if bad else "PASS", mins, expected, f"The latest run of {what} took {mins:g} min"
                   + (f", longer than {c['max_minutes']} min." if bad else "."), runs, run_id=done[0]["run_id"])


def _not_stuck(c, runs, now, what):
    # in progress at `now`: still running, or (replaying an earlier moment) it had started and not yet ended
    open_runs = [r for r in runs if r["run_start"] and (r["status"] in RUNNING or (r["run_end"] and r["run_end"] > now))]
    expected = f"no run in progress longer than {c['max_minutes']} min"
    if not open_runs:
        return _result("PASS", 0, expected, f"No run of {what} is in progress.", runs)
    oldest = min(open_runs, key=lambda r: r["run_start"])
    mins = round((now - oldest["run_start"]).total_seconds() / 60, 1)
    bad = mins > c["max_minutes"]
    return _result("FAIL" if bad else "PASS", mins, expected, f"A run of {what} has been in progress for {mins:g} min"
                   + (f", longer than {c['max_minutes']} min." if bad else "."), runs, run_id=oldest["run_id"])


# ============================================================================ choices for the form, and suggestions from history
def options(source_id: int) -> dict:
    """The factory's pipelines and their steps (from its definitions), for the form."""
    from adf import insights, resolve
    defs = insights._definitions(source_id)
    pipes = []
    for name in sorted(defs["pipeline"]):
        acts = resolve.flatten_activities(((defs["pipeline"][name].get("properties") or {}).get("activities")))
        pipes.append({"name": name, "steps": [{"name": a["name"], "type": a["type"]} for a in acts]})
    return {"source_id": source_id, "pipelines": pipes}


def _pct(values: list, q: float) -> float:
    s = sorted(values)
    return s[min(len(s) - 1, max(0, round(q * (len(s) - 1))))]


def suggest(source_id: int, pipeline: str, activity: str | None = None, utc_offset_min: int = 0) -> list:
    """Rules that fit what this pipeline usually does, measured from its runs of the last 30 days. Nothing is saved;
    each comes with what it would report today."""
    base = {"source_id": source_id, "pipeline": pipeline, "activity": activity or None}
    runs = _runs(base)
    ok = [r for r in runs if r["status"] == "Succeeded"]
    done = [r for r in runs if r["status"] in FINISHED]
    out = []
    who = pipeline + (f" step {activity}" if activity else "")

    def add(name, why, cfg, severity="MEDIUM"):
        out.append({"name": name, "why": why, "severity": severity, "config": {**base, **cfg}})

    if done:
        rate = 100 * len(ok) / len(done)
        target = 100 if rate == 100 else max(50, int(rate // 5 * 5))
        add(f"{who}: latest run succeeds", f"{len(ok)} of {len(done)} runs succeeded in 30 days.", {"kind": "last_run"}, "HIGH")
        add(f"{who}: at least {target}% of runs succeed", f"Over 30 days {rate:.0f}% succeeded; this keeps it from getting worse.",
            {"kind": "success_rate", "days": 7, "min_pct": target})
    if len(ok) >= 3:
        off = timedelta(minutes=utc_offset_min)
        ends = [((r["run_end"] + off).hour * 60 + (r["run_end"] + off).minute) for r in ok if r["run_end"]]
        if ends:
            latest = _pct(ends, 0.9) + 30                               # usual finish + 30 min margin, rounded up to 15 min
            latest = min(23 * 60 + 45, -(-latest // 15) * 15)
            hhmm = f"{latest // 60:02d}:{latest % 60:02d}"
            days = sorted({(r["run_end"] + off).weekday() for r in ok if r["run_end"]})
            add(f"{who}: succeeds by {hhmm}", f"It usually finishes by {_pct(ends, 0.9) // 60:02d}:{_pct(ends, 0.9) % 60:02d}; "
                f"30 minutes of margin added. Days it ran: {_days_label(days)}.",
                {"kind": "finishes_by", "time": hhmm, "days": days, "utc_offset_min": utc_offset_min}, "HIGH")
        mins = [r["duration_ms"] / 60000 for r in ok if r["duration_ms"]]
        if mins:
            limit = max(5, int(-(-(_pct(mins, 0.95) * 1.5) // 5) * 5))
            add(f"{who}: takes at most {limit} min", f"95% of successful runs took {_pct(mins, 0.95):.1f} min or less; 50% margin added.",
                {"kind": "duration", "max_minutes": limit})
            add(f"{who}: not stuck for more than {limit * 2} min", "A run still in progress after twice the usual limit is stuck.",
                {"kind": "not_stuck", "max_minutes": limit * 2}, "HIGH")
        rows = [r["rows_written"] for r in ok if r.get("rows_written")]
        if len(rows) >= 4:
            spread = 100 * (max(rows) - min(rows)) / max(1, sum(rows) / len(rows))
            if spread <= 60:
                add(f"{who}: rows within ±30% of usual", f"Rows written per run are steady ({min(rows):,.0f} to {max(rows):,.0f}).",
                    {"kind": "rows", "metric": "rows_written", "mode": "average", "pct": 30, "days": 14})
            else:
                add(f"{who}: writes at least one row", f"Rows per run vary a lot ({min(rows):,.0f} to {max(rows):,.0f}), so only an "
                    "empty run is flagged.", {"kind": "rows", "metric": "rows_written", "mode": "range", "min": 1})
    for s in out:
        s["rule"] = describe(validate_config(s["config"]))
        s["preview"] = evaluate({"config": s["config"]})
    return out


# ============================================================================ a rule described in words
TEXT_SYSTEM = """You turn a sentence about an Azure Data Factory pipeline into ONE monitoring rule. Answer with JSON only:
{"kind": "finishes_by|success_rate|last_run|rows|duration|not_stuck", "settings": {...}, "name": "short name",
 "understood": "the rule in one plain sentence", "assumptions": ["..."]}
settings per kind: finishes_by {"time": "HH:MM", "days": [0-6, Monday=0]}; success_rate {"days": int, "min_pct": number};
last_run {}; rows {"metric": "rows_written|rows_read", "mode": "range|average", "min": int, "max": int, "pct": number, "days": int};
duration {"max_minutes": number}; not_stuck {"max_minutes": number}.
Use only what the sentence says; when something is not said, use a sensible default and list it in assumptions.
If the sentence is not a rule about this pipeline's runs, answer {"cannot": "why"}."""


def phrase(text: str) -> dict | None:
    """Common phrasings without a model: 'by 9:00 on weekdays', 'under 20 minutes', 'at least 1000 rows', '95% succeed'."""
    low = " ".join(text.lower().split())
    m = re.search(r"\bby (\d{1,2})(?::(\d{2}))?\s*(am|pm)?", low)
    if m and re.search(r"finish|succeed|complete|done|run|load", low):
        h = int(m.group(1)) % 12 + (12 if m.group(3) == "pm" else 0) if m.group(3) else int(m.group(1))
        days = list(range(5)) if re.search(r"weekday|working day|business day|mon(day)?\s*(-|to)\s*fri", low) else list(range(7))
        return {"kind": "finishes_by", "settings": {"time": f"{h:02d}:{m.group(2) or '00'}", "days": days},
                "understood": f"A successful run by {h:02d}:{m.group(2) or '00'} on {_days_label(days)}."}
    m = re.search(r"(\d+(?:\.\d+)?)\s*%", low)
    if m and re.search(r"succe", low):
        d = re.search(r"(\d+)\s*days?", low)
        return {"kind": "success_rate", "settings": {"min_pct": float(m.group(1)), "days": int(d.group(1)) if d else 7},
                "understood": f"At least {m.group(1)}% of runs succeed."}
    m = re.search(r"(?:under|less than|at most|within|no more than)\s*(\d+)\s*(min|minute|hour|hr)", low)
    if m:
        mins = int(m.group(1)) * (60 if m.group(2).startswith(("hour", "hr")) else 1)
        kind = "not_stuck" if "stuck" in low or "in progress" in low else "duration"
        return {"kind": kind, "settings": {"max_minutes": mins}, "understood": f"Takes at most {mins} min." if kind == "duration"
                else f"No run in progress longer than {mins} min."}
    m = re.search(r"(?:at least|more than|minimum(?: of)?)\s*([\d,]+)\s*rows", low)
    if m:
        return {"kind": "rows", "settings": {"metric": "rows_written", "mode": "range", "min": int(m.group(1).replace(",", ""))},
                "understood": f"Every successful run writes at least {m.group(1)} rows."}
    if re.search(r"(always|every run|each run|latest run|last run).*(succeed|success)|never fail", low):
        return {"kind": "last_run", "settings": {}, "understood": "The latest run succeeds."}
    return None


def from_text(source_id: int, pipeline: str, activity: str | None, text: str, utc_offset_min: int = 0) -> dict:
    """A rule in words -> a definition, checked against the run history. The model sees the sentence and the pipeline's
    name only (no data). Nothing is saved."""
    import json
    import llm
    text = " ".join((text or "").split())
    if not 5 <= len(text) <= 300:
        raise ValueError("describe the rule in a sentence (5 to 300 characters)")
    got, source, note = None, "phrase", None
    if llm.provider() != "none":
        try:
            got = llm.complete_json(TEXT_SYSTEM, json.dumps({"pipeline": pipeline, "step": activity, "sentence": text}))
            source = f"ai ({llm.model_name()})"
        except Exception as e:                                        # noqa: BLE001  busy / no quota: fall back to phrasings
            note = f"The AI model did not answer ({str(e)[:120]}); matched common phrasings instead."
    if got and got.get("cannot"):
        return {"ok": False, "reason": got["cannot"], "source": source}
    if not (got and got.get("kind") in KINDS):
        got, source = phrase(text), "phrase"
    if not got:
        return {"ok": False, "source": source, "note": note,
                "reason": "This sentence does not match a rule DataTrust knows. Try \"succeeds by 09:00 on weekdays\", "
                          "\"takes under 20 minutes\", \"at least 1000 rows\" or \"95% of runs succeed\", or use the form."}
    cfg = {"source_id": source_id, "pipeline": pipeline, "activity": activity, "kind": got["kind"], **(got.get("settings") or {})}
    if got["kind"] == "finishes_by":
        cfg["utc_offset_min"] = utc_offset_min
    try:
        cfg = validate_config(cfg)
    except ValueError as e:
        return {"ok": False, "reason": f"The proposed rule is not valid: {e}", "source": source}
    return {"ok": True, "config": cfg, "name": (got.get("name") or describe(cfg))[:100], "rule": describe(cfg),
            "understood": got.get("understood"), "assumptions": got.get("assumptions") or [], "source": source, "note": note,
            "preview": evaluate({"config": cfg})}
