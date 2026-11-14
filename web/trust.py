"""
One trust score for every source: the POC pipeline, any database, an ADF factory, and any kind of source added later.

A source is measured by SIGNALS. Each signal belongs to one of seven subjects and says how far it passes:

    {"subject": "freshness", "ok": 1.0, "severity": "HIGH", "failed": False, "text": "..."}

    ok        1 = passes, 0 = fails, 0.5 = warning or could not run, or a share such as 0.98 (980 of 1,000 rows valid)
    severity  how serious it is when it fails (CRITICAL / HIGH / MEDIUM / LOW); also its weight inside its subject
    failed    the signal is an open problem (a failing rule, a failed run): only these can cap the score

The score, the same way for every source:
    1. each subject's mark = the share of its signals that pass, weighted by severity (0-100)
    2. score = the average of the subjects that have signals; a subject with none is left out, never counted as 0
    3. a serious open problem sets a ceiling, so it is never averaged away:
       CRITICAL -> at most 74 (At risk), HIGH -> at most 89 (Needs attention)
Confidence (how much of the source the signals cover, e.g. 10 of 44 tables) is reported beside the score, never mixed in.

A new kind of source only has to produce signals; nothing here changes.
"""
from __future__ import annotations

SUBJECTS = [
    ("freshness", "Freshness", "Is new data arriving on time?"),
    ("volume", "Volume", "Did a normal amount arrive?"),
    ("validity", "Validity", "Do the values follow the rules?"),
    ("uniqueness", "Uniqueness", "Is each record there only once?"),
    ("schema", "Schema", "Are the columns still the same?"),
    ("reconciliation", "Reconciliation", "Do totals match another system?"),
    ("runs", "Pipeline runs", "Did the loads succeed?"),
]
SUBJECT_LABEL = {k: label for k, label, _ in SUBJECTS}
WEIGHT = {"CRITICAL": 4, "HIGH": 3, "MEDIUM": 2, "LOW": 1}
CAP = {"CRITICAL": 74, "HIGH": 89}
HEALTHY, ATTENTION = 90, 75                 # >= 90 Healthy, >= 75 Needs attention, below = At risk
STATUS_OK = {"PASS": 1.0, "WARN": 0.5, "ERROR": 0.5, "FAIL": 0.0}
SEV_ORDER = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3}


def label_for(score) -> str:
    if score is None:
        return "No data yet"
    return "Healthy" if score >= HEALTHY else "Needs attention" if score >= ATTENTION else "At risk"


def signal(subject: str, ok: float, severity: str = "HIGH", text: str = "", failed: bool | None = None,
           ok_text: str | None = None, origin: str = "built-in", rule_id: int | None = None) -> dict:
    """text says what is wrong; ok_text (optional) what is fine, shown instead when the check fully passes.
    origin: "built-in" (DataTrust's own check of this kind of source) or "rule" (a rule in DQ Rule Studio, rule_id)."""
    assert subject in SUBJECT_LABEL, subject
    ok = max(0.0, min(1.0, float(ok)))
    return {"subject": subject, "ok": ok, "severity": severity if severity in WEIGHT else "MEDIUM",
            "text": ok_text if ok >= 1 and ok_text else text, "failed": (ok == 0) if failed is None else bool(failed),
            "origin": origin, "rule_id": rule_id}


def status_signal(subject: str, status: str | None, severity: str, text: str, **kw) -> dict | None:
    """A rule result (PASS / WARN / ERROR / FAIL) as a signal; a rule that never ran says nothing yet."""
    if status not in STATUS_OK:
        return None
    return signal(subject, STATUS_OK[status], severity, text, failed=status == "FAIL", **kw)


def _state(s: dict) -> str:
    """pass; fail (fails outright, or an open problem serious enough to cap the score); partial (a share, a warning)."""
    if s["ok"] >= 1:
        return "pass"
    return "fail" if s["ok"] == 0 or (s["failed"] and s["severity"] in CAP) else "partial"


def confidence(measured: int | None, total: int | None, unit: str) -> dict | None:
    """How much of the source the signals cover: high from 80 %, medium from 30 %, else low."""
    if not total:
        return None
    pct = round(100 * (measured or 0) / total)
    level = "high" if pct >= 80 else "medium" if pct >= 30 else "low"
    return {"level": level, "measured": measured or 0, "total": total, "unit": unit, "pct": pct,
            "text": f"{level.capitalize()} confidence: {measured or 0} of {total} {unit} measured"}


def compute(signals: list, coverage: dict | None = None) -> dict:
    sig = [s for s in signals if s]
    subjects, marks = [], {}
    for key, label, question in SUBJECTS:
        mine = [s for s in sig if s["subject"] == key]
        if not mine:
            continue
        w = sum(WEIGHT[s["severity"]] for s in mine)
        mark = round(100 * sum(WEIGHT[s["severity"]] * s["ok"] for s in mine) / w, 1)
        marks[key] = (mark, w)
        ordered = sorted(mine, key=lambda s: (s["ok"] >= 1, s["ok"], SEV_ORDER[s["severity"]]))     # problems first, worst first
        subjects.append({"key": key, "label": label, "question": question, "mark": mark, "signals": len(mine),
                         "passing": sum(s["ok"] >= 1 for s in mine),
                         "problems": [s["text"] for s in ordered if s["ok"] < 1][:3],
                         "checks": [{"text": s["text"], "state": _state(s), "ok": round(s["ok"], 4), "severity": s["severity"],
                                     "origin": s.get("origin", "built-in"), "rule_id": s.get("rule_id")} for s in ordered]})
    out = {"score": None, "label": label_for(None), "average": None, "subjects": subjects, "cap": None, "reasons": [],
           "measured": [s["key"] for s in subjects], "not_measured": [label for k, label, _ in SUBJECTS if k not in marks],
           "confidence": coverage, "rules": {"healthy_from": HEALTHY, "attention_from": ATTENTION, "caps": CAP, "weights": WEIGHT}}
    if not subjects:
        return out
    average = sum(m for m, _ in marks.values()) / len(marks)
    score = round(average)
    failing = sorted((s for s in sig if s["failed"] and s["severity"] in CAP), key=lambda s: SEV_ORDER[s["severity"]])
    reasons = []
    if failing and score > CAP[failing[0]["severity"]]:
        worst = failing[0]
        cap = CAP[worst["severity"]]
        out["cap"] = {"severity": worst["severity"], "limit": cap, "text": worst["text"],
                      "subject": SUBJECT_LABEL[worst["subject"]], "open": len(failing)}
        reasons.append({"points": score - cap, "subject": SUBJECT_LABEL[worst["subject"]], "severity": worst["severity"],
                        "reason": f"capped at {cap} ({label_for(cap)}) while a {worst['severity'].lower()} problem is open: {worst['text']}"})
        score = cap
    for s in sig:                                   # what each signal costs on the average: its share of its subject / subjects
        if s["ok"] < 1:
            _, w = marks[s["subject"]]
            pts = round(WEIGHT[s["severity"]] * (1 - s["ok"]) / w * 100 / len(marks), 1)
            if pts > 0:
                reasons.append({"points": pts, "subject": SUBJECT_LABEL[s["subject"]], "severity": s["severity"], "reason": s["text"]})
    reasons = reasons[:1 if out["cap"] else 0] + sorted(reasons[1 if out["cap"] else 0:], key=lambda r: -r["points"])  # the cap first
    out.update(score=score, label=label_for(score), average=round(average, 1), reasons=reasons)
    return out
