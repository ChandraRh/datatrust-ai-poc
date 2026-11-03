"""
Data quality rules as DATA (audit.dq_rules) instead of code, shared by the PySpark silver layer and the web app.

    rule = {entity, rule_name, rule_type, column_name, params, action, active, sort_order}

rule_type     a row FAILS when ...                                     params
  NOT_NULL      the value is empty
  POSITIVE      value <= 0                                               allow_empty
  NON_NEGATIVE  value < 0                                                allow_empty
  RANGE         value < min or value > max                               min, max, allow_empty
  IN_LIST       value is not one of the values                           values [..], allow_empty
  REGEX         value does not match the pattern                         pattern, allow_empty
  CONTAINS      value does not contain the text                          text, allow_empty
  MAX_LENGTH    value is longer than max characters                      max, allow_empty
  NOT_IN_FUTURE date / time is later than now                            allow_empty
Every type except NOT_NULL also fails on an empty value, unless params.allow_empty is true.

action  REJECT  failing rows go to silver.rejected_records (first failing rule wins, in sort_order)
        WARN    rows are loaded; the count is written to audit.error_log as a warning

The rules are checked on the CLEANED silver columns (after trim / upper case / type cast), exactly like the
original hard-coded rules. DEFAULT_RULES below are those originals: they are the seed of audit.dq_rules, and the
silver layer falls back to them whenever the table does not exist or has no rows for an entity.
This module never imports pyspark at the top, so the web app can use it without Spark.
"""
import json
import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

TYPES = {
    "NOT_NULL":      "must not be empty",
    "POSITIVE":      "must be greater than 0",
    "NON_NEGATIVE":  "must be 0 or more",
    "RANGE":         "must be between min and max",
    "IN_LIST":       "must be one of a list of values",
    "REGEX":         "must match a pattern",
    "CONTAINS":      "must contain a text",
    "MAX_LENGTH":    "must not be longer than N characters",
    "NOT_IN_FUTURE": "date / time must not be in the future",
}
ACTIONS = ("REJECT", "WARN")
ENTITIES = ["payments", "payment_orders", "refunds", "settlements", "customers", "products", "categories"]
NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]{2,59}$")

# the rules that were hard-coded in silver_layer.py, unchanged (entity, name, type, column, params, description)
DEFAULT_RULES = [
    ("payments", "PAYMENT_ID_REQUIRED", "NOT_NULL", "payment_id", {}, "Every payment needs a payment_id"),
    ("payments", "CURRENCY_REQUIRED", "NOT_NULL", "currency", {}, "Every payment needs a currency"),
    ("payments", "AMOUNT_POSITIVE", "POSITIVE", "amount", {}, "Payment amount must be more than 0"),
    ("payments", "STATUS_VALID", "IN_LIST", "status", {"values": ["CAPTURED", "FAILED", "REFUNDED", "AUTHORIZED"]},
     "Payment status must be a known gateway status"),
    ("payment_orders", "ORDER_ID_REQUIRED", "NOT_NULL", "order_id", {}, "Every order needs an order_id"),
    ("payment_orders", "CURRENCY_REQUIRED", "NOT_NULL", "currency", {}, "Every order needs a currency"),
    ("payment_orders", "AMOUNT_POSITIVE", "POSITIVE", "amount", {}, "Order amount must be more than 0"),
    ("refunds", "REFUND_ID_REQUIRED", "NOT_NULL", "refund_id", {}, "Every refund needs a refund_id"),
    ("refunds", "AMOUNT_POSITIVE", "POSITIVE", "amount", {}, "Refund amount must be more than 0"),
    ("settlements", "SETTLEMENT_ID_REQUIRED", "NOT_NULL", "settlement_id", {}, "Every settlement needs a settlement_id"),
    ("settlements", "AMOUNT_VALID", "NOT_NULL", "gross_amount", {}, "Settlement gross amount must be a number"),
    ("customers", "CUSTOMER_ID_REQUIRED", "NOT_NULL", "customer_id", {}, "Every customer change needs a customer_id"),
    ("customers", "EMAIL_VALID", "CONTAINS", "email", {"text": "@"}, "Customer e-mail must contain @"),
    ("customers", "CHANGE_TIME_REQUIRED", "NOT_NULL", "valid_from", {}, "Every customer change needs a change time"),
    ("products", "PRODUCT_ID_REQUIRED", "NOT_NULL", "product_id", {}, "Every product needs an id"),
    ("products", "PRICE_POSITIVE", "POSITIVE", "price", {}, "Product price must be more than 0"),
    ("categories", "CATEGORY_ID_REQUIRED", "NOT_NULL", "category_id", {}, "Every category needs an id"),
]


def default_rules() -> list:
    out = []
    for i, (entity, name, rtype, column, params, text) in enumerate(DEFAULT_RULES):
        out.append({"entity": entity, "rule_name": name, "rule_type": rtype, "column_name": column, "params": dict(params),
                    "action": "REJECT", "active": True, "built_in": True, "description": text,
                    "sort_order": 10 * (1 + [r[1] for r in DEFAULT_RULES if r[0] == entity].index(name))})
    return out


def _params(p) -> dict:
    if isinstance(p, str):
        try:
            return json.loads(p or "{}")
        except ValueError:
            return {}
    return dict(p or {})


def _num(v):
    if v is None or v == "":
        return None
    try:
        return Decimal(str(v))
    except (InvalidOperation, ValueError):
        raise ValueError(f"'{v}' is not a number")


# ============================================================================ validation (web: before saving)
def validate(rule: dict, columns: list) -> dict:
    """Check a rule and return it cleaned. Raises ValueError with a plain message."""
    r = {k: rule.get(k) for k in ("entity", "rule_name", "rule_type", "column_name", "action", "description")}
    r["rule_name"] = (r["rule_name"] or "").strip().upper().replace(" ", "_")
    r["rule_type"] = (r["rule_type"] or "").upper()
    r["action"] = (r["action"] or "REJECT").upper()
    r["description"] = (r["description"] or "").strip()[:300]
    p = _params(rule.get("params"))
    if r["entity"] not in ENTITIES:
        raise ValueError(f"Unknown table '{r['entity']}'.")
    if not NAME_RE.match(r["rule_name"]):
        raise ValueError("Rule name: 3-60 characters, capital letters, digits and _ (e.g. CURRENCY_REQUIRED).")
    if r["rule_type"] not in TYPES:
        raise ValueError(f"Unknown rule type '{r['rule_type']}'.")
    if r["action"] not in ACTIONS:
        raise ValueError("Action must be REJECT or WARN.")
    if r["column_name"] not in columns:
        raise ValueError(f"Column '{r['column_name']}' is not in silver.{r['entity']}. Columns: {', '.join(columns)}")
    clean = {}
    if r["rule_type"] != "NOT_NULL":
        clean["allow_empty"] = bool(p.get("allow_empty"))
    t = r["rule_type"]
    if t == "RANGE":
        lo, hi = _num(p.get("min")), _num(p.get("max"))
        if lo is None and hi is None:
            raise ValueError("A range needs a minimum, a maximum or both.")
        if lo is not None and hi is not None and lo > hi:
            raise ValueError("The minimum is bigger than the maximum.")
        clean.update({"min": None if lo is None else float(lo), "max": None if hi is None else float(hi)})
    elif t == "IN_LIST":
        values = p.get("values")
        if isinstance(values, str):
            values = values.split(",")
        values = [str(v).strip() for v in (values or []) if str(v).strip()]
        if not values:
            raise ValueError("Give at least one allowed value.")
        clean["values"] = values[:200]
    elif t == "REGEX":
        pattern = str(p.get("pattern") or "")
        if not pattern:
            raise ValueError("Give a pattern.")
        try:
            re.compile(pattern)
        except re.error as e:
            raise ValueError(f"The pattern is not valid: {e}")
        clean["pattern"] = pattern[:300]
    elif t == "CONTAINS":
        if not str(p.get("text") or ""):
            raise ValueError("Give the text the value must contain.")
        clean["text"] = str(p["text"])[:100]
    elif t == "MAX_LENGTH":
        try:
            n = int(p.get("max"))
            assert n > 0
        except (TypeError, ValueError, AssertionError):
            raise ValueError("Max length must be a whole number above 0.")
        clean["max"] = n
    r["params"] = clean
    r["active"] = bool(rule.get("active", True))
    return r


def _fmt(v):
    try:
        f = float(v)
        return str(int(f)) if f == int(f) else str(f)
    except (TypeError, ValueError):
        return str(v)


def describe(rule: dict) -> str:
    p, col, t = _params(rule.get("params")), rule.get("column_name"), rule.get("rule_type")
    text = {"NOT_NULL": f"{col} must not be empty",
            "POSITIVE": f"{col} must be greater than 0",
            "NON_NEGATIVE": f"{col} must be 0 or more",
            "RANGE": f"{col} must be between {_fmt(p.get('min'))} and {_fmt(p.get('max'))}"
                     if p.get("min") is not None and p.get("max") is not None
                     else f"{col} must be at least {_fmt(p.get('min'))}" if p.get("min") is not None
                     else f"{col} must be at most {_fmt(p.get('max'))}",
            "IN_LIST": f"{col} must be one of {', '.join(p.get('values') or [])}",
            "REGEX": f"{col} must match {p.get('pattern')}",
            "CONTAINS": f"{col} must contain '{p.get('text')}'",
            "MAX_LENGTH": f"{col} must be at most {p.get('max')} characters",
            "NOT_IN_FUTURE": f"{col} must not be in the future"}.get(t, f"{t} on {col}")
    if t != "NOT_NULL" and p.get("allow_empty"):
        text += " (empty is OK)"
    return text


# ============================================================================ Python check (web: re-check a row)
def failing_rules(rules: list, row: dict, action: str = "REJECT") -> list:
    """Names of the active rules (of one action) that this cleaned silver row fails, in rule order."""
    out = []
    for r in sorted(rules, key=lambda x: (x.get("sort_order") or 0, x["rule_name"])):
        if r.get("active", True) and r.get("action", "REJECT") == action and r["column_name"] in row:
            if fails(r, row[r["column_name"]]):
                out.append(r["rule_name"])
    return out


def fails(rule: dict, value) -> bool:
    t, p = rule["rule_type"], _params(rule.get("params"))
    if value is None or (isinstance(value, str) and value == ""):
        return t == "NOT_NULL" or not p.get("allow_empty")
    if t == "NOT_NULL":
        return False
    try:
        if t in ("POSITIVE", "NON_NEGATIVE", "RANGE"):
            v = Decimal(str(value))
            if t == "POSITIVE":
                return v <= 0
            if t == "NON_NEGATIVE":
                return v < 0
            return ((p.get("min") is not None and v < Decimal(str(p["min"]))) or
                    (p.get("max") is not None and v > Decimal(str(p["max"]))))
    except (InvalidOperation, ValueError):
        return True
    if t == "IN_LIST":
        return str(value) not in [str(v) for v in p.get("values") or []]
    if t == "REGEX":
        return re.search(p.get("pattern") or "", str(value)) is None
    if t == "CONTAINS":
        return str(p.get("text") or "") not in str(value)
    if t == "MAX_LENGTH":
        return len(str(value)) > int(p.get("max") or 0)
    if t == "NOT_IN_FUTURE":
        now = datetime.now()
        if isinstance(value, datetime):
            return value > now
        if isinstance(value, date):
            return value > now.date()
        return False
    return False


# ============================================================================ SQL check (web: dry-run on silver)
def sql_failing(rule: dict):
    """-> (SQL condition that is TRUE for a failing row, params). Column name is validated by the caller."""
    t, p, c = rule["rule_type"], _params(rule.get("params")), '"' + rule["column_name"].replace('"', "") + '"'
    empty = f"({c} IS NULL OR {c}::text = '')"
    if t == "NOT_NULL":
        return empty, []
    cond, args = {
        "POSITIVE": (f"{c}::numeric <= 0", []),
        "NON_NEGATIVE": (f"{c}::numeric < 0", []),
        "IN_LIST": (f"NOT ({c}::text = ANY(%s))", [list(p.get("values") or [])]),
        "REGEX": (f"{c}::text !~ %s", [p.get("pattern") or ""]),
        "CONTAINS": (f"strpos({c}::text, %s) = 0", [p.get("text") or ""]),
        "MAX_LENGTH": (f"length({c}::text) > %s", [int(p.get("max") or 0)]),
        "NOT_IN_FUTURE": (f"{c} > now()", []),
    }.get(t, (None, []))
    if t == "RANGE":
        parts = []
        if p.get("min") is not None:
            parts.append(f"{c}::numeric < %s")
            args.append(p["min"])
        if p.get("max") is not None:
            parts.append(f"{c}::numeric > %s")
            args.append(p["max"])
        cond = " OR ".join(parts) or "FALSE"
    if p.get("allow_empty"):
        return f"(NOT {empty} AND ({cond}))", args
    return f"({empty} OR ({cond}))", args


# ============================================================================ PySpark (silver layer)
def spark_failing(rule: dict):
    """Column expression that is TRUE for a failing row (never NULL)."""
    from pyspark.sql import functions as F
    t, p, col = rule["rule_type"], _params(rule.get("params")), F.col(rule["column_name"])
    empty = col.isNull() | (col.cast("string") == "")
    if t == "NOT_NULL":
        return F.coalesce(empty, F.lit(True))
    if t == "POSITIVE":
        cond = col <= 0
    elif t == "NON_NEGATIVE":
        cond = col < 0
    elif t == "RANGE":
        cond = F.lit(False)
        if p.get("min") is not None:
            cond = cond | (col < F.lit(p["min"]))
        if p.get("max") is not None:
            cond = cond | (col > F.lit(p["max"]))
    elif t == "IN_LIST":
        cond = ~col.cast("string").isin(*[str(v) for v in p.get("values") or []])
    elif t == "REGEX":
        cond = ~col.cast("string").rlike(p.get("pattern") or "")
    elif t == "CONTAINS":
        cond = ~col.cast("string").contains(str(p.get("text") or ""))
    elif t == "MAX_LENGTH":
        cond = F.length(col.cast("string")) > int(p.get("max") or 0)
    elif t == "NOT_IN_FUTURE":
        cond = col > F.current_timestamp()
    else:
        cond = F.lit(False)
    cond = F.coalesce(cond, F.lit(True))                        # a value that cannot be compared fails
    if p.get("allow_empty"):
        return F.when(empty, F.lit(False)).otherwise(cond)
    return F.when(empty, F.lit(True)).otherwise(cond)


def load(conn) -> dict:
    """{entity: [rules]} from audit.dq_rules (active and inactive). {} when the table does not exist.
    An entity with rows in the table uses ONLY its table rules; an entity without rows keeps the code defaults."""
    cur = conn.execute("SELECT to_regclass('audit.dq_rules') IS NOT NULL")
    if not cur.fetchone()[0]:
        return {}
    out = {}
    for entity, name, rtype, column, params, action, active, sort_order in conn.execute(
            """SELECT entity, rule_name, rule_type, column_name, params::text, action, active, sort_order
               FROM audit.dq_rules ORDER BY entity, sort_order, rule_name""").fetchall():
        out.setdefault(entity, []).append({"entity": entity, "rule_name": name, "rule_type": rtype, "column_name": column,
                                           "params": _params(params), "action": action, "active": active,
                                           "sort_order": sort_order})
    return out
