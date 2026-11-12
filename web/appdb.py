"""
DataTrust's own database (config APP_PG_*): everything the product itself keeps.

    portal.*      logins, connections to customer systems, people, access requests, portal audit log
    telemetry.*   what connectors collect (ADF runs, steps, checks, sync log)
    app.*         approvals (remediation_log) and alerts sent (alert_log)

The systems DataTrust monitors (the POC database, pg-sales-hv, ...) are only ever read through connections.

    prepare()     create the database if needed and, once, move DataTrust's tables out of the POC database
"""
import sys
import threading
from pathlib import Path

import psycopg
from psycopg import sql
from psycopg.rows import dict_row

ROOT = next(f for f in Path(__file__).resolve().parents if (f / "config.py").exists())
sys.path.insert(0, str(ROOT))
import config  # noqa: E402

# what moves from the POC database on first start (parents before children, for the foreign keys)
MOVE = [
    ("portal.settings", "portal.settings"), ("portal.users", "portal.users"), ("portal.auth_tokens", "portal.auth_tokens"),
    ("portal.connections", "portal.connections"), ("portal.access_requests", "portal.access_requests"),
    ("portal.people", "portal.people"), ("portal.audit_log", "portal.audit_log"),
    ("telemetry.sources", "telemetry.sources"), ("telemetry.definitions", "telemetry.definitions"),
    ("telemetry.pipeline_runs", "telemetry.pipeline_runs"), ("telemetry.task_runs", "telemetry.task_runs"),
    ("telemetry.trigger_runs", "telemetry.trigger_runs"), ("telemetry.checks", "telemetry.checks"),
    ("telemetry.sync_log", "telemetry.sync_log"), ("telemetry.setup_draft", "telemetry.setup_draft"),
    ("audit.remediation_log", "app.remediation_log"), ("audit.alert_log", "app.alert_log"),
]

APP_DDL = """
CREATE SCHEMA IF NOT EXISTS app;
CREATE TABLE IF NOT EXISTS app.remediation_log (
    remediation_id  BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    incident_id     TEXT,
    incident_title  TEXT,
    actions         TEXT,
    approved_by     TEXT,
    note            TEXT,
    status          TEXT DEFAULT 'APPROVED',
    approved_at     TIMESTAMP DEFAULT now());
CREATE TABLE IF NOT EXISTS app.alert_log (
    incident_id         TEXT PRIMARY KEY,
    severity            TEXT,
    title               TEXT,
    first_alerted_at    TIMESTAMP DEFAULT now(),
    resolved_alerted_at TIMESTAMP,
    last_error          TEXT);
"""


def connect(dbname: str = None, **kw):
    return psycopg.connect(sslmode=config.APP_PG_SSLMODE, host=config.APP_PG_HOST, port=config.APP_PG_PORT,
                           dbname=dbname or config.APP_PG_DATABASE, user=config.APP_PG_USER, password=config.APP_PG_PASSWORD,
                           row_factory=dict_row, connect_timeout=5, **kw)


_local = threading.local()


def _reader():
    """One read-only autocommit connection per worker thread, reconnecting when broken (same pattern as data_service)."""
    conn = getattr(_local, "conn", None)
    if conn is None or conn.closed or conn.broken:
        conn = connect(autocommit=True)
        conn.execute("SET default_transaction_read_only = on")
        conn.execute("SET statement_timeout = '10s'")
        _local.conn = conn
    return conn


def rows(query: str, params=None) -> list:
    from data_service import plain
    for attempt in (1, 2):
        try:
            return [{k: plain(v) for k, v in r.items()} for r in _reader().execute(query, params).fetchall()]
        except psycopg.OperationalError:
            _local.conn = None
            if attempt == 2:
                raise


def one(query: str, params=None) -> dict:
    result = rows(query, params)
    return result[0] if result else {}


def table_exists(name: str) -> bool:
    return bool(one("SELECT to_regclass(%(n)s) IS NOT NULL AS ok", {"n": name}).get("ok"))


def label() -> str:
    return f"{config.APP_PG_HOST}:{config.APP_PG_PORT}/{config.APP_PG_DATABASE}"


def ensure_database() -> bool:
    """Create the database when it does not exist yet. True if it was created."""
    try:
        connect().close()
        return False
    except psycopg.OperationalError as e:
        if "does not exist" not in str(e):
            raise
    with connect("postgres", autocommit=True) as admin:
        try:
            admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(config.APP_PG_DATABASE)))
        except psycopg.errors.InsufficientPrivilege as e:
            raise RuntimeError(f"Database {config.APP_PG_DATABASE} does not exist and {config.APP_PG_USER} may not create it. "
                               f"Create it once (CREATE DATABASE {config.APP_PG_DATABASE};) and restart.") from e
    return True


def ensure_tables() -> None:
    with connect() as conn:
        conn.execute(APP_DDL)
        conn.commit()


def _poc_connect():
    return psycopg.connect(sslmode=config.PG_SSLMODE, host=config.PG_HOST, port=config.PG_PORT, dbname=config.PG_DATABASE,
                           user=config.PG_USER, password=config.PG_PASSWORD, row_factory=dict_row, connect_timeout=5)


def _same_database() -> bool:
    return (config.APP_PG_HOST.lower(), config.APP_PG_PORT, config.APP_PG_DATABASE) == \
           (config.PG_HOST.lower(), config.PG_PORT, config.PG_DATABASE)


def _exists(conn, table: str) -> bool:
    return conn.execute("SELECT to_regclass(%s) IS NOT NULL AS ok", (table,)).fetchone()["ok"]


def _columns(conn, table: str) -> list:
    schema, name = table.split(".")
    return [r["column_name"] for r in conn.execute(
        """SELECT column_name FROM information_schema.columns WHERE table_schema = %s AND table_name = %s
           AND is_generated = 'NEVER' ORDER BY ordinal_position""", (schema, name))]


def migrate_from_poc() -> dict:
    """Copy DataTrust's tables from the POC database into this one, once. Runs only while this database holds no
    users yet, so existing logins, connections and collected ADF history carry over. The POC copies are left in place."""
    if _same_database():
        return {"skipped": "APP_PG_* points at the POC database"}
    with connect() as app:
        if _exists(app, "portal.users") and app.execute("SELECT 1 FROM portal.users LIMIT 1").fetchone():
            return {"skipped": "already has users"}
    try:
        poc = _poc_connect()
    except psycopg.OperationalError as e:
        return {"skipped": f"POC database not reachable: {e}"}
    copied = {}
    with poc, connect() as app:
        if not _exists(poc, "portal.users"):
            return {"skipped": "nothing to move"}
        for src, dst in MOVE:
            if not _exists(poc, src) or not _exists(app, dst):
                continue
            cols = [c for c in _columns(poc, src) if c in set(_columns(app, dst))]
            ident = sql.SQL(", ").join(map(sql.Identifier, cols))
            s_schema, s_table = src.split(".")
            d_schema, d_table = dst.split(".")
            app.execute(sql.SQL("DELETE FROM {}.{}").format(sql.Identifier(d_schema), sql.Identifier(d_table)))
            n = 0
            with poc.cursor().copy(sql.SQL("COPY (SELECT {} FROM {}.{}) TO STDOUT").format(
                    ident, sql.Identifier(s_schema), sql.Identifier(s_table))) as out, \
                 app.cursor().copy(sql.SQL("COPY {}.{} ({}) FROM STDIN").format(
                    sql.Identifier(d_schema), sql.Identifier(d_table), ident)) as into:
                for block in out:
                    into.write(block)
                    n += 1
            copied[dst] = app.execute(sql.SQL("SELECT COUNT(*) AS n FROM {}.{}").format(
                sql.Identifier(d_schema), sql.Identifier(d_table))).fetchone()["n"]
            for c in cols:                                  # serial / identity counters continue after the copied rows
                seq = app.execute("SELECT pg_get_serial_sequence(%s, %s) AS s", (dst, c)).fetchone()["s"]
                if seq:
                    app.execute(sql.SQL("SELECT setval(%s, COALESCE((SELECT MAX({}) FROM {}.{}), 0) + 1, false)").format(
                        sql.Identifier(c), sql.Identifier(d_schema), sql.Identifier(d_table)), (seq,))
        app.execute("""INSERT INTO portal.settings (key, value) VALUES ('moved_from', %s)
                       ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value""",
                    (f"{config.PG_HOST}:{config.PG_PORT}/{config.PG_DATABASE}",))
        app.commit()
    return {"copied": copied}


def prepare() -> dict:
    """Server start: database, tables (portal, telemetry, app), then the one-time move. Seeds come after the move."""
    created = ensure_database()
    from estate import store as estate_store
    from adf import store as adf_store
    estate_store.create_tables()
    adf_store.ensure_tables()
    ensure_tables()
    moved = migrate_from_poc()
    return {"database": label(), "created": created, **moved}
