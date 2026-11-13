"""
The Data quality pages for any source other than the POC pipeline (which keeps its own rule engine in dq.py).

    rows(scope)    "Rejected records": the actual rows each source's quality findings point at
                     database  rows found by its failing checks (duplicate keys, rule-breaking rows, empty values,
                               keys that differ between two queries)
                     factory   rows ADF skipped while copying (fault tolerance), with the skip-log samples, and what
                               DataTrust's own source / target checks found
    rules(scope)   "Rule Studio": what decides whether data is good
                     database  its data checks, written as rules
                     factory   each Copy step's data settings (fault tolerance, skip log, column mapping) and the
                               checks DataTrust runs on every copy, with their latest result
Everything is read-only; the rows are samples the checks stored or a 20-row read-only query.
"""
from __future__ import annotations

from datetime import datetime, timezone

import appdb
import scopes

NULL_SAMPLE = 20
ADF_CHECK = {"SOURCE_FILES": "Source files still present", "SOURCE_ROWS": "Rows in the source file = rows ADF read",
             "SKIPPED_ROWS": "Rows ADF skipped", "TARGET_TABLE": "Target table exists", "TARGET_ROWS": "Rows in the target = rows written",
             "SCHEMA_MATCH": "File columns match the table"}


# ============================================================================ rows that fail (Rejected records)
def rows(scope: str | None) -> dict:
    s = scopes.resolve(scope)
    if s["poc"]:
        return {"kind": "poc", "scope": s}
    return {"kind": s["kind"], "scope": s, **(_adf_rows(s) if s["kind"] == "adf" else _db_rows(s))}


def _db_rows(s: dict) -> dict:
    groups, table_level = [], []
    for c in scopes._checks_of(s["id"], skip_dedupe=False):
        if c["last_status"] not in ("FAIL", "WARN"):
            continue
        d = c.get("details") or {}
        base = {"check_id": c["check_id"], "check": c["name"], "type": c["check_type"], "severity": c["severity"], "status": c["last_status"],
                "target": scopes._target(c), "found_at": c["run_at"], "message": c["message"], "total": c["observed"]}
        sample = []
        if c["check_type"] == "uniqueness":
            sample = [{**r, "problem": f"appears {r.get('copies')} times"} for r in d.get("duplicates") or []]
        elif c["check_type"] == "custom_sql":
            sample = [{**r, "problem": "breaks the rule"} for r in d.get("rows") or []] if (c["config"] or {}).get("mode", "no_rows") == "no_rows" else []
        elif c["check_type"] == "reconciliation":
            for m in d.get("mismatches") or []:
                if m.get("differences"):
                    for k, v in m["differences"].items():
                        sample.append({d.get("key_column") or "key": m["key"], "measure": k, **{str(a): b for a, b in v.items()},
                                       "problem": "values differ"})
                else:
                    sample.append({d.get("key_column") or "key": m["key"], "problem": m["problem"]})
        elif c["check_type"] == "null_rate":
            sample = _empty_rows(s["id"], c)
        if sample:
            groups.append({**base, "rows": sample[:50]})
        else:
            table_level.append(base)                                   # freshness, volume, schema: about the table, not rows
    return {"groups": groups, "table_level": table_level,
            "empty": None if groups or table_level else "No failing rows: every data check on this source passes, or none exist yet."}


def _empty_rows(connection_id: int, c: dict) -> list:
    """The first rows where the checked column is empty (a 20-row read-only query on the source)."""
    from checks import engine
    try:
        d = engine._dialect(connection_id)
        col = d.ident(c["config"]["column"])
        sql = d.limit(f"SELECT * FROM {d.table(c['schema_name'], c['table_name'])} WHERE {col} IS NULL", NULL_SAMPLE)
        return [{**{k: engine._plain(v) for k, v in r.items()}, "problem": f"{c['config']['column']} is empty"}
                for r in engine.query(connection_id, c.get("database"), sql, max_rows=NULL_SAMPLE)]
    except Exception as e:                                              # noqa: BLE001  the source may be unreachable right now
        return [{"problem": f"could not read the rows: {type(e).__name__}"}]


def _adf_rows(s: dict) -> dict:
    skipped = appdb.rows("""
        SELECT t.activity_run_id, t.run_id, t.pipeline_name, t.activity_name, t.run_start, t.rows_read, t.rows_written, t.rows_skipped,
               (SELECT c.detail FROM telemetry.checks c WHERE c.activity_run_id = t.activity_run_id AND c.check_type = 'SKIPPED_ROWS') AS detail,
               (SELECT c.observed FROM telemetry.checks c WHERE c.activity_run_id = t.activity_run_id AND c.check_type = 'SKIPPED_ROWS') AS observed
        FROM telemetry.task_runs t
        WHERE t.source_id = %s AND COALESCE(t.rows_skipped, 0) > 0 AND t.run_start > now() - interval '30 days'
        ORDER BY t.run_start DESC LIMIT 100""", (s["id"],))
    findings = appdb.rows("""
        SELECT DISTINCT ON (k.pipeline_name, k.activity_name, c.check_type)
               k.pipeline_name, k.activity_name, c.run_id, c.check_type, c.side, c.asset, c.status, c.expected, c.observed, c.detail, c.checked_at
        FROM telemetry.checks c JOIN telemetry.task_runs k USING (activity_run_id)
        WHERE c.source_id = %s AND c.checked_at > now() - interval '30 days'
        ORDER BY k.pipeline_name, k.activity_name, c.check_type, c.checked_at DESC""", (s["id"],))
    for f in findings:
        f["label"] = ADF_CHECK.get(f["check_type"], f["check_type"])
        f["asset"] = scopes._pretty(f["asset"])
        if f["status"] == "UNKNOWN" and "timeout" in (f["detail"] or "").lower():
            f["hint"] = ("DataTrust could not reach the target database in time. Check that this machine (or the deployed site) is "
                         "allowed in the database's firewall, and that the matching Data estate connection works.")
    problems = [f for f in findings if f["status"] != "PASS"]
    return {"skipped": skipped, "findings": problems, "checked": len(findings),
            "total_skipped": sum(int(r["rows_skipped"] or 0) for r in skipped),
            "empty": None if skipped or problems else "No rows were skipped in the last 30 days, and every check DataTrust ran on the copies passed."}


# ============================================================================ what decides "good data" (Rule Studio)
RULE_TEXT = {
    "freshness": lambda c: f"newest {c['config'].get('column')} at most {c['config'].get('max_age_hours', 24)} h old",
    "volume": lambda c: f"at least {c['config'].get('min_rows', 1)} rows, not {c['config'].get('max_drop_pct', 50)}% below usual",
    "null_rate": lambda c: f"{c['config'].get('column')} at most {c['config'].get('max_null_pct', 0)}% empty",
    "uniqueness": lambda c: f"{' + '.join(c['config'].get('columns') or [])} is unique",
    "schema": lambda c: "columns are not removed, renamed or retyped",
    "custom_sql": lambda c: (f"the query returns no rows" if (c["config"].get("mode") or "no_rows") == "no_rows"
                             else f"the query's value {c['config'].get('op', '=')} {c['config'].get('threshold')}"),
    "reconciliation": lambda c: f"{(c['config'].get('left') or {}).get('label', 'left')} equals {(c['config'].get('right') or {}).get('label', 'right')} per key",
}


def rules(scope: str | None) -> dict:
    """DQ Rule Studio: one place per source. The POC also lists its data checks under its pipeline rules."""
    s = scopes.resolve(scope)
    if s["kind"] == "adf":
        return {"kind": "adf", "scope": s, **_adf_rules(s)}
    return {"kind": "poc" if s["poc"] else "db", "scope": s, **_db_rules(s)}


def _db_rules(s: dict) -> dict:
    cs = scopes._checks_of(s["id"], skip_dedupe=False)
    hist = {}
    if cs:
        for r in appdb.rows("""SELECT check_id, status FROM (SELECT check_id, status, run_at,
                                      row_number() OVER (PARTITION BY check_id ORDER BY run_at DESC) AS n
                               FROM monitor.results WHERE check_id = ANY(%s)) x WHERE n <= 20 ORDER BY check_id, run_at""",
                            ([c["check_id"] for c in cs],)):
            hist.setdefault(r["check_id"], []).append(r["status"])
    out = [{"check_id": c["check_id"], "rule": RULE_TEXT[c["check_type"]](c), "name": c["name"], "type": c["check_type"],
            "applies_to": scopes._target(c), "severity": c["severity"], "status": c["last_status"], "message": c["message"],
            "last_run_at": c["last_run_at"], "sql": (c["config"] or {}).get("sql"), "history": hist.get(c["check_id"], []),
            "repeats_pipeline": (c["config"] or {}).get("dedupe") == "poc"} for c in cs]
    tables = {}
    for r in out:
        tables.setdefault(r["applies_to"], []).append(r["status"])
    return {"rules": out, "scorecard": [{"applies_to": t, "rules": len(v), "passing": v.count("PASS"), "failing": v.count("FAIL"),
                                         "warnings": v.count("WARN"), "errors": v.count("ERROR")} for t, v in sorted(tables.items())]}


def _adf_rules(s: dict) -> dict:
    from adf import resolve
    latest = {(r["pipeline_name"], r["activity_name"], r["check_type"]): r for r in appdb.rows("""
        SELECT DISTINCT ON (k.pipeline_name, k.activity_name, c.check_type) k.pipeline_name, k.activity_name, c.check_type, c.status,
               c.detail, c.checked_at
        FROM telemetry.checks c JOIN telemetry.task_runs k USING (activity_run_id) WHERE c.source_id = %s
        ORDER BY k.pipeline_name, k.activity_name, c.check_type, c.checked_at DESC""", (s["id"],))}
    steps = []
    for d in appdb.rows("SELECT name, body FROM telemetry.definitions WHERE source_id = %s AND kind = 'pipeline' AND NOT removed ORDER BY name",
                        (s["id"],)):
        acts = (d["body"].get("properties") or {}).get("activities") or []
        raw = {}

        def walk(items):
            for a in items or []:
                raw[a.get("name")] = a
                tp = a.get("typeProperties") or {}
                for k in ("activities", "ifTrueActivities", "ifFalseActivities", "defaultActivities"):
                    walk(tp.get(k))
                for case in tp.get("cases") or []:
                    walk(case.get("activities"))
        walk(acts)
        for a in resolve.flatten_activities(acts):
            if a["type"] != "Copy":
                continue
            tp = (raw.get(a["name"]) or {}).get("typeProperties") or {}
            skip = bool(tp.get("enableSkipIncompatibleRow"))
            log = bool((tp.get("logSettings") or {}).get("enableCopyActivityLog") or tp.get("redirectIncompatibleRowSettings"))
            tr = tp.get("translator") or {}
            mapping = tr.get("mappings") or [] if isinstance(tr, dict) else []
            dynamic = isinstance(tr, dict) and tr.get("type") == "Expression"   # mapping decided at run time, e.g. from a config table
            advice = []
            if not skip:
                advice.append("Fault tolerance is off: one bad row stops the whole copy (the run fails). Safe, but nothing loads until the file is fixed.")
            elif not log:
                advice.append("Rows can be skipped silently: turn on the skipped-rows log so DataTrust can show which rows and why.")
            if mapping:
                advice.append(f"Explicit column mapping ({len(mapping)} columns): a renamed or missing column in the file or table fails the copy.")
            if dynamic:
                advice.append(f"The column mapping is decided at run time ({scopes._pretty(tr.get('value'))}). If it names a column the file or "
                              "the target table does not have, the copy fails: keep that mapping in step with both.")
            checks = [{"check_type": k, "label": ADF_CHECK[k], "status": (latest.get((d["name"], a["name"], k)) or {}).get("status"),
                       "detail": (latest.get((d["name"], a["name"], k)) or {}).get("detail")} for k in ADF_CHECK]
            steps.append({"pipeline": d["name"], "activity": a["name"], "skip_incompatible_rows": skip, "skip_log": log,
                          "mapped_columns": len(mapping), "mapping_at_runtime": scopes._pretty(tr.get("value")) if dynamic else None, "mapping": [{"from": (m.get("source") or {}).get("name") or (m.get("source") or {}).get("path"),
                                                                       "to": (m.get("sink") or {}).get("name") or (m.get("sink") or {}).get("path")}
                                                                      for m in mapping][:60],
                          "advice": advice, "checks": [c for c in checks if c["status"]]})
    return {"steps": steps, "empty": None if steps else "No Copy steps in this factory's pipelines."}
