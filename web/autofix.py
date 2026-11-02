"""
Auto-fix with approval: repair rows that silver rejected, using evidence from the company's own data.

    proposal(incident_id)        what can be fixed, row by row, with the evidence for every value (nothing changes)
    apply(incident_id, by)       after an admin approves: ONE transaction that
                                   1. records every correction in audit.dq_corrections (who, when, old -> new, evidence)
                                   2. loads the corrected rows into silver (same cleaning as the PySpark silver layer)
                                   3. removes them from silver.rejected_records
                                   4. logs the remediation (audit.remediation_log), so Verify shows the result
The pipeline reads audit.dq_corrections too (silver_layer.apply_corrections), so a later re-run gives the same result.

Fix rules (only where the evidence is unambiguous; anything else stays for the source team):
  payments       CURRENCY_REQUIRED  currency of the payment's own order (silver.payment_orders); else the currency of
                                    every other payment of that customer, if they all use one currency
  payment_orders AMOUNT_POSITIVE    a negative amount whose absolute value equals amount_paid (+ amount_due) = sign error
"""
import json
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

import data_service as ds

FIXES = {("payments", "CURRENCY_REQUIRED"), ("payment_orders", "AMOUNT_POSITIVE")}
SILVER_KEY = {"payments": ("silver.payments", "payment_id"), "payment_orders": ("silver.payment_orders", "order_id")}


# ============================================================================ the same cleaning as silver_layer.py
def _clean(v, upper=False):
    if v is None:
        return None
    s = str(v).strip()
    return None if s == "" else (s.upper() if upper else s)


def _dec(v):
    try:
        return Decimal(str(v).strip()).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP) if _clean(v) else None
    except (InvalidOperation, ValueError):
        return None


def _int(v):
    try:
        return int(str(v).strip()) if _clean(v) else None
    except ValueError:
        return None


def _ts(v):
    s = _clean(v)
    if not s:
        return None
    s = s.replace("T", " ").replace("Z", " ").strip()
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def _date(v):
    try:
        return date.fromisoformat(_clean(v)[:10]) if _clean(v) else None
    except ValueError:
        return None


def silver_row(entity: str, raw: dict) -> dict:
    """Raw (bronze) values -> the silver row, exactly as transform_<entity> in silver_layer.py builds it."""
    if entity == "payments":
        contact = _clean(raw.get("contact"))
        return {
            "payment_id": _clean(raw.get("payment_id")), "order_id": _clean(raw.get("order_id")),
            "customer_id": _clean(raw.get("customer_id")), "business_date": _date(raw.get("business_date")),
            "created_at": _ts(raw.get("created_at")), "captured_at": _ts(raw.get("captured_at")),
            "amount": _dec(raw.get("amount")), "currency": _clean(raw.get("currency"), True),
            "status": _clean(raw.get("status"), True), "method": _clean(raw.get("method"), True),
            "card_network": _clean(raw.get("card_network")), "bank": _clean(raw.get("bank")),
            "wallet": _clean(raw.get("wallet")),
            "email": _clean(raw.get("email")).lower() if _clean(raw.get("email")) else None,
            "contact_masked": ("******" + contact[-4:]) if contact else None,                 # PII masked
            "fee": _dec(raw.get("fee")), "tax": _dec(raw.get("tax")), "amount_refunded": _dec(raw.get("amount_refunded")),
            "error_code": _clean(raw.get("error_code")), "error_reason": _clean(raw.get("error_reason")),
            "batch_id": raw.get("batch_id"), "loaded_at": datetime.now(),
        }
    if entity == "payment_orders":
        return {
            "order_id": _clean(raw.get("order_id")), "receipt": _clean(raw.get("receipt")),
            "customer_id": _clean(raw.get("customer_id")), "business_date": _date(raw.get("business_date")),
            "created_at": _ts(raw.get("created_at")), "amount": _dec(raw.get("amount")),
            "amount_paid": _dec(raw.get("amount_paid")), "amount_due": _dec(raw.get("amount_due")),
            "currency": _clean(raw.get("currency"), True), "payment_status": _clean(raw.get("status"), True),
            "attempts": _int(raw.get("attempts")), "channel": _clean(raw.get("channel"), True),
            "order_status": _clean(raw.get("order_status"), True), "batch_id": raw.get("batch_id"), "loaded_at": datetime.now(),
        }
    raise ValueError(f"no auto-fix for {entity}")


def passes_rules(entity: str, r: dict) -> bool:
    """The silver rules of silver_layer.py (a fixed row must pass all of them, not only the one it failed)."""
    if entity == "payments":
        return bool(r["payment_id"] and r["currency"] and r["amount"] is not None and r["amount"] > 0
                    and r["status"] in ("CAPTURED", "FAILED", "REFUNDED", "AUTHORIZED"))
    if entity == "payment_orders":
        return bool(r["order_id"] and r["currency"] and r["amount"] is not None and r["amount"] > 0)
    return False


# ============================================================================ which rows, and the evidence
def _target(incident_id: str):
    """incident -> (entity, rule, business_date, incident ids this fix resolves)."""
    inc = next((i for i in ds.incidents() if i["id"] == incident_id), None)
    if not inc:
        return None, None
    if inc["type"] == "REJECTED_ROWS":
        m = inc["metrics"]
        ids = [inc["id"]]
        if m["entity"] == "payments":                       # the money gap of that date is the same rows
            ids += [i["id"] for i in ds.incidents() if i["type"] == "SETTLEMENT_MISMATCH" and i["business_date"] == m["business_date"]]
        return inc, (m["entity"], m["rule_name"], m["business_date"], ids)
    if inc["type"] == "SETTLEMENT_MISMATCH":
        d = inc["business_date"]
        rules = (inc.get("rejected_explained") or {}).get("rules") or ""
        rule = "CURRENCY_REQUIRED" if "CURRENCY_REQUIRED" in rules else rules.split(",")[0].strip()
        ids = [inc["id"]] + [i["id"] for i in ds.incidents() if i["type"] == "REJECTED_ROWS"
                             and i["metrics"]["entity"] == "payments" and i["business_date"] == d]
        return inc, ("payments", rule, d, ids)
    return inc, None


def _evidence(entity: str, rule: str, raw: dict):
    """-> (column, proposed raw value, evidence text) or (None, None, reason it cannot be fixed safely)."""
    if (entity, rule) == ("payments", "CURRENCY_REQUIRED"):
        order = ds.one("SELECT currency FROM silver.payment_orders WHERE order_id = %(o)s", {"o": _clean(raw.get("order_id"))})
        if order.get("currency"):
            return "currency", order["currency"], f"its order {raw.get('order_id')} in silver.payment_orders is {order['currency']}"
        cur = ds.rows("""SELECT currency, COUNT(*) AS n FROM silver.payments WHERE customer_id = %(c)s AND currency IS NOT NULL
                         GROUP BY 1""", {"c": _clean(raw.get("customer_id"))})
        if len(cur) == 1:
            return "currency", cur[0]["currency"], f"all {cur[0]['n']} other payments of customer {raw.get('customer_id')} are {cur[0]['currency']}"
        return None, None, "no order and no single customer currency to take it from"
    if (entity, rule) == ("payment_orders", "AMOUNT_POSITIVE"):
        amount, paid, due = _dec(raw.get("amount")), _dec(raw.get("amount_paid")), _dec(raw.get("amount_due")) or Decimal("0")
        if amount is not None and amount < 0 and paid is not None and -amount == paid + due:
            return "amount", str(-amount), f"sign error: amount {amount} while amount_paid {paid} + amount_due {due} = {paid + due}"
        return None, None, "the amount does not match amount_paid, so the correct value is not known"
    return None, None, "no automatic fix for this rule"


def proposal(incident_id: str) -> dict:
    """What would be fixed and why. Read-only."""
    inc, target = _target(incident_id)
    if not inc:
        return {"available": False, "reason": "This incident is no longer open."}
    if not target:
        return {"available": False, "reason": "No automatic fix exists for this kind of incident."}
    entity, rule, day, resolves = target
    if (entity, rule) not in FIXES:
        return {"available": False, "reason": f"No safe automatic fix for rule {rule}: the source team has to correct it."}
    rows, skipped = [], []
    for r in ds.rows("""SELECT record_key, raw_record FROM silver.rejected_records
                        WHERE entity = %(e)s AND rule_name = %(r)s AND business_date = %(d)s ORDER BY record_key""",
                     {"e": entity, "r": rule, "d": day}):
        raw = json.loads(r["raw_record"])
        col, value, why = _evidence(entity, rule, raw)
        fixed = dict(raw, **{col: value}) if col else None
        if col and passes_rules(entity, silver_row(entity, fixed)):
            rows.append({"record_key": r["record_key"], "column": col, "current": raw.get(col), "proposed": value,
                         "evidence": why, "amount": str(_dec(raw.get("amount")) or ""), "status": raw.get("status")})
        else:
            skipped.append({"record_key": r["record_key"], "reason": why if not col else "still fails another rule after the fix"})
    silver_table, key = SILVER_KEY[entity]
    effect = {}
    if entity == "payments":
        rec = next((x for x in ds.settlement_recon(day)), None)
        if rec:
            captured = sum(1 for x in rows if (x["status"] or "").upper() == "CAPTURED")
            amount = sum(Decimal(x["amount"] or "0") for x in rows if (x["status"] or "").upper() == "CAPTURED")
            effect = {"settled_payments": rec["settled_payments"], "silver_now": rec["silver_payments"],
                      "silver_after": rec["silver_payments"] + captured,
                      "amount_gap_now": rec["amount_difference"], "amount_gap_after": round(float(rec["amount_difference"]) - float(amount), 2)}
    col = rows[0]["column"] if rows else "?"
    sql = (f"-- 1. record {len(rows)} correction(s) with evidence\nINSERT INTO audit.dq_corrections (entity, record_key, column_name, old_value, new_value, evidence, approved_by, ...) VALUES ...;\n"
           f"-- 2. load the corrected rows into silver (same cleaning as the silver layer)\nINSERT INTO {silver_table} (...) VALUES ... ON CONFLICT ({key}) DO UPDATE ...;  -- {len(rows)} row(s), {col} filled in\n"
           f"-- 3. take them out of the rejected list\nDELETE FROM silver.rejected_records WHERE entity = '{entity}' AND rule_name = '{rule}' AND business_date = '{day}' AND record_key IN (...);\n"
           f"-- 4. log the remediation\nINSERT INTO audit.remediation_log (...) VALUES (...);  -- all in ONE transaction")
    title = (f"Fill in {col} for {len(rows)} {entity} row(s) of {day} from {'their orders' if rule == 'CURRENCY_REQUIRED' else 'amount_paid'}"
             if rows else "Nothing can be fixed automatically")
    return {"available": bool(rows), "incident_id": incident_id, "entity": entity, "rule": rule, "business_date": day,
            "title": title, "rows": rows, "skipped": skipped, "effect": effect, "sql_preview": sql, "resolves": resolves,
            "reason": "" if rows else "None of the rows has clear evidence for a fix: the source team has to correct them."}


# ============================================================================ apply (after approval)
def ensure_tables(conn) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS audit.dq_corrections (
            correction_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            entity        TEXT NOT NULL,
            record_key    TEXT NOT NULL,
            business_date DATE,
            column_name   TEXT NOT NULL,
            old_value     TEXT,
            new_value     TEXT,
            rule_name     TEXT,
            evidence      TEXT,
            incident_id   TEXT,
            approved_by   TEXT,
            applied_at    TIMESTAMP DEFAULT now(),
            active        BOOLEAN DEFAULT TRUE)""")


def apply(incident_id: str, approved_by: str) -> dict:
    """Run the approved fix in ONE transaction (all rows or none). Re-checks everything first."""
    try:
        return _apply(incident_id, approved_by)
    except ValueError:
        raise
    except Exception as e:                                      # e.g. silver table changed by someone: readable message
        raise ValueError(f"The fix was not applied and nothing was changed (it runs in one transaction): "
                         f"{type(e).__name__}: {str(e).splitlines()[0]}")


def _apply(incident_id: str, approved_by: str) -> dict:
    p = proposal(incident_id)
    if not p["available"]:
        raise ValueError(p["reason"])
    entity, rule, day = p["entity"], p["rule"], p["business_date"]
    silver_table, key = SILVER_KEY[entity]
    with ds.connect(read_only=False) as conn:
        ensure_tables(conn)
        fixed = 0
        for r in p["rows"]:
            rec = conn.execute("""SELECT raw_record FROM silver.rejected_records
                                  WHERE entity = %s AND rule_name = %s AND business_date = %s AND record_key = %s""",
                               (entity, rule, day, r["record_key"])).fetchone()
            if not rec:
                continue                                        # already fixed by someone else in the meantime
            raw = json.loads(rec["raw_record"])
            row = silver_row(entity, dict(raw, **{r["column"]: r["proposed"]}))
            cols = list(row)
            conn.execute(f"""INSERT INTO {silver_table} ({", ".join(cols)}) VALUES ({", ".join(["%s"] * len(cols))})
                             ON CONFLICT ({key}) DO UPDATE SET {", ".join(f"{c} = EXCLUDED.{c}" for c in cols if c != key)}""",
                         [row[c] for c in cols])
            conn.execute("""INSERT INTO audit.dq_corrections (entity, record_key, business_date, column_name, old_value, new_value,
                                                              rule_name, evidence, incident_id, approved_by)
                            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                         (entity, r["record_key"], day, r["column"], r["current"], r["proposed"], rule, r["evidence"],
                          incident_id, approved_by))
            conn.execute("""DELETE FROM silver.rejected_records
                            WHERE entity = %s AND rule_name = %s AND business_date = %s AND record_key = %s""",
                         (entity, rule, day, r["record_key"]))
            fixed += 1
        if not fixed:
            raise ValueError("Nothing left to fix: the rows were already corrected.")
        source = {"CURRENCY_REQUIRED": "each payment's own order (silver.payment_orders)",
                  "AMOUNT_POSITIVE": "amount_paid (sign error)"}.get(rule, "evidence in the data")
        action = (f"Auto-fix applied: {fixed} {entity} row(s) of {day} loaded into silver, {p['rows'][0]['column']} taken from "
                  f"{source}; every change recorded in audit.dq_corrections")
        conn.execute("""CREATE TABLE IF NOT EXISTS audit.remediation_log (
                            remediation_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY, incident_id TEXT, incident_title TEXT,
                            actions TEXT, approved_by TEXT, note TEXT, status TEXT DEFAULT 'APPROVED', approved_at TIMESTAMP DEFAULT now())""")
        titles = {i["id"]: i["title"] for i in ds.incidents()}
        for iid in p["resolves"]:
            conn.execute("""INSERT INTO audit.remediation_log (incident_id, incident_title, actions, approved_by, note, status)
                            VALUES (%s, %s, %s, %s, %s, 'APPLIED')""",
                         (iid, titles.get(iid, iid), action, approved_by, f"{fixed} correction(s) in audit.dq_corrections"))
        conn.commit()
    return {"fixed": fixed, "resolves": p["resolves"], "action": action}


def corrections(limit: int = 200) -> list:
    if not ds.table_exists("audit.dq_corrections"):
        return []
    return ds.rows("""SELECT applied_at, entity, record_key, column_name, old_value, new_value, evidence, approved_by, incident_id
                      FROM audit.dq_corrections WHERE active ORDER BY correction_id DESC LIMIT %(n)s""", {"n": limit})
