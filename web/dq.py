"""
Data quality workbench: DQ Rule Studio + Rejected records explorer.

Rule Studio
    audit.dq_rules            the silver rules as data (seeded with the 17 rules that were in the code, unchanged)
    audit.dq_rule_changes     who changed which rule, when, before -> after
    test / suggest / draft    dry-run a rule on silver, rule ideas from a data profile, plain English -> rule

Rejected records
    silver.rejected_records   what the LATEST load held back
    audit.rejected_history    every rejection ever (filled by the silver layer, back-filled here on start)
    audit.dq_reprocess        re-insert requests: fix values -> re-check with the current rules -> admin approves ->
                              row loaded into silver (SCD1 tables now; SCD2 tables at the next silver run),
                              correction kept in audit.dq_corrections so every later re-run gives the same result
"""
import csv
import io
import json
import os
import re
import sys
from datetime import date, datetime
from pathlib import Path

import data_service as ds
import autofix

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import dq_rules  # noqa: E402  (project root)

SCD1 = {"payments": "payment_id", "payment_orders": "order_id", "refunds": "refund_id", "settlements": "settlement_id",
        "categories": "category_id"}
SCD2 = {"customers": "customer_id", "products": "product_id"}
RAW_KEY = {"products": "id", "categories": "id"}
HIDDEN_COLUMNS = {"batch_id", "loaded_at", "record_hash", "valid_to", "is_current", "surrogate_key", "version"}


# ============================================================================ tables
def ensure_tables() -> None:
    with ds.connect(read_only=False) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS audit.dq_rules (
                rule_id     BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                entity      TEXT NOT NULL,
                rule_name   TEXT NOT NULL,
                rule_type   TEXT NOT NULL,
                column_name TEXT NOT NULL,
                params      JSONB NOT NULL DEFAULT '{}',
                action      TEXT NOT NULL DEFAULT 'REJECT',
                active      BOOLEAN NOT NULL DEFAULT TRUE,
                built_in    BOOLEAN NOT NULL DEFAULT FALSE,
                sort_order  INT NOT NULL DEFAULT 100,
                description TEXT,
                created_by  TEXT DEFAULT 'setup',
                created_at  TIMESTAMP DEFAULT now(),
                updated_by  TEXT,
                updated_at  TIMESTAMP,
                UNIQUE (entity, rule_name))""")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS audit.dq_rule_changes (
                change_id  BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                rule_id    BIGINT, entity TEXT, rule_name TEXT, change TEXT,
                before     JSONB, after JSONB, changed_by TEXT, changed_at TIMESTAMP DEFAULT now())""")
        if not conn.execute("SELECT 1 FROM audit.dq_rules LIMIT 1").fetchone():      # first start: the code rules
            for r in dq_rules.default_rules():
                conn.execute("""INSERT INTO audit.dq_rules (entity, rule_name, rule_type, column_name, params, action, active,
                                                            built_in, sort_order, description)
                                VALUES (%s, %s, %s, %s, %s, %s, TRUE, TRUE, %s, %s) ON CONFLICT DO NOTHING""",
                             (r["entity"], r["rule_name"], r["rule_type"], r["column_name"], json.dumps(r["params"]),
                              r["action"], r["sort_order"], r["description"]))
        conn.execute("""
            CREATE TABLE IF NOT EXISTS audit.rejected_history (
                history_id    BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                entity        TEXT NOT NULL,
                record_key    TEXT,
                business_date DATE,
                rule_name     TEXT,
                raw_record    TEXT,
                batch_id      TEXT,
                rejected_at   TIMESTAMP,
                record_hash   TEXT NOT NULL)""")
        conn.execute("""CREATE UNIQUE INDEX IF NOT EXISTS ux_rejected_history
                        ON audit.rejected_history (entity, batch_id, rule_name, record_hash)""")
        conn.execute("""INSERT INTO audit.rejected_history (entity, record_key, business_date, rule_name, raw_record, batch_id,
                                                            rejected_at, record_hash)
                        SELECT entity, record_key, business_date, rule_name, raw_record, batch_id, rejected_at, md5(raw_record)
                        FROM silver.rejected_records ON CONFLICT DO NOTHING""")      # rows rejected before this feature
        conn.execute("""
            CREATE TABLE IF NOT EXISTS audit.dq_reprocess (
                request_id    BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                entity        TEXT NOT NULL,
                items         JSONB NOT NULL,          -- [{record_key, business_date, rule_name}]
                changes       JSONB NOT NULL DEFAULT '{}',
                note          TEXT,
                status        TEXT NOT NULL DEFAULT 'PENDING',   -- PENDING, APPLIED, QUEUED, REJECTED, FAILED
                result        TEXT,
                requested_by  TEXT, requested_at TIMESTAMP DEFAULT now(),
                decided_by    TEXT, decided_at TIMESTAMP)""")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS audit.rejected_review (
                review_id     BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                entity TEXT, record_key TEXT, business_date DATE, rule_name TEXT,
                note TEXT, reviewed_by TEXT, reviewed_at TIMESTAMP DEFAULT now())""")
        autofix.ensure_tables(conn)                                                  # audit.dq_corrections
        conn.commit()


def _rules(entity: str = None) -> list:
    sql = """SELECT rule_id, entity, rule_name, rule_type, column_name, params, action, active, built_in, sort_order,
                    description, created_by, created_at, updated_by, updated_at FROM audit.dq_rules"""
    out = ds.rows(sql + (" WHERE entity = %(e)s" if entity else "") + " ORDER BY entity, sort_order, rule_name", {"e": entity})
    for r in out:
        r["params"] = r["params"] if isinstance(r["params"], dict) else json.loads(r["params"] or "{}")
        r["text"] = dq_rules.describe(r)
    return out


def columns(entity: str) -> list:
    if entity not in dq_rules.ENTITIES:
        raise ValueError(f"Unknown table '{entity}'.")
    return [c["column_name"] for c in ds.rows("""SELECT column_name FROM information_schema.columns
                                                 WHERE table_schema = 'silver' AND table_name = %(t)s ORDER BY ordinal_position""",
                                              {"t": entity}) if c["column_name"] not in HIDDEN_COLUMNS]


# ============================================================================ Rule Studio
def rules_page() -> dict:
    rules = _rules()
    held = {(r["entity"], r["rule_name"]): r["n"] for r in ds.rows(
        "SELECT entity, rule_name, COUNT(*) AS n FROM silver.rejected_records GROUP BY 1, 2")}
    warned = {}
    for w in ds.rows("""SELECT error_message AS message FROM audit.error_log WHERE error_type = 'DQ_RULE_WARNING'
                        AND batch_id = (SELECT batch_id FROM audit.batch_control WHERE layer = 'SILVER'
                                        ORDER BY started_at DESC NULLS LAST LIMIT 1)""") \
            if ds.table_exists("audit.error_log") else []:
        m = re.match(r"(\d+) (\w+) row\(s\) loaded with warning (\w+)", w.get("message") or "")
        if m:
            warned[(m.group(2), m.group(3))] = int(m.group(1))
    for r in rules:
        r["rejected_now"] = held.get((r["entity"], r["rule_name"]), 0)
        r["warned_last_run"] = warned.get((r["entity"], r["rule_name"]), 0)
    changes = ds.rows("""SELECT changed_at, entity, rule_name, change, changed_by FROM audit.dq_rule_changes
                         ORDER BY change_id DESC LIMIT 30""")
    return {"rules": rules, "types": dq_rules.TYPES, "entities": dq_rules.ENTITIES, "changes": changes,
            "scorecard": scorecard(), "columns": {e: columns(e) for e in dq_rules.ENTITIES}}


def scorecard() -> list:
    out = []
    held = {r["entity"]: r["n"] for r in ds.rows("SELECT entity, COUNT(*) AS n FROM silver.rejected_records GROUP BY 1")}
    active = {r["entity"]: r["n"] for r in ds.rows("SELECT entity, COUNT(*) AS n FROM audit.dq_rules WHERE active GROUP BY 1")}
    for e in dq_rules.ENTITIES:
        if not ds.table_exists(f"silver.{e}"):
            continue
        n = ds.one(f"SELECT COUNT(*) AS n FROM silver.{e}" + (" WHERE is_current" if e in SCD2 else ""))["n"]
        bad = held.get(e, 0)
        out.append({"entity": e, "silver_rows": n, "rejected": bad, "rules": active.get(e, 0),
                    "pass_rate": round(100.0 * n / (n + bad), 2) if n + bad else 100.0})
    return out


def _log_change(conn, rule_id, entity, name, change, before, after, by):
    conn.execute("""INSERT INTO audit.dq_rule_changes (rule_id, entity, rule_name, change, before, after, changed_by)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)""",
                 (rule_id, entity, name, change, json.dumps(before, default=str) if before else None,
                  json.dumps(after, default=str) if after else None, by))


def save_rule(body: dict, by: str) -> dict:
    rule = dq_rules.validate(body, columns(body.get("entity")))
    rid = body.get("rule_id")
    with ds.connect(read_only=False) as conn:
        if rid:
            old = conn.execute("SELECT * FROM audit.dq_rules WHERE rule_id = %s", (rid,)).fetchone()
            if not old:
                raise ValueError("This rule no longer exists.")
            if old["built_in"] and (old["rule_name"] != rule["rule_name"] or old["entity"] != rule["entity"]):
                raise ValueError("A built-in rule keeps its name and table (you can change its check, or switch it off).")
            if conn.execute("SELECT 1 FROM audit.dq_rules WHERE entity = %s AND rule_name = %s AND rule_id <> %s",
                            (rule["entity"], rule["rule_name"], rid)).fetchone():
                raise ValueError(f"{rule['entity']} already has a rule called {rule['rule_name']}.")
            conn.execute("""UPDATE audit.dq_rules SET rule_name = %s, rule_type = %s, column_name = %s, params = %s, action = %s,
                                   active = %s, description = %s, updated_by = %s, updated_at = now() WHERE rule_id = %s""",
                         (rule["rule_name"], rule["rule_type"], rule["column_name"], json.dumps(rule["params"]),
                          rule["action"], rule["active"], rule["description"], by, rid))
            _log_change(conn, rid, rule["entity"], rule["rule_name"], "changed",
                        {k: old[k] for k in ("rule_type", "column_name", "params", "action", "active")}, rule, by)
        else:
            if conn.execute("SELECT 1 FROM audit.dq_rules WHERE entity = %s AND rule_name = %s",
                            (rule["entity"], rule["rule_name"])).fetchone():
                raise ValueError(f"{rule['entity']} already has a rule called {rule['rule_name']}.")
            order = conn.execute("SELECT COALESCE(MAX(sort_order), 0) + 10 AS n FROM audit.dq_rules WHERE entity = %s",
                                 (rule["entity"],)).fetchone()["n"]
            rid = conn.execute("""INSERT INTO audit.dq_rules (entity, rule_name, rule_type, column_name, params, action, active,
                                                               sort_order, description, created_by)
                                  VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING rule_id""",
                               (rule["entity"], rule["rule_name"], rule["rule_type"], rule["column_name"],
                                json.dumps(rule["params"]), rule["action"], rule["active"], order, rule["description"],
                                by)).fetchone()["rule_id"]
            _log_change(conn, rid, rule["entity"], rule["rule_name"], "added", None, rule, by)
        conn.commit()
    return {"rule_id": rid, "text": dq_rules.describe(rule)}


def toggle_rule(rule_id: int, active: bool, by: str) -> dict:
    with ds.connect(read_only=False) as conn:
        r = conn.execute("UPDATE audit.dq_rules SET active = %s, updated_by = %s, updated_at = now() WHERE rule_id = %s "
                         "RETURNING entity, rule_name", (active, by, rule_id)).fetchone()
        if not r:
            raise ValueError("This rule no longer exists.")
        _log_change(conn, rule_id, r["entity"], r["rule_name"], "switched on" if active else "switched off", None, None, by)
        conn.commit()
    return {"ok": True}


def delete_rule(rule_id: int, by: str) -> dict:
    with ds.connect(read_only=False) as conn:
        r = conn.execute("SELECT * FROM audit.dq_rules WHERE rule_id = %s", (rule_id,)).fetchone()
        if not r:
            raise ValueError("This rule no longer exists.")
        if r["built_in"]:
            raise ValueError("Built-in rules cannot be deleted; switch it off instead.")
        conn.execute("DELETE FROM audit.dq_rules WHERE rule_id = %s", (rule_id,))
        _log_change(conn, rule_id, r["entity"], r["rule_name"], "deleted",
                    {k: r[k] for k in ("rule_type", "column_name", "params", "action")}, None, by)
        conn.commit()
    return {"ok": True}


def test_rule(body: dict) -> dict:
    """Dry run: how many rows now in silver would fail this rule (nothing changes)."""
    rule = dq_rules.validate(body, columns(body.get("entity")))
    e = rule["entity"]
    where, args = dq_rules.sql_failing(rule)
    current = " AND is_current" if e in SCD2 else ""
    key = SCD1.get(e) or SCD2.get(e)
    total = ds.one(f"SELECT COUNT(*) AS n FROM silver.{e} WHERE TRUE{current}")["n"]
    with ds.connect() as conn:
        failing = conn.execute(f"SELECT COUNT(*) AS n FROM silver.{e} WHERE {where}{current}", args).fetchone()["n"]
        examples = conn.execute(f"""SELECT {key}::text AS record_key, "{rule['column_name']}"::text AS value
                                    FROM silver.{e} WHERE {where}{current} LIMIT 8""", args).fetchall()
        by_date = []
        if "business_date" in columns(e):
            by_date = conn.execute(f"""SELECT business_date::text AS business_date, COUNT(*) AS n FROM silver.{e}
                                       WHERE {where}{current} GROUP BY 1 ORDER BY 1 DESC LIMIT 10""", args).fetchall()
    # held-back rows of this table that would now pass all rules (if this rule replaced the one with its name)
    others = [r for r in _rules(e) if r["rule_name"] != rule["rule_name"]] + [dict(rule, sort_order=0)]
    would_pass = _recheck(e, others)
    return {"rule": dq_rules.describe(rule), "action": rule["action"], "total": total, "failing": failing,
            "pass_rate": round(100.0 * (total - failing) / total, 2) if total else 100.0,
            "examples": examples, "by_date": by_date, "held_back_would_pass": len(would_pass),
            "note": ("Dry run on the rows now in silver. Nothing was changed. A saved rule is used from the next silver run.")}


def suggest(entity: str) -> list:
    """Rule ideas from a profile of the silver table: columns that are always filled, always positive, or few values."""
    cols = columns(entity)
    current = " WHERE is_current" if entity in SCD2 else ""
    existing = _rules(entity)
    have = {(r["column_name"], r["rule_type"]) for r in existing}
    covered = {r["column_name"] for r in existing if not r["params"].get("allow_empty")}   # empty already fails
    numeric_ruled = {r["column_name"] for r in existing if r["rule_type"] in ("POSITIVE", "NON_NEGATIVE", "RANGE")}
    types = {c["column_name"]: c["data_type"] for c in ds.rows(
        """SELECT column_name, data_type FROM information_schema.columns WHERE table_schema = 'silver' AND table_name = %(t)s""",
        {"t": entity})}
    total = ds.one(f"SELECT COUNT(*) AS n FROM silver.{entity}{current}")["n"]
    if not total:
        return []
    out = []
    for c in cols:
        q = f'"{c}"'
        p = ds.one(f"SELECT COUNT({q}) AS filled, COUNT(DISTINCT {q}) AS distinct_n FROM silver.{entity}{current}")
        t = types.get(c, "")
        if p["filled"] == total and c not in covered:
            out.append({"rule_type": "NOT_NULL", "column_name": c, "params": {}, "rule_name": f"{c.upper()}_REQUIRED",
                        "why": f"{c} is filled in all {total:,} rows today: an empty value would be unusual."})
        if t in ("numeric", "integer", "bigint", "double precision", "real", "smallint"):
            mm = ds.one(f"SELECT MIN({q}) AS lo, MAX({q}) AS hi FROM silver.{entity}{current}")
            if mm["lo"] is not None and float(mm["lo"]) > 0 and not c.endswith("_id") and c not in numeric_ruled:
                out.append({"rule_type": "POSITIVE", "column_name": c, "params": {"allow_empty": p["filled"] < total},
                            "rule_name": f"{c.upper()}_POSITIVE",
                            "why": f"{c} is between {mm['lo']:,} and {mm['hi']:,} today; 0 or negative would be suspicious."})
            elif mm["lo"] is not None and float(mm["lo"]) >= 0 and not c.endswith("_id") and c not in numeric_ruled:
                out.append({"rule_type": "NON_NEGATIVE", "column_name": c, "params": {"allow_empty": p["filled"] < total},
                            "rule_name": f"{c.upper()}_NOT_NEGATIVE", "why": f"{c} is never negative today."})
        elif t in ("text", "character varying") and 1 < p["distinct_n"] <= 12 and (c, "IN_LIST") not in have \
                and not c.endswith("_id") and p["filled"] >= total * 0.5:
            vals = [r["v"] for r in ds.rows(f"SELECT DISTINCT {q}::text AS v FROM silver.{entity}{current} WHERE {q} IS NOT NULL ORDER BY 1")]
            out.append({"rule_type": "IN_LIST", "column_name": c, "params": {"values": vals, "allow_empty": p["filled"] < total},
                        "rule_name": f"{c.upper()}_VALID", "why": f"{c} has only {len(vals)} values today: {', '.join(vals)}."})
        elif "date" in t or "timestamp" in t:
            fut = ds.one(f"SELECT COUNT(*) AS n FROM silver.{entity}{current} WHERE {q} > now()")["n"]
            if not fut and (c, "NOT_IN_FUTURE") not in have:
                out.append({"rule_type": "NOT_IN_FUTURE", "column_name": c, "params": {"allow_empty": p["filled"] < total},
                            "rule_name": f"{c.upper()}_NOT_FUTURE", "why": f"No {c} is in the future today."})
        if c == "email" and (c, "REGEX") not in have and (c, "CONTAINS") not in have:
            out.append({"rule_type": "REGEX", "column_name": c, "params": {"pattern": r"^[^@\s]+@[^@\s]+\.[^@\s]+$",
                                                                          "allow_empty": p["filled"] < total},
                        "rule_name": "EMAIL_FORMAT", "why": "A proper e-mail format check (name@domain.tld)."})
    for s in out:
        s["entity"] = entity
        s["action"] = "REJECT" if s["rule_type"] == "NOT_NULL" and s["column_name"].endswith("_id") else "WARN"
        s["text"] = dq_rules.describe(s)
    return out[:12]


# ---------------------------------------------------------------- plain English -> rule
_WORDS = [
    (r"\b(?:must not|should not|cannot|can't|never)\s+be\s+(?:empty|blank|null|missing)|\bis required\b|\bmandatory\b|\bmust be (?:filled|present|given)\b",
     "NOT_NULL"),
    (r"\b(?:greater|more|bigger|higher) than (?:0|zero)\b|\bpositive\b|\babove (?:0|zero)\b|> ?0\b", "POSITIVE"),
    (r"\bnot negative\b|\bnon[- ]negative\b|\b0 or more\b|\bzero or more\b|>= ?0\b|\bat least (?:0|zero)\b", "NON_NEGATIVE"),
    (r"\bbetween\b", "RANGE"),
    (r"\bone of\b|\bin \(|\ballowed values?\b|\bmust be in\b", "IN_LIST"),
    (r"\bcontains?\b|\bmust have\b", "CONTAINS"),
    (r"\b(?:longer|more) than \d+ (?:characters|chars)\b|\bmax(?:imum)? length\b|\bat most \d+ (?:characters|chars)\b", "MAX_LENGTH"),
    (r"\bfuture\b", "NOT_IN_FUTURE"),
    (r"\bmatch(?:es)?\b|\bpattern\b|\bformat\b", "REGEX"),
]


def draft_rule(entity: str, text: str) -> dict:
    """'currency must not be empty' -> a rule (not saved). Rules first, the AI model only when the words are unclear."""
    cols = columns(entity)
    t = (text or "").strip()
    if not t:
        raise ValueError("Describe the rule, e.g. 'amount must be greater than 0'.")
    low = t.lower()
    col = next((c for c in sorted(cols, key=len, reverse=True) if re.search(rf"\b{re.escape(c.lower())}\b", low)), None)
    if not col:                                           # 'payment status' -> status, 'e-mail' -> email
        col = next((c for c in sorted(cols, key=len, reverse=True)
                    if re.search(rf"\b{re.escape(c.lower().replace('_', ' '))}\b", low.replace("-", ""))), None)
    rtype = next((k for pat, k in _WORDS if re.search(pat, low)), None)
    params, used = {}, "rules"
    if rtype == "RANGE":
        nums = re.findall(r"-?\d+(?:\.\d+)?", low)
        params = {"min": nums[0], "max": nums[1]} if len(nums) >= 2 else {}
    elif rtype == "IN_LIST":
        m = re.search(r"(?:one of|in|values?)\s*:?\s*\(?([^)]*)\)?$", t, re.I)
        params = {"values": [v.strip(" '\"") for v in re.split(r",|\bor\b", m.group(1)) if v.strip(" '\"")]} if m else {}
        if col and "values" in params:
            params["values"] = [v.upper() if v.isalpha() else v for v in params["values"]]
    elif rtype == "CONTAINS":
        m = re.search(r"(?:contain|contains|must have)\s+(?:an?\s+)?['\"]?([^'\"\s]+)", t, re.I)
        params = {"text": m.group(1)} if m else {}
    elif rtype == "MAX_LENGTH":
        m = re.search(r"(\d+)", low)
        params = {"max": m.group(1)} if m else {}
    elif rtype == "REGEX":
        m = re.search(r"(?:match(?:es)?|pattern)\s+['\"`]?([^'\"`]+)['\"`]?", t, re.I)
        params = {"pattern": m.group(1).strip()} if m else {}
    if re.search(r"\b(?:or empty|empty is ok|can be empty|if (?:given|filled|present))\b", low):
        params["allow_empty"] = True
    rule = {"entity": entity, "column_name": col, "rule_type": rtype, "params": params}
    try:
        if not (col and rtype):
            raise ValueError("unclear")
        rule = dq_rules.validate(dict(rule, rule_name=_name(col, rtype), action="REJECT"), cols)
    except ValueError:
        ai = _ask_model(entity, cols, t)
        if not ai:
            raise ValueError("I could not turn that into a rule. Name a column and a check, e.g. 'currency must not be empty', "
                             "'amount must be greater than 0', 'status must be one of CAPTURED, FAILED'. "
                             f"Columns: {', '.join(cols)}")
        rule, used = ai, "AI model"
        rule = dq_rules.validate(dict(rule, entity=entity, rule_name=rule.get("rule_name") or _name(rule.get("column_name", "X"), rule.get("rule_type", "X")),
                                      action=rule.get("action") or "REJECT"), cols)
    rule["text"] = dq_rules.describe(rule)
    rule["drafted_by"] = used
    return rule


def _name(col, rtype):
    return f"{col.upper()}_" + {"NOT_NULL": "REQUIRED", "POSITIVE": "POSITIVE", "NON_NEGATIVE": "NOT_NEGATIVE", "RANGE": "IN_RANGE",
                                "IN_LIST": "VALID", "CONTAINS": "VALID", "MAX_LENGTH": "LENGTH", "NOT_IN_FUTURE": "NOT_FUTURE",
                                "REGEX": "FORMAT"}.get(rtype, "CHECK")


def _ask_model(entity, cols, text):
    provider = os.getenv("LLM_PROVIDER", "none").lower()
    if provider not in ("ollama", "anthropic", "gemini"):
        return None
    import requests
    system = ("Turn one data quality rule written in plain English into JSON. Reply with JSON only.\n"
              '{"column_name": one of the columns, "rule_type": one of ' + ", ".join(dq_rules.TYPES) +
              ', "params": {"min","max"} for RANGE, {"values": [..]} for IN_LIST, {"pattern"} for REGEX, {"text"} for CONTAINS, '
              '{"max"} for MAX_LENGTH, plus "allow_empty": true when an empty value is fine, "rule_name": UPPER_SNAKE_CASE, '
              f'"action": "REJECT" or "WARN"}}.\nTable: silver.{entity}; columns: {", ".join(cols)}')
    try:
        if provider == "gemini":
            import llm_gemini
            content = llm_gemini.complete_json(system, text)
        elif provider == "ollama":
            r = requests.post(os.getenv("OLLAMA_URL", "http://localhost:11434") + "/api/chat", timeout=90, json={
                "model": os.getenv("OLLAMA_MODEL", "qwen2.5:7b"), "stream": False, "format": "json", "options": {"temperature": 0},
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": text}]})
            r.raise_for_status()
            content = r.json()["message"]["content"]
        else:
            r = requests.post("https://api.anthropic.com/v1/messages", timeout=60, headers={
                "x-api-key": os.environ["ANTHROPIC_API_KEY"], "anthropic-version": "2023-06-01", "content-type": "application/json"},
                json={"model": os.getenv("ANTHROPIC_MODEL", "claude-sonnet-5-5"), "max_tokens": 400, "system": system,
                      "messages": [{"role": "user", "content": text}]})
            r.raise_for_status()
            content = "".join(b.get("text", "") for b in r.json()["content"])
        m = re.search(r"\{.*\}", content, re.S)
        return json.loads(m.group(0)) if m else None
    except Exception as e:
        print(f"!! rule drafting with {provider} failed: {type(e).__name__}: {e}")
        return None


# ============================================================================ the silver row of a raw record (Python mirror)
def _c(v, upper=False):
    return autofix._clean(v, upper)


def silver_row(entity: str, raw: dict) -> dict:
    """Raw bronze values -> cleaned silver values, as transform_<entity> in silver_layer.py does."""
    if entity in ("payments", "payment_orders"):
        return autofix.silver_row(entity, raw)
    now = datetime.now()
    if entity == "refunds":
        return {"refund_id": _c(raw.get("refund_id")), "payment_id": _c(raw.get("payment_id")), "order_id": _c(raw.get("order_id")),
                "business_date": autofix._date(raw.get("business_date")), "created_at": autofix._ts(raw.get("created_at")),
                "processed_at": autofix._ts(raw.get("processed_at")), "amount": autofix._dec(raw.get("amount")),
                "currency": _c(raw.get("currency"), True), "status": _c(raw.get("status"), True), "speed": _c(raw.get("speed")),
                "reason": _c(raw.get("reason"), True), "batch_id": raw.get("batch_id"), "loaded_at": now}
    if entity == "settlements":
        return {"settlement_id": _c(raw.get("settlement_id")), "business_date": autofix._date(raw.get("business_date")),
                "settled_at": autofix._ts(raw.get("settled_at")), "payments_count": autofix._int(raw.get("payments_count")),
                "gross_amount": autofix._dec(raw.get("gross_amount")), "refunds": autofix._dec(raw.get("refunds")),
                "fees": autofix._dec(raw.get("fees")), "tax": autofix._dec(raw.get("tax")),
                "net_settled_amount": autofix._dec(raw.get("net_settled_amount")), "currency": _c(raw.get("currency"), True),
                "status": _c(raw.get("status"), True), "utr": _c(raw.get("utr")), "batch_id": raw.get("batch_id"), "loaded_at": now}
    if entity == "categories":
        return {"category_id": autofix._int(raw.get("id")), "category_name": _c(raw.get("name")), "slug": _c(raw.get("slug")),
                "image_url": _c(raw.get("image")), "updated_at": autofix._ts(raw.get("updated_at")),
                "batch_id": raw.get("batch_id"), "loaded_at": now}
    if entity == "customers":
        email = _c(raw.get("email"))
        return {"customer_id": _c(raw.get("customer_id")), "customer_name": _c(raw.get("customer_name")),
                "email": email.lower() if email else None, "city": _c(raw.get("city")), "state": _c(raw.get("state")),
                "region": _c(raw.get("region"), True), "segment": _c(raw.get("segment"), True),
                "valid_from": autofix._ts(raw.get("changed_at")), "business_date": autofix._date(raw.get("business_date"))}
    if entity == "products":
        return {"product_id": autofix._int(raw.get("id")), "title": _c(raw.get("title")), "slug": _c(raw.get("slug")),
                "price": autofix._dec(raw.get("price")), "category_id": autofix._int(raw.get("category_id")),
                "image_url": _c(raw.get("image_1")), "valid_from": autofix._ts(raw.get("updated_at"))}
    raise ValueError(f"unknown table {entity}")


def _check(entity: str, raw: dict, rules: list = None) -> dict:
    rules = rules if rules is not None else _rules(entity)
    row = silver_row(entity, raw)
    failing = dq_rules.failing_rules(rules, row)
    by_name = {r["rule_name"]: r for r in rules}
    return {"passes": not failing, "failing": [{"rule_name": f, "text": by_name[f]["text"] if "text" in by_name[f]
                                                else dq_rules.describe(by_name[f]),
                                                "column": by_name[f]["column_name"],
                                                "value": None if row.get(by_name[f]["column_name"]) is None
                                                else str(row.get(by_name[f]["column_name"]))} for f in failing],
            "warnings": dq_rules.failing_rules(rules, row, "WARN"), "row": row}


def _recheck(entity: str, rules: list) -> list:
    """Held-back rows of this table that pass the given rules (with their current raw values)."""
    out = []
    for r in ds.rows("SELECT record_key, business_date::text AS business_date, rule_name, raw_record FROM silver.rejected_records "
                     "WHERE entity = %(e)s AND record_key IS NOT NULL", {"e": entity}):
        try:
            if _check(entity, json.loads(r["raw_record"]), rules)["passes"]:
                out.append({k: r[k] for k in ("record_key", "business_date", "rule_name")})
        except (ValueError, TypeError):
            continue
    return out


# ============================================================================ Rejected records explorer
def _filters(f: dict):
    where, args = ["TRUE"], {}
    if f.get("date_from"):
        where.append("r.business_date >= %(df)s"); args["df"] = f["date_from"]
    if f.get("date_to"):
        where.append("r.business_date <= %(dt)s"); args["dt"] = f["date_to"]
    if f.get("entity"):
        where.append("r.entity = %(e)s"); args["e"] = f["entity"]
    if f.get("rule"):
        where.append("r.rule_name = %(r)s"); args["r"] = f["rule"]
    if f.get("q"):
        where.append("(r.record_key ILIKE %(q)s OR r.raw_record ILIKE %(q)s)"); args["q"] = f"%{f['q'].strip()}%"
    return " AND ".join(where), args


def rejected(f: dict) -> dict:
    """status=open: what the latest load holds back. status=history: every rejection ever, with what happened to it."""
    where, args = _filters(f)
    limit = min(int(f.get("limit") or 200), 2000)
    args["n"] = limit
    if f.get("status") == "history":
        rows = ds.rows(f"""
            SELECT r.entity, r.record_key, r.business_date::text AS business_date, r.rule_name, r.raw_record, r.batch_id,
                   r.rejected_at,
                   EXISTS (SELECT 1 FROM silver.rejected_records o WHERE o.entity = r.entity AND o.record_key IS NOT DISTINCT FROM r.record_key
                           AND o.business_date IS NOT DISTINCT FROM r.business_date AND o.rule_name = r.rule_name) AS still_open
            FROM audit.rejected_history r WHERE {where}
            ORDER BY r.rejected_at DESC, r.entity, r.record_key LIMIT %(n)s""", args)
        total = ds.one(f"SELECT COUNT(*) AS n FROM audit.rejected_history r WHERE {where}", args)["n"]
        fixes = _resolutions()
        for r in rows:
            r["raw"] = _json(r.pop("raw_record"))
            r["status"] = "OPEN" if r["still_open"] else "RESOLVED"
            r["resolution"] = None if r["still_open"] else fixes.get((r["entity"], r["record_key"])) or \
                "Not rejected any more (passed on a later run: fixed at the source or a rule changed)"
        return {"rows": rows, "total": total, "limit": limit}
    rows = ds.rows(f"""
        SELECT r.entity, r.record_key, r.business_date::text AS business_date, r.rule_name, r.raw_record, r.batch_id, r.rejected_at,
               h.first_seen, COALESCE(h.times, 1) AS times_seen
        FROM silver.rejected_records r
        LEFT JOIN (SELECT entity, record_key, business_date, rule_name, MIN(rejected_at) AS first_seen, COUNT(*) AS times
                   FROM audit.rejected_history GROUP BY 1, 2, 3, 4) h
               ON h.entity = r.entity AND h.record_key IS NOT DISTINCT FROM r.record_key
              AND h.business_date IS NOT DISTINCT FROM r.business_date AND h.rule_name = r.rule_name
        WHERE {where} ORDER BY r.business_date DESC, r.entity, r.rule_name, r.record_key LIMIT %(n)s""", args)
    total = ds.one(f"SELECT COUNT(*) AS n FROM silver.rejected_records r WHERE {where}", args)["n"]
    rules = {(r["entity"], r["rule_name"]): r for r in _rules()}
    reviewed = {(v["entity"], v["record_key"], v["rule_name"]): v for v in ds.rows(
        "SELECT DISTINCT ON (entity, record_key, rule_name) * FROM audit.rejected_review ORDER BY entity, record_key, rule_name, reviewed_at DESC")}
    pending = {}
    for p in ds.rows("SELECT request_id, entity, items FROM audit.dq_reprocess WHERE status = 'PENDING'"):
        for it in (p["items"] if isinstance(p["items"], list) else json.loads(p["items"])):
            pending[(p["entity"], it.get("record_key"))] = p["request_id"]
    for r in rows:
        raw = _json(r.pop("raw_record"))
        rule = rules.get((r["entity"], r["rule_name"]))
        r["raw"] = raw
        r["rule_text"] = rule["text"] if rule else ""
        r["column"] = rule["column_name"] if rule else None
        r["amount"] = raw.get("amount") or raw.get("gross_amount") or raw.get("price")
        rv = reviewed.get((r["entity"], r["record_key"], r["rule_name"]))
        r["reviewed"] = {"note": rv["note"], "by": rv["reviewed_by"], "at": rv["reviewed_at"]} if rv else None
        r["pending_request"] = pending.get((r["entity"], r["record_key"]))
        r["status"] = "IN REVIEW" if r["pending_request"] else "WON'T FIX" if rv else "OPEN"
    return {"rows": rows, "total": total, "limit": limit}


def _json(s):
    try:
        return json.loads(s) if s else {}
    except ValueError:
        return {"_raw": s}


def _resolutions() -> dict:
    out = {}
    for p in ds.rows("""SELECT entity, items, status, decided_by, decided_at, changes FROM audit.dq_reprocess
                        WHERE status IN ('APPLIED', 'QUEUED') ORDER BY request_id"""):
        ch = p["changes"] if isinstance(p["changes"], dict) else json.loads(p["changes"] or "{}")
        what = ", ".join(f"{k} → {v}" for k, v in ch.items()) or "no value changes"
        for it in (p["items"] if isinstance(p["items"], list) else json.loads(p["items"])):
            out[(p["entity"], it.get("record_key"))] = f"Re-inserted ({what}), approved by {p['decided_by']} on {str(p['decided_at'])[:16]}"
    if ds.table_exists("audit.dq_corrections"):
        for c in ds.rows("""SELECT entity, record_key, column_name, new_value, approved_by, applied_at, incident_id
                            FROM audit.dq_corrections WHERE incident_id IS NOT NULL AND active"""):
            out.setdefault((c["entity"], c["record_key"]),
                           f"Auto-fixed ({c['column_name']} → {c['new_value']}), approved by {c['approved_by']} on {str(c['applied_at'])[:16]}")
    return out


def summary(f: dict) -> dict:
    where, args = _filters(dict(f, entity=None, rule=None, q=None))
    by_date = ds.rows(f"""SELECT r.business_date::text AS business_date, r.entity, r.rule_name, COUNT(*) AS n,
                                 COALESCE(SUM(CASE WHEN r.raw_record::jsonb->>'amount' ~ '^-?[0-9.]+$'
                                                   THEN (r.raw_record::jsonb->>'amount')::numeric END), 0) AS amount
                          FROM silver.rejected_records r WHERE {where} GROUP BY 1, 2, 3 ORDER BY 1 DESC, 2, 3""", args)
    hist = ds.rows(f"""SELECT r.business_date::text AS business_date, COUNT(*) AS n FROM audit.rejected_history r
                       WHERE {where} GROUP BY 1 ORDER BY 1 DESC LIMIT 60""", args)
    open_total = sum(r["n"] for r in by_date)
    reviewed = ds.one("SELECT COUNT(DISTINCT (entity, record_key, rule_name)) AS n FROM audit.rejected_review")["n"]
    pending = ds.one("SELECT COUNT(*) AS n FROM audit.dq_reprocess WHERE status = 'PENDING'")["n"]
    fixed = ds.one("SELECT COALESCE(SUM(jsonb_array_length(items)), 0) AS n FROM audit.dq_reprocess WHERE status IN ('APPLIED', 'QUEUED')")["n"]
    return {"by_date": by_date, "history_by_date": hist, "open": open_total, "reviewed": reviewed, "pending": pending,
            "reinserted": fixed, "entities": sorted({r["entity"] for r in by_date}),
            "rules": sorted({r["rule_name"] for r in by_date}),
            "dates": sorted({r["business_date"] for r in by_date if r["business_date"]}, reverse=True)}


def export_csv(f: dict) -> str:
    data = rejected(dict(f, limit=2000))["rows"]
    keys = sorted({k for r in data for k in r["raw"]})
    buf = io.StringIO()
    w = csv.writer(buf)
    head = ["business_date", "entity", "record_key", "rule_name", "status", "batch_id", "rejected_at"]
    w.writerow(head + ["raw_" + k for k in keys])
    for r in data:
        w.writerow([r.get(h, "") for h in head] + [r["raw"].get(k, "") for k in keys])
    return buf.getvalue()


def record(entity: str, key: str, business_date: str = None) -> dict:
    """One held-back record: raw values, why it fails now (column + value), history of runs, requests."""
    args = {"e": entity, "k": key, "d": business_date}
    cur = ds.rows("""SELECT record_key, business_date::text AS business_date, rule_name, raw_record, batch_id, rejected_at
                     FROM silver.rejected_records WHERE entity = %(e)s AND record_key = %(k)s
                       AND (%(d)s::date IS NULL OR business_date = %(d)s::date)""", args)
    hist = ds.rows("""SELECT business_date::text AS business_date, rule_name, batch_id, rejected_at FROM audit.rejected_history
                      WHERE entity = %(e)s AND record_key = %(k)s ORDER BY rejected_at DESC LIMIT 50""", args)
    reqs = ds.rows("""SELECT request_id, status, changes, note, result, requested_by, requested_at, decided_by, decided_at
                      FROM audit.dq_reprocess WHERE entity = %(e)s AND items @> %(j)s::jsonb ORDER BY request_id DESC""",
                   {"e": entity, "j": json.dumps([{"record_key": key}])})
    if not cur:
        return {"open": False, "history": hist, "requests": reqs}
    raw = _json(cur[0]["raw_record"])
    chk = _check(entity, raw)
    chk.pop("row")
    owners = []
    try:
        from estate import store
        owners = store.match_people(f"silver.{entity} {entity}")
    except Exception:
        pass
    return {"open": True, "entity": entity, "record_key": key, "business_date": cur[0]["business_date"],
            "rule_name": cur[0]["rule_name"], "batch_id": cur[0]["batch_id"], "rejected_at": cur[0]["rejected_at"],
            "raw": raw, "check": chk, "history": hist, "requests": reqs, "editable": raw_fields(entity, raw),
            "key_column": RAW_KEY.get(entity, SCD1.get(entity) or SCD2.get(entity)), "scd2": entity in SCD2,
            "owners": [o.get("name") for o in owners][:3]}


# ---------------------------------------------------------------- re-insert (preview -> request -> approve)
def _items(entity: str, items: list) -> list:
    if entity not in dq_rules.ENTITIES:
        raise ValueError(f"Unknown table '{entity}'.")
    if not items:
        raise ValueError("Select at least one record.")
    if len(items) > 500:
        raise ValueError("At most 500 records in one request.")
    out = []
    for it in items:
        if not it.get("record_key"):
            raise ValueError("Records without a key cannot be re-inserted: the source has to send them again with a key.")
        out.append({"record_key": str(it["record_key"]), "business_date": it.get("business_date") or None,
                    "rule_name": it.get("rule_name")})
    return out


SYSTEM_FIELDS = {"batch_id", "ingested_at", "source_file", "load_mode", "_rescued_data"}


def raw_fields(entity: str, raw: dict = None) -> list:
    """Fields a held-back record can have: its own fields plus every bronze column (a missing value is not in the
    stored JSON at all, e.g. a payment without currency has no 'currency' key)."""
    cols = [c["column_name"] for c in ds.rows("""SELECT column_name FROM information_schema.columns
                                                 WHERE table_schema = 'bronze' AND table_name = %(t)s ORDER BY ordinal_position""",
                                              {"t": entity})]
    out = [c for c in cols if c not in SYSTEM_FIELDS]
    out += [k for k in (raw or {}) if k not in out and k not in SYSTEM_FIELDS]
    return out


def _clean_changes(entity: str, changes: dict) -> dict:
    key = RAW_KEY.get(entity, SCD1.get(entity) or SCD2.get(entity))
    out = {}
    for k, v in (changes or {}).items():
        if not re.match(r"^[A-Za-z_][A-Za-z0-9_]{0,62}$", str(k)):
            raise ValueError(f"'{k}' is not a column name.")
        if k in (key, "batch_id"):
            raise ValueError(f"{k} cannot be changed here (it identifies the record).")
        out[k] = None if v is None else str(v)[:500]
    return out


def preview(entity: str, items: list, changes: dict, exclude_request: int = None) -> dict:
    """For every selected record: the raw values with the changes, and whether it now passes the current rules.
    Nothing is changed."""
    items, changes = _items(entity, items), _clean_changes(entity, changes)
    rules = _rules(entity)
    key = SCD1.get(entity) or SCD2.get(entity)
    pending = {}
    for p in ds.rows("SELECT request_id, items FROM audit.dq_reprocess WHERE status = 'PENDING' AND entity = %(e)s "
                     "AND request_id <> %(me)s", {"e": entity, "me": exclude_request or 0}):
        for x in (p["items"] if isinstance(p["items"], list) else json.loads(p["items"])):
            pending[x.get("record_key")] = p["request_id"]
    out = []
    for it in items:
        if it["record_key"] in pending:
            out.append({**it, "ok": False, "reason": f"already in re-insert request #{pending[it['record_key']]} (waiting for approval)"})
            continue
        rec = ds.one("""SELECT raw_record, business_date::text AS business_date, rule_name FROM silver.rejected_records
                        WHERE entity = %(e)s AND record_key = %(k)s AND (%(d)s::date IS NULL OR business_date = %(d)s::date)
                        LIMIT 1""", {"e": entity, "k": it["record_key"], "d": it["business_date"]})
        if not rec:
            out.append({**it, "ok": False, "reason": "not held back any more (already fixed or re-run)"})
            continue
        raw = _json(rec["raw_record"])
        fields = raw_fields(entity, raw)
        unknown = [k for k in changes if k not in fields]
        if unknown:
            out.append({**it, "ok": False, "reason": f"the record has no field {', '.join(unknown)}"})
            continue
        fixed = dict(raw, **changes)
        chk = _check(entity, fixed, rules)
        row = chk["row"]
        exists = ds.one(f"SELECT 1 AS x FROM silver.{entity} WHERE {key} = %(k)s" + (" AND is_current" if entity in SCD2 else ""),
                        {"k": row.get(key)}) if row.get(key) is not None else {}
        reason = ("passes all current rules" if chk["passes"] else
                  "still fails " + "; ".join(f"{f['rule_name']} ({f['column']} = {f['value'] if f['value'] is not None else 'empty'})"
                                              for f in chk["failing"]))
        ok = chk["passes"]
        if ok and exists and entity in SCD1:
            ok, reason = False, f"{key} {row.get(key)} is already in silver.{entity} (a newer version was loaded); re-inserting would overwrite it"
        out.append({**it, "ok": ok, "reason": reason, "changes": {k: [raw.get(k), v] for k, v in changes.items()},
                    "warnings": chk["warnings"]})
    good = [o for o in out if o["ok"]]
    how = ("loaded into silver right away" if entity in SCD1 else
           "queued: loaded by the next silver run (history table, SCD2)")
    return {"entity": entity, "items": out, "ready": len(good), "blocked": len(out) - len(good), "how": how,
            "changes": changes}


def request_reprocess(entity: str, items: list, changes: dict, note: str, by: str) -> dict:
    p = preview(entity, items, changes)
    if not p["ready"]:
        raise ValueError("None of the selected records would pass the rules: " + (p["items"][0]["reason"] if p["items"] else ""))
    ready = [{k: o[k] for k in ("record_key", "business_date", "rule_name")} for o in p["items"] if o["ok"]]
    with ds.connect(read_only=False) as conn:
        rid = conn.execute("""INSERT INTO audit.dq_reprocess (entity, items, changes, note, requested_by)
                              VALUES (%s, %s, %s, %s, %s) RETURNING request_id""",
                           (entity, json.dumps(ready), json.dumps(p["changes"]), (note or "")[:500], by)).fetchone()["request_id"]
        conn.commit()
    return {"request_id": rid, "records": len(ready), "skipped": p["blocked"]}


def requests_list(status: str = None) -> list:
    out = ds.rows("""SELECT request_id, entity, items, changes, note, status, result, requested_by, requested_at, decided_by, decided_at
                     FROM audit.dq_reprocess""" + (" WHERE status = %(s)s" if status else "") +
                  " ORDER BY request_id DESC LIMIT 100", {"s": status})
    for r in out:
        r["items"] = r["items"] if isinstance(r["items"], list) else json.loads(r["items"])
        r["changes"] = r["changes"] if isinstance(r["changes"], dict) else json.loads(r["changes"] or "{}")
    return out


def decide(request_id: int, decision: str, by: str) -> dict:
    req = ds.one("SELECT * FROM audit.dq_reprocess WHERE request_id = %(i)s", {"i": request_id})
    if not req:
        raise ValueError("Request not found.")
    if req["status"] != "PENDING":
        raise ValueError(f"This request is already {req['status']}.")
    if decision == "reject":
        with ds.connect(read_only=False) as conn:
            conn.execute("UPDATE audit.dq_reprocess SET status = 'REJECTED', decided_by = %s, decided_at = now() WHERE request_id = %s",
                         (by, request_id))
            conn.commit()
        return {"status": "REJECTED"}
    if decision != "approve":
        raise ValueError("Decision must be approve or reject.")
    try:
        return _apply(req, by)
    except ValueError:
        raise
    except Exception as e:
        with ds.connect(read_only=False) as conn:
            conn.execute("UPDATE audit.dq_reprocess SET status = 'FAILED', result = %s, decided_by = %s, decided_at = now() "
                         "WHERE request_id = %s", (f"{type(e).__name__}: {str(e).splitlines()[0]}"[:500], by, request_id))
            conn.commit()
        raise ValueError(f"Nothing was changed (one transaction): {type(e).__name__}: {str(e).splitlines()[0]}")


def _apply(req: dict, by: str) -> dict:
    entity = req["entity"]
    items = req["items"] if isinstance(req["items"], list) else json.loads(req["items"])
    changes = req["changes"] if isinstance(req["changes"], dict) else json.loads(req["changes"] or "{}")
    p = preview(entity, items, changes, exclude_request=req["request_id"])   # re-check now: the data may have changed
    ready = [o for o in p["items"] if o["ok"]]
    if not ready:
        raise ValueError("Nothing to re-insert any more: " + (p["items"][0]["reason"] if p["items"] else "no records"))
    rid = req["request_id"]
    scd1 = entity in SCD1
    key = SCD1.get(entity) or SCD2.get(entity)
    loaded = 0
    with ds.connect(read_only=False) as conn:
        for it in ready:
            rec = conn.execute("""SELECT raw_record, business_date, rule_name FROM silver.rejected_records
                                  WHERE entity = %s AND record_key = %s AND (%s::date IS NULL OR business_date = %s::date) LIMIT 1""",
                               (entity, it["record_key"], it["business_date"], it["business_date"])).fetchone()
            if not rec:
                continue
            raw = json.loads(rec["raw_record"])
            for col, value in changes.items():                        # kept: the next pipeline run applies the same fix
                conn.execute("""INSERT INTO audit.dq_corrections (entity, record_key, business_date, column_name, old_value,
                                                                  new_value, rule_name, evidence, incident_id, approved_by)
                                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                             (entity, it["record_key"], rec["business_date"], col, raw.get(col), value, rec["rule_name"],
                              f"Re-insert request #{rid} by {req['requested_by']}: {req.get('note') or 'manual correction'}",
                              f"REPROCESS-{rid}", by))
            if scd1:
                row = silver_row(entity, dict(raw, **changes))
                cols = list(row)
                conn.execute(f"""INSERT INTO silver.{entity} ({", ".join(cols)}) VALUES ({", ".join(["%s"] * len(cols))})
                                 ON CONFLICT ({key}) DO UPDATE SET {", ".join(f"{c} = EXCLUDED.{c}" for c in cols if c != key)}""",
                             [row[c] for c in cols])
                conn.execute("""DELETE FROM silver.rejected_records WHERE entity = %s AND record_key = %s
                                AND (%s::date IS NULL OR business_date = %s::date)""",
                             (entity, it["record_key"], it["business_date"], it["business_date"]))
            loaded += 1
        status = "APPLIED" if scd1 else "QUEUED"
        result = (f"{loaded} {entity} record(s) loaded into silver.{entity}" if scd1 else
                  f"{loaded} correction(s) saved; silver.{entity} keeps history (SCD2), so the next silver run loads them")
        if p["blocked"]:
            result += f"; {p['blocked']} skipped ({p['items'][0]['reason'] if not p['items'][0]['ok'] else 'see preview'})"
        conn.execute("UPDATE audit.dq_reprocess SET status = %s, result = %s, decided_by = %s, decided_at = now() WHERE request_id = %s",
                     (status, result, by, rid))
        if scd1:                                                       # other waiting requests for rows that are now loaded
            for other in conn.execute("SELECT request_id, items FROM audit.dq_reprocess WHERE status = 'PENDING' AND entity = %s",
                                      (entity,)).fetchall():
                keys = [x.get("record_key") for x in other["items"]]
                still = conn.execute("SELECT COUNT(*) AS n FROM silver.rejected_records WHERE entity = %s AND record_key = ANY(%s)",
                                     (entity, keys)).fetchone()["n"]
                if not still:
                    conn.execute("""UPDATE audit.dq_reprocess SET status = 'CLOSED', result = %s, decided_by = %s, decided_at = now()
                                    WHERE request_id = %s""", (f"Nothing left to do: re-inserted by request #{rid}", by, other["request_id"]))
        remediations = []
        if scd1 and loaded:                                            # Detect / Verify: the matching DQ incidents
            open_ids = {i["id"] for i in ds.incidents()}               # read before this commit: still open
            for rule in {it.get("rule_name") for it in ready if it.get("rule_name")}:
                for d in {it.get("business_date") for it in ready}:
                    if f"DQ-{entity}-{rule}-{d}" not in open_ids:
                        continue
                    remediations.append((f"DQ-{entity}-{rule}-{d}", f"{entity} rows failed rule {rule}",
                                         f"Re-insert request #{rid}: {result}", by, req.get("note") or "", "APPLIED"))
                    if entity == "payments" and f"RECON-{d}" in open_ids:   # the money gap of that date is the same rows
                        remediations.append((f"RECON-{d}", f"Revenue gap on {d}", f"Re-insert request #{rid}: {result}", by,
                                             req.get("note") or "", "APPLIED"))
        conn.commit()
    ds.log_remediation(remediations)                                   # DataTrust's own database, after the data commit
    return {"status": status, "result": result, "loaded": loaded}


def review(entity: str, items: list, note: str, by: str) -> dict:
    items = _items(entity, items)
    if not (note or "").strip():
        raise ValueError("Add a note: why will these records not be fixed?")
    with ds.connect(read_only=False) as conn:
        for it in items:
            if not conn.execute("SELECT 1 FROM silver.rejected_records WHERE entity = %s AND record_key = %s",
                                (entity, it["record_key"])).fetchone():
                raise ValueError(f"{it['record_key']} is not held back any more.")
            conn.execute("""INSERT INTO audit.rejected_review (entity, record_key, business_date, rule_name, note, reviewed_by)
                            VALUES (%s, %s, %s, %s, %s, %s)""",
                         (entity, it["record_key"], it["business_date"], it["rule_name"], note.strip()[:500], by))
        conn.commit()
    return {"reviewed": len(items)}


def recheck(entity: str) -> dict:
    """Held-back rows that pass the CURRENT rules without any change (e.g. after a rule was loosened)."""
    if entity not in dq_rules.ENTITIES:
        raise ValueError(f"Unknown table '{entity}'.")
    rows = _recheck(entity, _rules(entity))
    return {"entity": entity, "would_pass": rows, "count": len(rows)}
