"""
Why an incident stopped showing: every resolution gets a recorded reason instead of "it is gone, so it is fixed".

    fixed_by_datatrust  auto-fix or re-insert wrote the corrected rows (audit.dq_corrections)          verified
    pipeline_rerun      a newer pipeline run succeeded and the same rule no longer finds anything       verified
    check_passes        the data check / factory read ran again and passes                             verified
    accepted            someone acknowledged a change that cannot be undone (a schema change)          verified
    rule_changed        the rule was switched off, edited or deleted after the incident opened         NOT verified
    unexplained         it disappeared, but no fix, rerun or rule change was recorded                  NOT verified
    superseded          a newer run has the same failure under a new id: not resolved, linked to it    (still open)

When an incident opens, DataTrust notes where the POC's logs stood (latest batch, last correction id, last rule change id).
Comparing ids instead of clocks keeps this correct even when the POC database and DataTrust's database use different time zones.
"""
from __future__ import annotations

import re

import appdb
import data_service as ds

LABELS = {
    "fixed_by_datatrust": ("Fixed by DataTrust", True),
    "pipeline_rerun": ("Fixed by a pipeline rerun", True),
    "check_passes": ("Re-checked: passes", True),
    "accepted": ("Accepted", True),
    "rule_changed": ("Rule changed", False),
    "unexplained": ("Not verified", False),
    "superseded": ("Still failing in a newer run", False),
}
NO_REASON = ("No reason recorded", False)               # resolved before DataTrust recorded reasons

ID_TYPES = (("RECON-", "SETTLEMENT_MISMATCH"), ("DQ-", "REJECTED_ROWS"), ("DUP-", "DUPLICATES"), ("LOSS-", "DATA_LOSS"),
            ("RUN-", "PIPELINE_FAILURE"), ("SCHEMA", "SCHEMA_CHANGE"))
DATE_AT_END = re.compile(r"-(\d{4}-\d{2}-\d{2})$")


def label(reason: str | None) -> tuple:
    return LABELS.get(reason, NO_REASON)


# ============================================================================ when an incident opens
def meta(i: dict) -> dict:
    """What the incident is about, kept so the reason can be found after the incident is gone."""
    m = i.get("metrics") or {}
    out = {"type": i.get("type")}
    if i.get("business_date"):
        out["business_date"] = str(i["business_date"])
    for k in ("entity", "rule_name", "check_id", "layer"):
        if m.get(k) is not None:
            out[k] = m[k]
    if i.get("adf"):
        out["pipeline"], out["run_id"] = i["adf"].get("pipeline"), i["adf"].get("run_id")
    return out


def _latest_batch() -> dict:
    """The POC's latest run (same choice as the Pipeline log: the newest BRONZE batch) and the status of each layer."""
    if not ds.table_exists("audit.batch_control"):
        return {}
    b = ds.latest_batch("BRONZE")
    if not b.get("batch_id"):
        return {}
    layers = {}
    for r in ds.rows("SELECT layer, status FROM audit.batch_control WHERE batch_id = %(b)s ORDER BY started_at NULLS FIRST",
                     {"b": b["batch_id"]}):
        layers[r["layer"]] = r["status"]                 # a retried layer: the last attempt counts
    return {"batch_id": b["batch_id"], "sig": f"{b['batch_id']}@{b.get('ended_at')}", "layers": layers}


def _max_id(table: str, column: str) -> int:
    return int(ds.one(f"SELECT COALESCE(MAX({column}), 0) AS n FROM {table}")["n"]) if ds.table_exists(table) else 0


def marks(s: dict) -> dict:
    """Where the POC's logs stand right now (only the POC has a pipeline, corrections and rule changes)."""
    if not s.get("poc"):
        return {}
    try:
        return {"batch": _latest_batch().get("sig"), "correction": _max_id("audit.dq_corrections", "correction_id"),
                "rule_change": _max_id("audit.dq_rule_changes", "change_id")}
    except Exception as e:                                # noqa: BLE001  without marks the reason is "not verified", never wrong
        print(f"!! resolution marks not read: {type(e).__name__}: {e}")
        return {}


# ============================================================================ when an incident is gone
def classify(row: dict, open_now: list) -> tuple:
    """(reason, detail in plain words, id of the newer incident that replaces it or None)."""
    iid = row["incident_id"]
    m = dict(row.get("meta") or {})
    mk = row.get("marks") or {}
    t = m.get("type") or next((typ for prefix, typ in ID_TYPES if iid.startswith(prefix)), None)
    open_ids = [i["id"] for i in open_now]
    try:
        if iid.startswith("adf:"):
            return _adf(iid, open_ids)
        if re.match(r"^db\d+:CHECK-\d+$", iid):
            return _check(int(iid.rsplit("-", 1)[1]), row["first_seen"])
        if t == "PIPELINE_FAILURE":
            return _run(iid, open_ids)
        if t == "REJECTED_ROWS":
            return _rejected(iid, m, mk)
        if t == "SETTLEMENT_MISMATCH":
            return _recon(iid, m, mk)
        if t in ("DUPLICATES", "DATA_LOSS"):
            return _counts(t, iid, mk)
        if t == "SCHEMA_CHANGE":
            return _schema(iid)
    except Exception as e:                                # noqa: BLE001
        return "unexplained", f"DataTrust could not work out why it disappeared ({type(e).__name__}).", None
    return "unexplained", "It is no longer in the data, but no fix, rerun or rule change was recorded.", None


def _rerun(mk: dict, layer: str | None = None) -> dict | None:
    """A newer POC run than the one seen when the incident opened, and it succeeded (the given layer, or every layer)."""
    if not mk.get("batch"):
        return None
    b = _latest_batch()
    if not b or b["sig"] == mk["batch"]:
        return None
    ok = b["layers"].get(layer) == "SUCCESS" if layer else bool(b["layers"]) and all(v == "SUCCESS" for v in b["layers"].values())
    return b if ok else None


def _corrections(entity: str, day: str | None, mk: dict, rule: str | None = None) -> dict:
    if "correction" not in mk or not ds.table_exists("audit.dq_corrections"):
        return {}
    r = ds.one("""SELECT COUNT(DISTINCT record_key) AS n, string_agg(DISTINCT approved_by, ', ') AS by
                  FROM audit.dq_corrections
                  WHERE correction_id > %(m)s AND entity = %(e)s AND (%(d)s::date IS NULL OR business_date = %(d)s::date)
                    AND (%(r)s::text IS NULL OR rule_name = %(r)s)""", {"m": mk["correction"], "e": entity, "d": day, "r": rule})
    return r if r.get("n") else {}


def _rule_change(entity: str, rule: str | None, mk: dict) -> dict:
    if "rule_change" not in mk or not ds.table_exists("audit.dq_rule_changes"):
        return {}
    return ds.one("""SELECT rule_name, change, changed_by FROM audit.dq_rule_changes
                     WHERE change_id > %(m)s AND entity = %(e)s AND (%(r)s::text IS NULL OR rule_name = %(r)s)
                     ORDER BY change_id DESC LIMIT 1""", {"m": mk["rule_change"], "e": entity, "r": rule})


def _rejected(iid: str, m: dict, mk: dict) -> tuple:
    day = m.get("business_date") or (DATE_AT_END.search(iid) or [None, None])[1]
    entity, rule = m.get("entity"), m.get("rule_name")
    if not (entity and rule):                             # an incident from before reasons were recorded: read the id
        head = DATE_AT_END.sub("", iid)[3:]
        entity = next((e for e in ds.ENTITIES if head.startswith(e + "-")), None)
        rule = head[len(entity) + 1:] if entity else None
    if not (entity and rule):
        return "unexplained", "The rejected rows are gone, but DataTrust cannot tell which rule they belonged to.", None
    ch = _rule_change(entity, rule, mk)
    if ch:
        return "rule_changed", (f"Rule {rule} was {ch['change'] or 'changed'} by {ch['changed_by'] or 'someone'} after the incident opened. "
                                "The rows stopped being rejected because the rule changed, not because the data was fixed."), None
    current = ds.one("SELECT active FROM audit.dq_rules WHERE entity = %(e)s AND rule_name = %(r)s", {"e": entity, "r": rule}) \
        if ds.table_exists("audit.dq_rules") else {"active": True}
    if not current:
        return "rule_changed", f"Rule {rule} no longer exists, so nothing checks these rows any more.", None
    if current.get("active") is False:
        return "rule_changed", f"Rule {rule} is switched off, so nothing checks these rows any more.", None
    fx = _corrections(entity, day, mk, rule)
    if fx:
        return "fixed_by_datatrust", (f"{fx['n']} {entity} row(s) were corrected and loaded into silver by DataTrust"
                                      f"{' (approved by ' + fx['by'] + ')' if fx.get('by') else ''}; every change is in audit.dq_corrections."), None
    b = _rerun(mk, "SILVER")
    if b:
        return "pipeline_rerun", f"Batch {b['batch_id']} ran after the incident opened, and rule {rule} (unchanged) no longer rejects any {entity} row for {day}.", None
    return "unexplained", ("The rows are no longer in silver.rejected_records, but no correction, pipeline run or rule change was recorded "
                           "(for example the data was cleared or reloaded)."), None


def _recon(iid: str, m: dict, mk: dict) -> tuple:
    day = m.get("business_date") or iid[len("RECON-"):]
    fx = _corrections("payments", day, mk)
    if fx:
        return "fixed_by_datatrust", (f"{fx['n']} payment(s) of {day} were corrected and loaded into silver by DataTrust"
                                      f"{' (approved by ' + fx['by'] + ')' if fx.get('by') else ''}, so silver now matches the gateway settlement."), None
    ch = _rule_change("payments", None, mk)
    if ch:
        return "rule_changed", (f"Payment rule {ch['rule_name']} was {ch['change'] or 'changed'} by {ch['changed_by'] or 'someone'} after the "
                                "incident opened. Silver now matches the settlement because different rows are let through, not because they were fixed."), None
    b = _rerun(mk)
    if b:
        return "pipeline_rerun", f"Batch {b['batch_id']} ran after the incident opened and silver now matches the gateway settlement for {day}.", None
    return "unexplained", "Silver now matches the settlement, but no correction, pipeline run or rule change was recorded.", None


def _counts(t: str, iid: str, mk: dict) -> tuple:
    entity = iid.split("-", 1)[1]
    b = _rerun(mk)
    if b:
        what = "the source file no longer has duplicate keys" if t == "DUPLICATES" else "source and target row counts now agree"
        return "pipeline_rerun", f"Batch {b['batch_id']} ran after the incident opened and {what} for {entity}.", None
    return "unexplained", "It is no longer shown, but no successful pipeline run was recorded after it opened.", None


def _run(iid: str, open_ids: list) -> tuple:
    m = re.match(r"^RUN-(.+)-([A-Z]+)$", iid)
    if not m:
        return "unexplained", "The failed run is no longer the latest.", None
    batch, layer = m.groups()
    newer = next((o for o in open_ids if o.startswith("RUN-") and o.endswith("-" + layer) and o != iid), None)
    if newer:
        return "superseded", f"The {layer} layer failed again in a newer run ({newer}); the incident continues there.", newer
    b = _latest_batch()
    if b.get("layers", {}).get(layer) == "SUCCESS":
        how = "was retried and" if b["batch_id"] == batch else f"ran again as batch {b['batch_id']} and"
        return "pipeline_rerun", f"The pipeline {how} its {layer} layer succeeded.", None
    return "unexplained", "This run is no longer the latest, but the newest run does not show a successful " + layer + " layer.", None


def _schema(iid: str) -> tuple:
    a = appdb.one("""SELECT approved_by, approved_at FROM app.remediation_log WHERE incident_id = %s
                     ORDER BY approved_at DESC LIMIT 1""", (iid,)) if appdb.table_exists("app.remediation_log") else {}
    if a:
        return "accepted", f"Acknowledged by {a['approved_by']}: a schema change is accepted, it cannot be undone by DataTrust.", None
    return "unexplained", "The change is older than the 14 days the schema guard watches, and nobody acknowledged it.", None


def _check(check_id: int, first_seen) -> tuple:
    c = appdb.one("""SELECT c.check_type, c.enabled, c.edited_at > %(fs)s AS edited_after,
                            c.baseline_accepted_at > %(fs)s AS accepted_after,
                            r.status, r.run_at > %(fs)s AS ran_after, r.run_at, r.observed, r.expected
                     FROM monitor.checks c
                     LEFT JOIN LATERAL (SELECT status, run_at, observed, expected FROM monitor.results x
                                        WHERE x.check_id = c.check_id ORDER BY run_at DESC LIMIT 1) r ON true
                     WHERE c.check_id = %(id)s""", {"id": check_id, "fs": first_seen})
    if not c:
        return "rule_changed", "The rule was deleted, so nothing checks this any more.", None
    if not c["enabled"]:
        return "rule_changed", "The rule was switched off, so nothing checks this any more.", None
    if c["check_type"] == "schema" and c.get("accepted_after"):
        return "accepted", "The table's current columns were accepted as the new expected schema.", None
    if c.get("edited_after"):
        return "rule_changed", ("The rule was edited after the incident opened, so it now measures something different; "
                                "the data itself was not shown to be fixed."), None
    if c.get("status") == "PASS" and c.get("ran_after"):
        seen = f": measured {c['observed']:g}" if isinstance(c.get("observed"), (int, float)) else ""
        return "check_passes", f"The rule ran again at {str(c['run_at'])[:16]} and passed{seen}{', expected ' + c['expected'] if c.get('expected') else ''}.", None
    return "unexplained", "It is no longer reported, but the rule has not passed a run since the incident opened.", None


def _adf(iid: str, open_ids: list) -> tuple:
    parts = iid.split(":")
    if len(parts) < 5:
        return "unexplained", "It is no longer reported by the factory.", None
    sid, pipeline, kind, run = parts[1], parts[2], parts[3], ":".join(parts[4:])
    prefix = f"adf:{sid}:{pipeline}:{kind}:"
    newer = next((o for o in open_ids if o.startswith(prefix) and o != iid), None)
    if newer and run != "-":
        return "superseded", f"The same problem showed up again in a newer run of {pipeline}; the incident continues there.", newer
    if run == "-" or pipeline == "-":
        return "check_passes", "DataTrust read the factory again and no longer finds this problem.", None
    ok = appdb.one("""SELECT run_id, run_start FROM telemetry.pipeline_runs
                      WHERE source_id = %(s)s AND pipeline_name = %(p)s AND status = 'Succeeded'
                        AND run_start > COALESCE((SELECT run_start FROM telemetry.pipeline_runs WHERE run_id = %(r)s), '-infinity')
                      ORDER BY run_start DESC LIMIT 1""", {"s": int(sid), "p": pipeline, "r": run})
    if ok:
        return "pipeline_rerun", f"{pipeline} ran again at {str(ok['run_start'])[:16]} and succeeded.", None
    return "unexplained", f"No later successful run of {pipeline} was recorded; the run may simply be older than the window DataTrust reads.", None
