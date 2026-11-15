"""
Built-in check settings: the checks DataTrust runs on its own for a kind of source (not rules from DQ Rule Studio),
with their severity, limits and on / off editable on the page Data quality -> Built-in checks.

    POC       one set of settings for the POC pipeline (settlement, rows lost / rejected / duplicated, runs, freshness)
    ADF       defaults for every pipeline of every factory, plus overrides for one pipeline ("adf:<source>:<pipeline>")

The same setting drives the incident on Detect and the trust signal (web/trust.py), so they always agree.
Stored in DataTrust's own database (app.builtin_settings); a check never changed keeps its default from CHECKS below.
"""
from __future__ import annotations

import threading
import time
from typing import Optional

from psycopg.types.json import Jsonb

import appdb

SEVERITIES = ["CRITICAL", "HIGH", "MEDIUM", "LOW"]


def _p(name, label, default, lo, hi, unit="", hint=""):
    return {"name": name, "label": label, "default": default, "min": lo, "max": hi, "unit": unit, "hint": hint}


# key -> definition. "severity" = how serious a failure is (and so: Critical caps the score at 74, High at 89)
CHECKS = {
    # ---------------------------------------------------------------- POC pipeline
    "poc.freshness": {"kind": "poc", "subject": "freshness", "label": "Pipeline ran in time", "severity": "HIGH",
                      "what": "A successful pipeline run within the last N hours.",
                      "params": [_p("hours", "Must succeed at least every", 24, 1, 720, "h")]},
    "poc.settlement": {"kind": "poc", "subject": "reconciliation", "label": "Settlement matches the gateway", "severity": "CRITICAL",
                       "what": "For every business date, the gateway's settlement file agrees with the captured payments in silver.",
                       "params": []},
    "poc.rows_lost": {"kind": "poc", "subject": "volume", "label": "No rows lost from file to silver", "severity": "HIGH",
                      "what": "Every row of each source file reaches silver, apart from rows rejected or removed as duplicates on purpose.",
                      "params": []},
    "poc.rejected": {"kind": "poc", "subject": "validity", "label": "Rows rejected by quality rules", "severity": "MEDIUM",
                     "what": "Rows the pipeline's own rules held back in silver.rejected_records (the rules themselves are in the pipeline).",
                     "params": []},
    "poc.duplicates": {"kind": "poc", "subject": "uniqueness", "label": "Duplicate rows in the source files", "severity": "LOW",
                       "what": "The same record delivered more than once in a source file (silver keeps one copy).",
                       "params": []},
    "poc.layers": {"kind": "poc", "subject": "runs", "label": "Latest run: every layer succeeded", "severity": "CRITICAL",
                   "what": "Bronze, Silver and Gold of the latest batch succeeded. The severity is for a failed layer; a cancelled or "
                           "skipped one counts one level lower.",
                   "params": []},
    "poc.incomplete": {"kind": "poc", "subject": "runs", "label": "Run finishes (not stuck halfway)", "severity": "HIGH",
                       "what": "A batch that has started (Bronze written) but has no Gold yet counts as in progress. It is stuck when "
                               "it stays so longer than N times the usual run time (learned from recent complete runs), and never "
                               "less than the minimum.",
                       "params": [_p("factor", "Stuck after", 3, 1, 20, "× the usual run time"),
                                  _p("min_minutes", "But never before", 20, 1, 1440, "min")]},
    # ---------------------------------------------------------------- ADF pipelines
    "adf.run_failed": {"kind": "adf", "subject": "runs", "label": "Latest run succeeded", "severity": "HIGH",
                       "what": "The newest finished run of the pipeline succeeded (a cancelled run counts one level lower).",
                       "params": [_p("streak", "Critical after this many failures in a row", 3, 0, 50, "runs", "0 = never")]},
    "adf.success_rate": {"kind": "adf", "subject": "runs", "label": "Success rate of the last 7 days", "severity": "HIGH", "caps": False,
                         "what": "The share of runs in the last 7 days that succeeded. Lowers the mark; never caps the score on its own.",
                         "params": []},
    "adf.stale": {"kind": "adf", "subject": "freshness", "label": "Loaded on time", "severity": "HIGH",
                  "what": "The last successful run is recent enough. By default the limit follows the trigger: twice its interval plus "
                          "30 minutes.",
                  "params": [_p("hours", "Fixed limit instead of the trigger", 0, 0, 720, "h", "0 = follow the trigger")]},
    "adf.volume": {"kind": "adf", "subject": "volume", "label": "Normal number of rows", "severity": "MEDIUM",
                   "what": "The rows of the latest successful run compared with the median of earlier successful runs. 0 rows counts "
                           "one level higher.",
                   "params": [_p("low_pct", "Too few below", 50, 1, 99, "% of usual"),
                              _p("min_history", "Earlier runs needed to compare", 3, 1, 20, "runs")]},
    "adf.rows_skipped": {"kind": "adf", "subject": "validity", "label": "No rows skipped", "severity": "MEDIUM",
                         "what": "ADF's fault tolerance skipped no rows while copying.",
                         "params": []},
    "adf.after_run": {"kind": "adf", "subject": "volume", "label": "After-run source / target checks", "severity": "HIGH",
                      "what": "DataTrust's checks after each Copy step: source files present, rows read = rows in the file, file "
                              "columns match the table, target table present. The severity is for a failure; a difference is lower.",
                      "params": []},
}

DDL = """
CREATE SCHEMA IF NOT EXISTS app;
CREATE TABLE IF NOT EXISTS app.builtin_settings (
    check_key   TEXT NOT NULL,
    target      TEXT NOT NULL DEFAULT '',          -- '' = the default; 'adf:<source_id>:<pipeline>' = one pipeline
    enabled     BOOLEAN,
    severity    TEXT,
    params      JSONB NOT NULL DEFAULT '{}'::jsonb,
    updated_by  TEXT,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (check_key, target)
);
"""
_cache = {"at": 0.0, "rows": {}}
_lock = threading.Lock()
TTL = 15


def ensure_tables() -> None:
    with appdb.connect() as conn:
        conn.execute(DDL)
        conn.commit()


def _load() -> dict:
    """(check_key, target) -> saved row; cached for TTL s. A database problem means: the defaults."""
    with _lock:
        if time.monotonic() - _cache["at"] < TTL:
            return _cache["rows"]
        try:
            got = {(r["check_key"], r["target"]): r for r in
                   appdb.rows("SELECT check_key, target, enabled, severity, params, updated_by, updated_at FROM app.builtin_settings")} \
                if appdb.table_exists("app.builtin_settings") else {}
        except Exception as e:                                       # noqa: BLE001  never break a page over settings
            print(f"!! built-in check settings not read, defaults used: {e}")
            got = {}
        _cache.update(at=time.monotonic(), rows=got)
        return got


def _forget() -> None:
    with _lock:
        _cache["at"] = 0.0


def target_of(source_id: Optional[int] = None, pipeline: Optional[str] = None) -> str:
    return f"adf:{source_id}:{pipeline}" if source_id is not None and pipeline else ""


def _merge(base: dict, row: dict | None) -> dict:
    if not row:
        return base
    out = dict(base)
    if row.get("enabled") is not None:
        out["enabled"] = bool(row["enabled"])
    if row.get("severity") in SEVERITIES:
        out["severity"] = row["severity"]
    for k, v in (row.get("params") or {}).items():
        if k in out:
            out[k] = v
    return out


def defaults(key: str) -> dict:
    d = CHECKS[key]
    return {"enabled": True, "severity": d["severity"], **{p["name"]: p["default"] for p in d["params"]}}


def setting(key: str, source_id: Optional[int] = None, pipeline: Optional[str] = None) -> dict:
    """The effective setting of a built-in check: its default <- the saved default <- the saved pipeline override."""
    saved = _load()
    s = _merge(defaults(key), saved.get((key, "")))
    t = target_of(source_id, pipeline)
    return _merge(s, saved.get((key, t))) if t else s


def lower(sev: str) -> str:
    return SEVERITIES[min(SEVERITIES.index(sev) + 1, len(SEVERITIES) - 1)]


def higher(sev: str) -> str:
    return SEVERITIES[max(SEVERITIES.index(sev) - 1, 0)]


# ============================================================================ changes
def _clean(key: str, enabled, severity, params: dict) -> tuple:
    if key not in CHECKS:
        raise ValueError(f"unknown built-in check {key}")
    if severity is not None and severity not in SEVERITIES:
        raise ValueError(f"severity must be one of {', '.join(SEVERITIES)}")
    spec = {p["name"]: p for p in CHECKS[key]["params"]}
    out = {}
    for k, v in (params or {}).items():
        if k not in spec:
            raise ValueError(f"{CHECKS[key]['label']} has no setting '{k}'")
        try:
            n = int(v)
        except (TypeError, ValueError):
            raise ValueError(f"{spec[k]['label']}: a whole number, please")
        if not spec[k]["min"] <= n <= spec[k]["max"]:
            raise ValueError(f"{spec[k]['label']}: between {spec[k]['min']} and {spec[k]['max']}")
        out[k] = n
    return enabled, severity, out


def save(key: str, enabled, severity, params: dict, by: str, source_id: Optional[int] = None, pipeline: Optional[str] = None) -> dict:
    enabled, severity, params = _clean(key, enabled, severity, params)
    t = target_of(source_id, pipeline)
    if t and CHECKS[key]["kind"] != "adf":
        raise ValueError("only ADF checks can be set per pipeline")
    with appdb.connect() as conn:
        conn.execute("""INSERT INTO app.builtin_settings (check_key, target, enabled, severity, params, updated_by, updated_at)
                        VALUES (%s, %s, %s, %s, %s, %s, now())
                        ON CONFLICT (check_key, target) DO UPDATE SET enabled = EXCLUDED.enabled, severity = EXCLUDED.severity,
                            params = EXCLUDED.params, updated_by = EXCLUDED.updated_by, updated_at = now()""",
                     (key, t, enabled, severity, Jsonb(params), by))
        conn.commit()
    _forget()
    return setting(key, source_id, pipeline)


def reset(key: str, source_id: Optional[int] = None, pipeline: Optional[str] = None) -> None:
    """Back to the default (or, for a pipeline, back to the setting of all pipelines)."""
    if key not in CHECKS:
        raise ValueError(f"unknown built-in check {key}")
    with appdb.connect() as conn:
        conn.execute("DELETE FROM app.builtin_settings WHERE check_key = %s AND target = %s", (key, target_of(source_id, pipeline)))
        conn.commit()
    _forget()


# ============================================================================ what the page shows
def overview(scope: Optional[str]) -> dict:
    import scopes
    s = scopes.resolve(scope)
    kind = "adf" if s["kind"] == "adf" else "poc" if s["poc"] else None
    out = {"scope": s, "kind": kind, "severities": SEVERITIES, "checks": [], "pipelines": []}
    if not kind:
        return out
    saved = _load()
    pipelines = []
    if kind == "adf":
        from adf import insights
        pipelines = sorted(set(insights._definitions(s["id"])["pipeline"]) | set(insights._recent_runs(s["id"])))
        out["pipelines"] = pipelines
    for key, d in CHECKS.items():
        if d["kind"] != kind:
            continue
        base = saved.get((key, ""))
        row = {"key": key, "label": d["label"], "what": d["what"], "subject": d["subject"], "params": d["params"],
               "caps": d.get("caps", True),                       # False: the severity only weighs how much it counts
               "default": defaults(key), "value": setting(key), "changed": bool(base),
               "changed_by": base and base.get("updated_by"), "changed_at": base and base.get("updated_at"), "overrides": []}
        for p in pipelines:
            o = saved.get((key, target_of(s["id"], p)))
            if o:
                row["overrides"].append({"pipeline": p, "value": setting(key, s["id"], p), "changed_by": o.get("updated_by"),
                                         "changed_at": o.get("updated_at")})
        out["checks"].append(row)
    return out
