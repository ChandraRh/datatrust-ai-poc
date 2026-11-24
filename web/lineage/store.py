"""app.lineage_snapshots: the last lineage graph read from each source.

A source is read again when its graph is older than LINEAGE_MAX_AGE_HOURS (default 6), when someone presses Refresh,
or (after a failure, e.g. the server was unreachable) at most once an hour, so one unreachable server never slows
every page. Reading is read-only on the source.
"""
from __future__ import annotations

import os
import threading
import time
from datetime import datetime, timedelta, timezone

from psycopg.types.json import Jsonb

import appdb

DDL = """
CREATE SCHEMA IF NOT EXISTS app;
CREATE TABLE IF NOT EXISTS app.lineage_snapshots (
    scope      TEXT PRIMARY KEY,
    name       TEXT,
    built_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    seconds    NUMERIC,
    ok         BOOLEAN NOT NULL,
    error      TEXT,
    graph      JSONB
);
"""
RETRY_FAILED = timedelta(hours=1)
_lock = threading.Lock()


def ensure_table() -> None:
    with appdb.connect() as conn:
        conn.execute(DDL)
        conn.commit()


def max_age() -> timedelta:
    try:
        return timedelta(hours=float(os.getenv("LINEAGE_MAX_AGE_HOURS", "6")))
    except ValueError:
        return timedelta(hours=6)


def _when(v) -> datetime:
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=timezone.utc)
    d = datetime.fromisoformat(str(v))
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def build(scope: dict) -> dict:
    """Read one source now and keep the result (or the error)."""
    from . import collect
    t0 = time.time()
    try:
        g = collect.collect(scope).to_json()
        ok, err = True, None
    except Exception as e:                                            # noqa: BLE001  unreachable server, no rights ...
        g, ok, err = None, False, f"{type(e).__name__}: {e}"[:500]
    with appdb.connect() as conn:
        conn.execute("""INSERT INTO app.lineage_snapshots (scope, name, built_at, seconds, ok, error, graph)
                        VALUES (%s, %s, now(), %s, %s, %s, %s)
                        ON CONFLICT (scope) DO UPDATE SET name = EXCLUDED.name, built_at = now(), seconds = EXCLUDED.seconds,
                            ok = EXCLUDED.ok, error = EXCLUDED.error,
                            graph = COALESCE(EXCLUDED.graph, app.lineage_snapshots.graph)""",
                     (scope["key"], scope["name"], round(time.time() - t0, 2), ok, err, Jsonb(g) if g is not None else None))
        conn.commit()
    return {"scope": scope["key"], "ok": ok, "error": err}


def snapshots(scopes: list, refresh: bool = False) -> list:
    """The latest graph of every source, reading again those that are missing or too old. One build at a time."""
    if not appdb.table_exists("app.lineage_snapshots"):
        ensure_table()
    with _lock:
        have = {r["scope"]: r for r in appdb.rows("SELECT scope, built_at, ok FROM app.lineage_snapshots")}
        now = datetime.now(timezone.utc)
        for s in scopes:
            r = have.get(s["key"])
            stale = r is None or now - _when(r["built_at"]) > (max_age() if r["ok"] else RETRY_FAILED)
            if refresh or stale:
                build(s)
        keys = [s["key"] for s in scopes]
        return appdb.rows("""SELECT scope, name, built_at, seconds, ok, error, graph FROM app.lineage_snapshots
                             WHERE scope = ANY(%s)""", (keys,))
