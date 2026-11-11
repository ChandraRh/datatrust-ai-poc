"""
Everything the DataTrust web page shows, computed live from PostgreSQL (gold views + audit tables).
Nothing is hard-coded: run the pipeline again and these numbers change.

    trust_score()      0-100 score with the reasons behind it
    incidents()        open issues found in the data (settlement mismatch, rejected rows, duplicates, failed steps)
    lineage()          source file -> bronze -> silver -> gold, with the rows lost or held back at each hop
    revenue(...)       revenue for any period, optionally by day / method / region / segment
    pipeline_status()  latest batch, every step and its status
"""
import csv
import sys
import threading
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import psycopg
from psycopg.rows import dict_row

def _find_project_root() -> Path:
    """The folder that has config.py: normally the parent of web/, also found if web/ is nested deeper."""
    for folder in Path(__file__).resolve().parents:
        if (folder / "config.py").exists():
            return folder
    raise SystemExit(
        "config.py not found. The 'web' folder must be inside your project folder, next to config.py:\n"
        "    VSCode_DataTrustAI\\config.py\n    VSCode_DataTrustAI\\web\\server.py\n"
        f"but server.py is here: {Path(__file__).resolve().parent}")


sys.path.insert(0, str(_find_project_root()))                    # project root: config.py, .env
import config  # noqa: E402

# entity -> (source file, bronze table, silver table, business key, SCD type)
ENTITIES = {
    "payment_orders": ("payment_orders.csv", "bronze.payment_orders", "silver.payment_orders", "order_id", 1),
    "payments":       ("payments.csv",       "bronze.payments",       "silver.payments",       "payment_id", 1),
    "refunds":        ("refunds.csv",        "bronze.refunds",        "silver.refunds",        "refund_id", 1),
    "settlements":    ("settlements.csv",    "bronze.settlements",    "silver.settlements",    "settlement_id", 1),
    "customers":      ("customers.csv",      "bronze.customers",      "silver.customers",      "customer_id", 2),
    "products":       ("products.csv",       "bronze.products",       "silver.products",       "product_id", 2),
    "categories":     ("categories.csv",     "bronze.categories",     "silver.categories",     "category_id", 1),
}

# Trust score rules (shown on the page, so the score is explainable)
PENALTY_SETTLEMENT_MISMATCH = 15        # per business date where settlement != silver (max 45)
PENALTY_PIPELINE_FAILED = 30            # latest run has a FAILED / CANCELLED / SKIPPED layer
PENALTY_STALE = 10                      # last successful run older than FRESHNESS_HOURS
FRESHNESS_HOURS = 24
HEALTHY, ATTENTION = 90, 75             # >= 90 Healthy, >= 75 Needs attention, below = At risk


# ============================================================================ database helpers
def connect(read_only: bool = True):
    conn = psycopg.connect(sslmode=config.PG_SSLMODE, host=config.PG_HOST, port=config.PG_PORT, dbname=config.PG_DATABASE,
                           user=config.PG_USER, password=config.PG_PASSWORD, row_factory=dict_row,
                           connect_timeout=5)
    if read_only:
        conn.execute("SET default_transaction_read_only = on")
        conn.execute("SET statement_timeout = '10s'")
    return conn


def plain(value):
    """Decimal / date -> JSON friendly."""
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (date, datetime)):
        return value.isoformat(sep=" ") if isinstance(value, datetime) else value.isoformat()
    return value


_local = threading.local()


def _reader():
    """One read-only connection per worker thread, reused for every query (opening one per query was the slowest
    part of a page refresh). Autocommit: every query sees the latest committed data. Reconnects when broken."""
    conn = getattr(_local, "conn", None)
    if conn is None or conn.closed or conn.broken:
        conn = psycopg.connect(sslmode=config.PG_SSLMODE, host=config.PG_HOST, port=config.PG_PORT, dbname=config.PG_DATABASE,
                               user=config.PG_USER, password=config.PG_PASSWORD, row_factory=dict_row,
                               connect_timeout=5, autocommit=True)
        conn.execute("SET default_transaction_read_only = on")
        conn.execute("SET statement_timeout = '10s'")
        _local.conn = conn
    return conn


def rows(sql: str, params=None) -> list:
    for attempt in (1, 2):
        try:
            return [{k: plain(v) for k, v in r.items()} for r in _reader().execute(sql, params).fetchall()]
        except psycopg.OperationalError:                         # database restarted / connection dropped: retry once
            _local.conn = None
            if attempt == 2:
                raise


def one(sql: str, params=None) -> dict:
    result = rows(sql, params)
    return result[0] if result else {}


def table_exists(name: str) -> bool:
    return bool(one("SELECT to_regclass(%(n)s) IS NOT NULL AS ok", {"n": name}).get("ok"))


def ensure_web_tables() -> None:
    """Remediation approvals made on the web page (created on first start, never dropped)."""
    with connect(read_only=False) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS audit.remediation_log (
                remediation_id  BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                incident_id     TEXT,
                incident_title  TEXT,
                actions         TEXT,
                approved_by     TEXT,
                note            TEXT,
                status          TEXT DEFAULT 'APPROVED',
                approved_at     TIMESTAMP DEFAULT now())""")
        conn.commit()


# ============================================================================ freshness / version
def data_version() -> dict:
    """Changes whenever the pipeline writes something: the page polls this and refreshes itself."""
    v = one("""SELECT (SELECT MAX(ended_at) FROM audit.batch_control)::text AS last_run,
                      (SELECT COUNT(*) FROM audit.batch_control)            AS runs,
                      (SELECT COUNT(*) FROM audit.error_log)                AS log_rows""")
    if table_exists("audit.remediation_log"):
        v["approvals"] = one("SELECT COUNT(*) AS n FROM audit.remediation_log")["n"]
    v["signature"] = f"{v.get('last_run')}|{v.get('runs')}|{v.get('log_rows')}|{v.get('approvals')}"
    return v


def data_coverage() -> dict:
    """First / last business date that has data in gold."""
    return one("""SELECT MIN(business_date)::text AS first_date, MAX(business_date)::text AS last_date,
                         COUNT(*) AS days FROM gold.daily_revenue""")


def latest_batch(layer: str = "BRONZE") -> dict:
    return one("""SELECT batch_id, business_date::text AS business_date, status, load_mode,
                         started_at, ended_at, rows_read, rows_written, rows_rejected, message
                  FROM audit.batch_control WHERE layer = %(l)s ORDER BY ended_at DESC LIMIT 1""", {"l": layer})


# ============================================================================ pipeline status
def pipeline_status() -> dict:
    bronze = latest_batch("BRONZE")
    batch_id = bronze.get("batch_id")
    layers = rows("""SELECT layer, status, load_mode, rows_read, rows_written, rows_rejected, message,
                            started_at, ended_at
                     FROM audit.batch_control WHERE batch_id = %(b)s ORDER BY started_at""", {"b": batch_id})
    steps = []
    if batch_id and table_exists("audit.step_log"):
        steps = rows("""SELECT step_id, layer, pipeline_id, status, rows_in, rows_out, rows_rejected, duration_sec,
                               COALESCE(error_type || ': ' || error_message, message) AS message, ended_at
                        FROM audit.step_log WHERE batch_id = %(b)s ORDER BY step_id""", {"b": batch_id})
    errors = rows("""SELECT error_id, logged_at, batch_id, business_date::text AS business_date, layer,
                            pipeline_id, status, error_type, sql_state, error_message
                     FROM audit.error_log ORDER BY error_id DESC LIMIT 50""")
    last_success = one("""SELECT MAX(ended_at) AS t FROM audit.batch_control
                          WHERE layer = 'GOLD' AND status = 'SUCCESS'""").get("t")
    return {"batch": bronze, "layers": layers, "steps": steps, "errors": errors, "last_success": last_success,
            "history": rows("""SELECT batch_id, COALESCE(MAX(business_date)::text, 'ALL DATES') AS business_date,
                                      MAX(load_mode) AS load_mode,
                                      MAX(CASE WHEN layer='BRONZE' THEN status END) AS bronze,
                                      MAX(CASE WHEN layer='SILVER' THEN status END) AS silver,
                                      MAX(CASE WHEN layer='GOLD'   THEN status END) AS gold,
                                      MAX(CASE WHEN layer='SILVER' THEN rows_rejected END) AS rejected,
                                      MAX(ended_at) AS finished_at
                               FROM audit.batch_control GROUP BY batch_id
                               ORDER BY MAX(ended_at) DESC LIMIT 15""")}


# ============================================================================ lineage source -> target
def _source_rows(file_name: str, business_date: str = None):
    """Rows in the source CSV (only for local source files). None if not available."""
    path = config.LOCAL_SOURCE_DIR / file_name
    if config.SOURCE_MODE != "local" or not path.exists():
        return None
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.reader(f)
        header = [h.strip().lower() for h in next(reader, [])]
        if business_date and "business_date" in header:
            i = header.index("business_date")
            return sum(1 for r in reader if len(r) > i and r[i] == business_date)
        return sum(1 for _ in reader)


def _gold_views_using(silver_table: str) -> list:
    """Real lineage from PostgreSQL's catalog: which gold views read this silver table."""
    schema, table = silver_table.split(".")
    return [r["view_name"] for r in rows("""
        SELECT DISTINCT view_name FROM information_schema.view_table_usage
        WHERE view_schema = 'gold' AND table_schema = %(s)s AND table_name = %(t)s ORDER BY 1""",
        {"s": schema, "t": table})]


def lineage() -> dict:
    """For the latest batch: rows at every hop and where any were lost or held back."""
    batch = latest_batch("BRONZE")
    batch_id, day = batch.get("batch_id"), batch.get("business_date")
    silver_steps = {}
    if batch_id and table_exists("audit.step_log"):
        for s in rows("""SELECT pipeline_id, status, rows_in, rows_out, rows_rejected, message
                         FROM audit.step_log WHERE batch_id = %(b)s AND layer = 'SILVER'""", {"b": batch_id}):
            silver_steps[s["pipeline_id"].replace("PL_SILVER_", "").lower()] = s

    result = []
    for entity, (file_name, bronze_t, silver_t, key, scd) in ENTITIES.items():
        source_label = file_name
        if batch.get("load_mode") == "STREAM":                       # Kafka batch: source = messages read
            source = None
            if table_exists("audit.kafka_ingest_log"):
                source = one("""SELECT SUM(messages) AS n FROM audit.kafka_ingest_log
                                WHERE batch_id = %(b)s AND bronze_table = %(t)s""", {"b": batch_id, "t": bronze_t})["n"]
            source_label = f"kafka topic ({entity})" if source is not None else "not streamed (file source)"
            source = source or 0
        else:
            source = _source_rows(file_name, day)
        bronze_rows = one(f"SELECT COUNT(*) AS n FROM {bronze_t} WHERE batch_id = %(b)s",
                          {"b": batch_id}).get("n", 0) if table_exists(bronze_t) else 0
        step = silver_steps.get(entity, {})
        rejected = step.get("rows_rejected")
        if rejected is None:
            rejected = one("""SELECT COUNT(*) AS n FROM silver.rejected_records
                              WHERE entity = %(e)s AND batch_id = %(b)s""", {"e": entity, "b": batch_id})["n"]
        loaded = step.get("rows_out")
        if loaded is None:
            loaded = bronze_rows - rejected
        duplicates = max(bronze_rows - loaded - rejected, 0) if scd == 1 else 0
        unchanged = 0
        if scd == 2 and "unchanged" in (step.get("message") or ""):
            try:
                unchanged = int((step["message"].split(" unchanged")[0]).split(", ")[-1])
            except ValueError:
                unchanged = 0
        silver_total = one(f"SELECT COUNT(*) AS n FROM {silver_t}" +
                           (" WHERE is_current" if scd == 2 else ""))["n"]
        source_gap = (source - bronze_rows) if source is not None else None
        unexplained = bronze_rows - loaded - rejected - duplicates
        status = "OK"
        if source_gap or unexplained:
            status = "MISSING"
        elif rejected:
            status = "REJECTED_ROWS"
        elif duplicates:
            status = "DUPLICATES_REMOVED"
        result.append({
            "entity": entity, "source_file": source_label, "source_rows": source,
            "bronze_table": bronze_t, "bronze_rows": bronze_rows,
            "silver_table": silver_t, "silver_loaded": loaded, "rejected": rejected, "duplicates": duplicates,
            "scd2_unchanged": unchanged, "silver_rows_now": silver_total,
            "source_to_bronze_gap": source_gap, "unexplained_loss": unexplained,
            "gold_views": _gold_views_using(silver_t), "status": status, "scd": scd,
        })
    return {"batch_id": batch_id, "business_date": day or "ALL DATES", "load_mode": batch.get("load_mode"),
            "entities": result,
            "settlement": settlement_recon()}


def settlement_recon(business_date: str = None) -> list:
    """External target check: what the payment gateway settled vs what reached silver/gold."""
    return rows("""SELECT business_date::text AS business_date, settled_payments, silver_payments, payment_difference,
                          settled_amount, silver_amount, amount_difference, rejected_payments, recon_status
                   FROM gold.settlement_reconciliation
                   WHERE %(d)s::date IS NULL OR business_date = %(d)s::date
                   ORDER BY business_date""", {"d": business_date})


# ============================================================================ incidents
def incidents() -> list:
    """Every open issue the data shows right now, most severe first. An issue disappears by itself
    when a later pipeline run no longer shows it (that is how 'verified resolved' works)."""
    found = []
    approvals = {}
    if table_exists("audit.remediation_log"):
        for a in rows("""SELECT DISTINCT ON (incident_id) incident_id, approved_by, approved_at, status
                         FROM audit.remediation_log ORDER BY incident_id, approved_at DESC"""):
            approvals[a["incident_id"]] = a

    # 1. settlement vs silver mismatch (money)
    for r in settlement_recon():
        if r["recon_status"] != "MISMATCH":
            continue
        rej = one("""SELECT COUNT(*) AS n, COALESCE(SUM((raw_record::jsonb->>'amount')::numeric), 0) AS amount,
                            string_agg(DISTINCT rule_name, ', ') AS rules
                     FROM silver.rejected_records
                     WHERE entity = 'payments' AND business_date = %(d)s
                       AND (upper(trim(raw_record::jsonb->>'status')) = 'CAPTURED'
                            OR rule_name = 'STATUS_VALID')""", {"d": r["business_date"]})   # a status typo was settled too
        found.append({
            "id": f"RECON-{r['business_date']}", "severity": "HIGH", "type": "SETTLEMENT_MISMATCH",
            "title": f"Revenue gap on {r['business_date']}: settlement does not match silver",
            "business_date": r["business_date"],
            "summary": (f"Gateway settled {r['settled_payments']:,} payments / {inr(r['settled_amount'])}; "
                        f"silver has {r['silver_payments']:,} / {inr(r['silver_amount'])}. "
                        f"Gap: {r['payment_difference']} payments, {inr(r['amount_difference'])}."),
            "metrics": r, "rejected_explained": rej,
        })

    # 2. rejected rows per entity / rule / date
    for r in rows("""SELECT entity, rule_name, business_date::text AS business_date, COUNT(*) AS n,
                            MIN(batch_id) AS batch_id
                     FROM silver.rejected_records GROUP BY 1, 2, 3 ORDER BY 1, 3"""):
        found.append({
            "id": f"DQ-{r['entity']}-{r['rule_name']}-{r['business_date']}", "severity": "MEDIUM",
            "type": "REJECTED_ROWS", "business_date": r["business_date"],
            "title": f"{r['n']} {r['entity']} row(s) failed rule {r['rule_name']}",
            "summary": f"Held back in silver.rejected_records (business date {r['business_date']}); "
                       f"they are not in silver or gold.",
            "metrics": r,
        })

    # 3. duplicates removed / unexplained loss / failed steps of the latest batch
    lin = lineage()
    for e in lin["entities"]:
        if e["duplicates"]:
            found.append({
                "id": f"DUP-{e['entity']}", "severity": "LOW", "type": "DUPLICATES",
                "title": f"{e['duplicates']} duplicate {e['entity']} row(s) in the source",
                "summary": f"{e['source_file']} delivered the same {e['entity']} more than once; silver kept "
                           f"the latest copy, so nothing is double counted.", "metrics": e,
                "business_date": lin["business_date"]})
        if e["source_to_bronze_gap"] or e["unexplained_loss"]:
            found.append({
                "id": f"LOSS-{e['entity']}", "severity": "HIGH", "type": "DATA_LOSS",
                "title": f"{e['entity']}: rows missing between source and target",
                "summary": f"source {e['source_rows']}, bronze {e['bronze_rows']}, silver loaded "
                           f"{e['silver_loaded']}, rejected {e['rejected']}, duplicates {e['duplicates']}.",
                "metrics": e, "business_date": lin["business_date"]})

    status = pipeline_status()
    for layer in status["layers"]:
        if layer["status"] in ("FAILED", "CANCELLED", "SKIPPED"):
            failed_steps = [s for s in status["steps"] if s["status"] in ("FAILED", "CANCELLED")]
            detail = failed_steps[0]["message"] if failed_steps else layer.get("message")
            found.append({
                "id": f"RUN-{status['batch']['batch_id']}-{layer['layer']}",
                "severity": "CRITICAL" if layer["status"] == "FAILED" else "HIGH", "type": "PIPELINE_FAILURE",
                "title": f"{layer['layer']} layer {layer['status']} in batch {status['batch']['batch_id']}",
                "summary": detail or "", "metrics": layer, "business_date": status["batch"].get("business_date")})

    # 4. breaking schema changes on bronze / silver / gold (schema-change guard, needs change tracking)
    try:
        import schema_guard
        found += schema_guard.incidents(approvals)
    except Exception as e:                                          # the guard must never break the other checks
        print(f"!! schema guard skipped: {type(e).__name__}: {e}")

    order = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3}
    for i in found:
        i["approval"] = approvals.get(i["id"])
    return sorted(found, key=lambda i: (order[i["severity"]], i["id"]))


def resolved_incidents() -> list:
    """Approved incidents that the latest data no longer shows = verified resolved."""
    if not table_exists("audit.remediation_log"):
        return []
    open_ids = {i["id"] for i in incidents()}
    return [r for r in rows("""SELECT DISTINCT ON (incident_id) incident_id, incident_title, actions, approved_by,
                                      approved_at FROM audit.remediation_log ORDER BY incident_id, approved_at DESC""")
            if r["incident_id"] not in open_ids]


def approve(incident_id: str, title: str, actions: list, approved_by: str, note: str = "") -> None:
    ensure_web_tables()
    with connect(read_only=False) as conn:
        conn.execute("""INSERT INTO audit.remediation_log (incident_id, incident_title, actions, approved_by, note)
                        VALUES (%s, %s, %s, %s, %s)""", (incident_id, title, "; ".join(actions), approved_by, note))
        conn.commit()


# ============================================================================ investigation (evidence)
def investigate(incident_id: str) -> dict:
    """Evidence chain for one incident, built only from queries (each check says where it came from)."""
    inc = next((i for i in incidents() if i["id"] == incident_id), None)
    if inc is None:
        return {"found": False}
    checks, chain, root, sample = [], [], "", []

    if inc["type"] == "SETTLEMENT_MISMATCH":
        m, rej = inc["metrics"], inc["rejected_explained"]
        d = inc["business_date"]
        count_match = rej["n"] == m["payment_difference"]
        amount_match = abs(float(rej["amount"]) - float(m["amount_difference"])) < 0.01
        bronze_payments = one("""SELECT COUNT(*) AS n FROM bronze.payments
                                  WHERE business_date = %(d)s AND batch_id = (
                                        SELECT batch_id FROM bronze.payments WHERE business_date = %(d)s
                                        ORDER BY ingested_at DESC LIMIT 1)""", {"d": d})["n"]
        source_payments = _source_rows("payments.csv", d)
        source_matches_bronze = None if source_payments is None else source_payments == bronze_payments
        checks = [
            {"ok": True, "text": f"Gateway settlement reports {m['settled_payments']:,} payments ({inr(m['settled_amount'])})",
             "source": "gold.settlement_reconciliation"},
            {"ok": True, "text": f"Silver contains {m['silver_payments']:,} captured payments ({inr(m['silver_amount'])})",
             "source": "silver.payments"},
            {"ok": source_matches_bronze,
             "text": (f"Source file rows for {d}: {source_payments:,}; bronze rows: {bronze_payments:,}"
                      if source_payments is not None else
                      f"Source file row count for {d} is unavailable; bronze contains {bronze_payments:,} rows"),
             "source": "payments.csv / bronze.payments"},
            {"ok": count_match, "text": f"{rej['n']} captured payments rejected in silver by rule {rej['rules']} "
                                        f"= the gap of {m['payment_difference']} payments", "source": "silver.rejected_records"},
            {"ok": amount_match, "text": f"Rejected amount {inr(rej['amount'])} = gap amount {inr(m['amount_difference'])}",
             "source": "silver.rejected_records.raw_record"},
        ]
        why = {"CURRENCY_REQUIRED": "without a currency", "STATUS_VALID": "with an unknown status (e.g. a typo)",
               "AMOUNT_POSITIVE": "with a zero or negative amount", "PAYMENT_ID_REQUIRED": "without a payment id"}
        what = " / ".join(why.get(x.strip(), f"that failed {x.strip()}") for x in str(rej["rules"] or "").split(",") if x.strip())
        if count_match and amount_match:
            root = (f"The source payments file sent {rej['n']} captured payments for {d} {what or 'that failed a rule'}. "
                    f"Silver rejected them (rule {rej['rules']}), explaining the {inr(m['amount_difference'])} revenue gap.")
        elif count_match:
            root = ("Rejected payment counts match the settlement gap, but the rejected amount does not match the "
                    f"{inr(m['amount_difference'])} revenue gap. Check the source extract and reconciliation.")
        else:
            root = "The gap is not fully explained by rejected rows: check the source extract and reconciliation."
        source_step = (f"payments.csv: {source_payments:,} rows for {d}" if source_payments is not None
                       else f"payments.csv row count unavailable for {d}")
        chain = [f"Gateway settled {m['settled_payments']:,} payments", source_step,
                 f"Bronze landed {bronze_payments:,} rows (complete)",
                 f"Silver rejected {rej['n']} rows ({rej['rules']})",
                 f"Silver/Gold revenue short by {inr(m['amount_difference'])}",
                 "Gold views affected: " + ", ".join(_gold_views_using("silver.payments"))]
        sample = rows("""SELECT record_key, rule_name, raw_record::jsonb->>'amount' AS amount,
                                raw_record::jsonb->>'currency' AS currency, raw_record::jsonb->>'method' AS method
                         FROM silver.rejected_records WHERE entity = 'payments' AND business_date = %(d)s
                         ORDER BY record_key LIMIT 20""", {"d": d})
    elif inc["type"] == "REJECTED_ROWS":
        m = inc["metrics"]
        sample = rows("""SELECT record_key, rule_name, left(raw_record, 160) AS raw_record
                         FROM silver.rejected_records WHERE entity = %(e)s AND rule_name = %(r)s
                           AND business_date = %(d)s ORDER BY record_key LIMIT 20""",
                      {"e": m["entity"], "r": m["rule_name"], "d": m["business_date"]})
        observed = one("""SELECT COUNT(*) AS n,
                                 COUNT(*) FILTER (WHERE raw_record IS NOT NULL) AS with_raw_record
                          FROM silver.rejected_records
                          WHERE entity = %(e)s AND rule_name = %(r)s AND business_date = %(d)s""",
                       {"e": m["entity"], "r": m["rule_name"], "d": m["business_date"]})
        checks = [
            {"ok": observed["n"] == m["n"],
             "text": f"Incident reports {m['n']} rejected row(s); current query returns {observed['n']}",
             "source": "silver.rejected_records"},
            {"ok": observed["with_raw_record"] == observed["n"],
             "text": f"{observed['with_raw_record']} of {observed['n']} rejected row(s) retain raw source values",
             "source": "silver.rejected_records.raw_record"},
        ]
        root = f"Source data for {m['entity']} broke rule {m['rule_name']}."
        chain = [f"{ENTITIES[m['entity']][0]} delivered the rows", f"Silver rule {m['rule_name']} rejected {m['n']}",
                 "Rows held back from silver and gold"]
    elif inc["type"] == "DATA_LOSS":
        m = inc["metrics"]
        source_rows = m["source_rows"]
        bronze_rows = m["bronze_rows"]
        loaded = m["silver_loaded"]
        rejected = m["rejected"]
        duplicates = m["duplicates"]
        unexplained = bronze_rows - loaded - rejected - duplicates
        source_matches_bronze = None if source_rows is None else source_rows == bronze_rows
        checks = [
            {"ok": source_matches_bronze,
             "text": (f"Source has {source_rows:,} rows; Bronze has {bronze_rows:,}"
                      if source_rows is not None else "Source row count is unavailable"),
             "source": f"{m['source_file']} / {m['bronze_table']}"},
            {"ok": unexplained == 0,
             "text": (f"Bronze rows {bronze_rows:,} = Silver loaded {loaded:,} + rejected {rejected:,} "
                      f"+ duplicates {duplicates:,}; unexplained difference {unexplained:,}"),
             "source": f"{m['bronze_table']} / {m['silver_table']} / silver.rejected_records"},
        ]
        causes = []
        if source_rows is not None and source_rows != bronze_rows:
            causes.append(f"{source_rows - bronze_rows:,} source row(s) did not reach Bronze")
        if unexplained:
            causes.append(f"{unexplained:,} Bronze row(s) are not accounted for in Silver, rejects, or duplicates")
        root = "; ".join(causes) or "The recorded row counts do not currently identify the cause of the data-loss incident."
        chain = [f"{m['source_file']}: {source_rows if source_rows is not None else 'count unavailable'} source rows",
                 f"{m['bronze_table']}: {bronze_rows:,} rows",
                 f"{m['silver_table']}: {loaded:,} loaded, {rejected:,} rejected, {duplicates:,} duplicates",
                 root]
    elif inc["type"] == "SCHEMA_CHANGE":
        import schema_guard
        ev = schema_guard.investigate(inc)
        checks, chain, root, sample = ev["checks"], ev["chain"], ev["root_cause"], ev["sample"]
    else:
        sample = []
        checks = [{"ok": True, "text": inc["summary"], "source": "audit.step_log / audit.batch_control"}]
        root = inc["summary"]
        chain = [inc["title"]]
    return {"found": True, "incident": inc, "checks": checks, "root_cause": root,
            "chain": chain, "sample": sample, "impact": impact(inc)}


def impact(inc: dict) -> dict:
    """What the issue touches: gold views (from the catalog), dates and money. No invented numbers."""
    entity = {"SETTLEMENT_MISMATCH": "payments"}.get(inc["type"]) or (inc.get("metrics") or {}).get("entity")
    views = _gold_views_using(ENTITIES[entity][2]) if entity in ENTITIES else []
    if inc["type"] == "SCHEMA_CHANGE":                              # blast radius from the guard
        entity = inc["metrics"]["object"]
        views = [v.split(".", 1)[1] for v in inc["metrics"]["gold_views"]]
    amount = None
    if inc["type"] == "SETTLEMENT_MISMATCH":
        amount = inc["metrics"]["amount_difference"]
    actions = {
        "SETTLEMENT_MISMATCH": ["Ask the source team to resend the rejected payments with currency",
                                "Re-run the pipeline for the business date", "Check settlement reconciliation is MATCH",
                                "Notify the revenue report owner"],
        "REJECTED_ROWS": ["Fix the rows at source (see sample)", "Re-run the pipeline for the business date",
                          "Confirm silver.rejected_records is empty for this rule"],
        "DUPLICATES": ["Inform the source team about duplicate delivery", "No data fix needed: silver kept one copy"],
        "DATA_LOSS": ["Compare the source file with bronze", "Re-run bronze for the batch", "Check audit.step_log"],
        "PIPELINE_FAILURE": ["Read the error in audit.error_log", "Fix the cause", "Re-run the pipeline"],
        "SCHEMA_CHANGE": ["Contact the owners of the objects that break (Contact owner)",
                          "Revert the change, or update the pipeline and the views that use it",
                          "Re-run the pipeline and check the gold views", "Acknowledge: the change was handled"],
    }.get(inc["type"], [])
    return {"entity": entity, "gold_views": views, "business_date": inc.get("business_date"),
            "amount_at_risk": amount, "actions": actions}


# ============================================================================ trust score
def trust_score() -> dict:
    lin = lineage()
    recon = lin["settlement"]
    mismatches = [r for r in recon if r["recon_status"] == "MISMATCH"]
    total_in = sum(e["bronze_rows"] for e in lin["entities"]) or 1
    rejected = sum(e["rejected"] for e in lin["entities"])
    duplicates = sum(e["duplicates"] for e in lin["entities"])
    lost = sum(max(e["unexplained_loss"], 0) + max(e["source_to_bronze_gap"] or 0, 0) for e in lin["entities"])
    status = pipeline_status()
    failed_layers = [l for l in status["layers"] if l["status"] != "SUCCESS"]
    last_success = status["last_success"]
    hours_old = None
    if last_success:
        hours_old = (datetime.now() - datetime.fromisoformat(last_success)).total_seconds() / 3600

    reasons = []
    score = 100.0

    def penalty(points, text):
        nonlocal score
        if points > 0:
            score -= points
            reasons.append({"points": round(points, 1), "reason": text})

    penalty(min(PENALTY_SETTLEMENT_MISMATCH * len(mismatches), 45),
            f"{len(mismatches)} business date(s) where settlement does not match silver")
    penalty(min(10, 100.0 * lost / total_in * 10), f"{lost} row(s) missing between source and silver")
    penalty(min(10, 100.0 * rejected / total_in * 5), f"{rejected} row(s) rejected by quality rules")
    penalty(min(5, 100.0 * duplicates / total_in * 5), f"{duplicates} duplicate row(s) in the source")
    penalty(PENALTY_PIPELINE_FAILED if failed_layers else 0,
            "latest run: " + ", ".join(f"{l['layer']} {l['status']}" for l in failed_layers))
    penalty(PENALTY_STALE if hours_old is None or hours_old > FRESHNESS_HOURS else 0,
            f"no successful run in the last {FRESHNESS_HOURS} h")
    score = max(0, round(score))
    stale = hours_old is None or hours_old > FRESHNESS_HOURS
    label = ("At risk" if score < ATTENTION else "Needs attention") if stale else (
        "Healthy" if score >= HEALTHY else "Needs attention" if score >= ATTENTION else "At risk"
    )

    matched = len(recon) - len(mismatches)
    dims = {
        "completeness": round(100.0 * (total_in - lost) / total_in, 2),
        "validity": round(100.0 * (total_in - rejected) / total_in, 2),
        "uniqueness": round(100.0 * (total_in - duplicates) / total_in, 2),
        "reconciliation": round(100.0 * matched / len(recon), 2) if recon else None,
        "freshness_hours": round(hours_old, 1) if hours_old is not None else None,
    }
    return {"score": score, "label": label, "reasons": reasons, "dimensions": dims,
            "rules": {"healthy_from": HEALTHY, "attention_from": ATTENTION,
                      "freshness_hours": FRESHNESS_HOURS}, "trend": trust_trend()}


def trust_trend() -> list:
    """Score per business date with the same rules (settlement mismatch, rejected rows that day)."""
    out = []
    for r in rows("""
        SELECT d.business_date::text AS business_date,
               COALESCE(s.recon_status, 'NO SETTLEMENT') AS recon_status,
               (SELECT COUNT(*) FROM silver.rejected_records x WHERE x.business_date = d.business_date) AS rejected,
               d.total_orders + d.captured_payments + d.failed_payments AS volume
        FROM gold.daily_revenue d LEFT JOIN gold.settlement_reconciliation s USING (business_date)
        ORDER BY 1"""):
        s = 100.0
        if r["recon_status"] == "MISMATCH":
            s -= PENALTY_SETTLEMENT_MISMATCH
        s -= min(10, 100.0 * r["rejected"] / max(r["volume"], 1) * 5)
        out.append({"business_date": r["business_date"], "score": round(s, 1), "recon_status": r["recon_status"],
                    "rejected": r["rejected"]})
    return out


# ============================================================================ overview KPIs
def overview() -> dict:
    cov = data_coverage()
    totals = one("""SELECT SUM(gross_revenue) AS gross, SUM(refund_amount) AS refunds, SUM(net_revenue) AS net,
                           SUM(total_orders) AS orders, SUM(captured_payments) AS payments
                    FROM gold.daily_revenue""")
    assets = rows("""SELECT table_schema AS schema, COUNT(*) AS n FROM information_schema.tables
                     WHERE table_schema IN ('bronze', 'silver', 'gold') AND table_name NOT LIKE 'stg_%%'
                     GROUP BY 1 ORDER BY 1""")
    inc = incidents()
    return {"coverage": cov, "totals": totals, "assets": assets,
            "incidents": {"open": len(inc), "high": sum(1 for i in inc if i["severity"] in ("CRITICAL", "HIGH")),
                          "quality": sum(1 for i in inc if i["type"] in ("REJECTED_ROWS", "DUPLICATES")),
                          "money": sum(1 for i in inc if i["type"] == "SETTLEMENT_MISMATCH")},
            "daily": rows("""SELECT business_date::text AS business_date, gross_revenue, refund_amount, net_revenue,
                                    total_orders, captured_payments FROM gold.daily_revenue ORDER BY 1""")}


# ============================================================================ revenue questions
def as_of_date() -> date:
    """'Last month', 'last 7 days' ... are counted back from this date.
    DATATRUST_AS_OF=data (default): latest business date in gold.  =today: the calendar date."""
    import os
    if os.getenv("DATATRUST_AS_OF", "data").lower() == "today":
        return date.today()
    last = data_coverage().get("last_date")
    return date.fromisoformat(last) if last else date.today()


GROUPINGS = {
    "day":     ("gold.daily_revenue", "business_date::text", "gross_revenue", "captured_payments"),
    "method":  ("gold.payment_method_summary", "method", "captured_amount", "captured"),
    "region":  ("gold.revenue_by_segment", "region", "captured_amount", "captured_payments"),
    "segment": ("gold.revenue_by_segment", "segment", "captured_amount", "captured_payments"),
}


def revenue(start: date, end: date, group_by: str = None) -> dict:
    total = one("""SELECT COUNT(*) AS days, SUM(gross_revenue) AS gross, SUM(refund_amount) AS refunds,
                          SUM(net_revenue) AS net, SUM(captured_payments) AS payments, SUM(total_orders) AS orders
                   FROM gold.daily_revenue WHERE business_date BETWEEN %(s)s AND %(e)s""", {"s": start, "e": end})
    result = {"start": start.isoformat(), "end": end.isoformat(), "total": total, "coverage": data_coverage(),
              "source": "gold.daily_revenue", "breakdown": []}
    if group_by in GROUPINGS:
        view, col, amount, count = GROUPINGS[group_by]
        result["breakdown"] = rows(f"""SELECT {col} AS label, SUM({amount}) AS amount, SUM({count}) AS payments
                                       FROM {view} WHERE business_date BETWEEN %(s)s AND %(e)s
                                       GROUP BY 1 ORDER BY {'1' if group_by == 'day' else '2 DESC'}""",
                                    {"s": start, "e": end})
        result["source"] = view
    return result


def top_customers(start: date, end: date, limit: int = 10) -> list:
    return rows("""SELECT p.customer_id, c.customer_name, c.segment, c.region, COUNT(*) AS payments,
                          SUM(p.amount) AS amount
                   FROM silver.payments p LEFT JOIN gold.customer_current c USING (customer_id)
                   WHERE p.status = 'CAPTURED' AND p.business_date BETWEEN %(s)s AND %(e)s
                   GROUP BY 1, 2, 3, 4 ORDER BY amount DESC LIMIT %(n)s""", {"s": start, "e": end, "n": limit})


def customer_history(customer_id: str) -> list:
    return rows("""SELECT customer_id, customer_name, email, city, state, region, segment,
                          valid_from, valid_to, is_current
                   FROM silver.customers WHERE customer_id = %(c)s ORDER BY valid_from""", {"c": customer_id.upper()})


def query_gold(sql: str, limit: int = 200) -> dict:
    """Read-only SELECT on gold views only (used by the LLM agent for questions the fixed tools do not cover)."""
    import re
    text = sql.strip().rstrip(";").strip()
    if ";" in text or not re.match(r"(?is)^\s*(select|with)\b", text):
        raise ValueError("only one SELECT statement is allowed")
    if re.search(r"(?i)\b(bronze|silver|audit|pg_catalog|information_schema)\s*\.", text):
        raise ValueError("only gold.* views can be queried")
    data = rows(f"SELECT * FROM ({text}) q LIMIT {int(limit)}")
    return {"columns": list(data[0].keys()) if data else [], "rows": data}


def inr(value) -> str:
    """Indian format: 1832333.7 -> ₹18,32,333.70"""
    if value is None:
        return "-"
    v = float(value)
    sign = "-" if v < 0 else ""
    whole, frac = f"{abs(v):.2f}".split(".")
    if len(whole) > 3:
        head, tail = whole[:-3], whole[-3:]
        head = ",".join([head[max(i - 2, 0):i] for i in range(len(head), 0, -2)][::-1])
        whole = f"{head},{tail}"
    return f"{sign}₹{whole}.{frac}"
