"""
Change tracking without a superuser: schema snapshots.

Event triggers (the "who changed what" log) can only be created by a superuser (on Azure Database for PostgreSQL 18:
also the server admin). When the source's own login may not create them, "Enable tracking" falls back to this:

    dt_audit.schema_snapshot   one row per table / view / materialized view / foreign table: its columns, as last seen
    dt_audit.ddl_log           the same change log the event triggers write; here DataTrust writes a row for each
                               difference it finds between two snapshots

Each change is written as the statement that would have made it ("ALTER TABLE silver.payments DROP COLUMN email"), so
the Schema changes page, the catalog and the schema-change guard read it exactly like a logged statement. What a
snapshot cannot know: who made the change (username stays empty) and the exact time (event_time is when DataTrust
noticed it; snapshots are compared every few minutes and whenever the changes are read).

Objects are matched by their oid and columns by their attnum, so a rename is seen as a rename, not as a drop + create.
"""
from __future__ import annotations

import json

APPLICATION = "schema snapshot"            # shown as the client; deliberately not "datatrust..." (that would be hidden)
KINDS = {"r": "table", "p": "table", "v": "view", "m": "materialized view", "f": "foreign table"}

DDL = """
CREATE SCHEMA IF NOT EXISTS dt_audit;
CREATE TABLE IF NOT EXISTS dt_audit.ddl_log (
    log_id          BIGSERIAL PRIMARY KEY,
    event_time      TIMESTAMPTZ DEFAULT now(),
    username        TEXT DEFAULT session_user,
    client_addr     TEXT DEFAULT inet_client_addr()::text,
    application     TEXT DEFAULT current_setting('application_name', true),
    command_tag     TEXT,
    object_type     TEXT,
    schema_name     TEXT,
    object_identity TEXT,
    query           TEXT);
CREATE TABLE IF NOT EXISTS dt_audit.schema_snapshot (
    rel_oid      OID PRIMARY KEY,
    schema_name  TEXT NOT NULL,
    object_name  TEXT NOT NULL,
    kind         TEXT NOT NULL,
    columns      JSONB NOT NULL,
    seen_at      TIMESTAMPTZ NOT NULL DEFAULT now());
"""

CATALOG = """
SELECT c.oid::bigint AS rel_oid, n.nspname AS schema_name, c.relname AS object_name, c.relkind::text AS kind,
       COALESCE(json_agg(json_build_array(a.attnum, a.attname, format_type(a.atttypid, a.atttypmod), a.attnotnull)
                         ORDER BY a.attnum) FILTER (WHERE a.attnum IS NOT NULL), '[]')::text AS columns
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
LEFT JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum > 0 AND NOT a.attisdropped
WHERE c.relkind IN ('r', 'p', 'v', 'm', 'f') AND NOT c.relispartition
  AND n.nspname NOT IN ('pg_catalog', 'information_schema', 'dt_audit')
  AND n.nspname NOT LIKE 'pg\\_toast%%' AND n.nspname NOT LIKE 'pg\\_temp%%'
GROUP BY c.oid, n.nspname, c.relname, c.relkind
"""


def _ident(schema: str, name: str) -> str:
    return f"{schema}.{name}"


def _cols(v) -> dict:
    """[[attnum, name, type, not_null], ...] -> {attnum: (name, type, not_null)}"""
    rows = json.loads(v) if isinstance(v, str) else (v or [])
    return {int(r[0]): (r[1], r[2], bool(r[3])) for r in rows}


def diff(before: dict, now: dict) -> list:
    """before / now: {oid: {"schema_name", "object_name", "kind", "columns"}}. Returns change-log rows
    (command_tag, object_type, schema_name, object_identity, query), in a stable order."""
    out = []
    for oid in sorted(set(before) | set(now)):
        b, n = before.get(oid), now.get(oid)
        if b is None:
            what = KINDS.get(n["kind"], "table")
            ident = _ident(n["schema_name"], n["object_name"])
            out.append((f"CREATE {what.upper()}", what, n["schema_name"], ident, f"CREATE {what.upper()} {ident}"))
            continue
        what = KINDS.get(b["kind"], "table")
        old = _ident(b["schema_name"], b["object_name"])
        if n is None:
            out.append((f"DROP {what.upper()}", what, b["schema_name"], old, f"DROP {what.upper()} {old}"))
            continue
        alter = f"ALTER {'VIEW' if what == 'view' else 'MATERIALIZED VIEW' if what == 'materialized view' else 'TABLE'}"
        ident = _ident(n["schema_name"], n["object_name"])
        if b["schema_name"] != n["schema_name"]:
            out.append((alter, what, n["schema_name"], ident, f"{alter} {old} SET SCHEMA {n['schema_name']}"))
            old = _ident(n["schema_name"], b["object_name"])
        if b["object_name"] != n["object_name"]:
            out.append((alter, what, n["schema_name"], ident, f"{alter} {old} RENAME TO {n['object_name']}"))
        bc, nc = _cols(b["columns"]), _cols(n["columns"])
        for num in sorted(set(bc) | set(nc)):
            x, y = bc.get(num), nc.get(num)
            if x is None:
                q = f"{alter} {ident} ADD COLUMN {y[0]} {y[1]}"
            elif y is None:
                q = f"{alter} {ident} DROP COLUMN {x[0]}"
            else:
                parts = []
                if x[0] != y[0]:
                    parts.append(f"{alter} {ident} RENAME COLUMN {x[0]} TO {y[0]}")
                if x[1] != y[1]:
                    parts.append(f"{alter} {ident} ALTER COLUMN {y[0]} TYPE {y[1]} (was {x[1]})")
                if x[2] != y[2]:
                    parts.append(f"{alter} {ident} ALTER COLUMN {y[0]} {'SET' if y[2] else 'DROP'} NOT NULL")
                for p in parts:
                    out.append((alter, what, n["schema_name"], ident, p))
                continue
            out.append((alter, what, n["schema_name"], ident, q))
    return out


def install(conn) -> None:
    """Tables + a first snapshot (the baseline: nothing is logged for what already exists). conn: a writable connection."""
    conn.execute(DDL)
    refresh(conn, log=False)


def installed(conn) -> bool:
    return bool(conn.execute("SELECT to_regclass('dt_audit.schema_snapshot') IS NOT NULL AS ok").fetchone()["ok"])


def refresh(conn, log: bool = True) -> int:
    """Compare the catalog with the last snapshot, log the differences, keep the new snapshot. One at a time per
    database (advisory lock), so two DataTrust processes never log the same change twice. Returns the rows logged."""
    if not conn.execute("SELECT pg_try_advisory_xact_lock(hashtext('dt_audit.schema_snapshot')) AS ok").fetchone()["ok"]:
        return 0
    before = {r["rel_oid"]: r for r in conn.execute(
        "SELECT rel_oid::bigint AS rel_oid, schema_name, object_name, kind, columns::text AS columns FROM dt_audit.schema_snapshot").fetchall()}
    now = {r["rel_oid"]: r for r in conn.execute(CATALOG).fetchall()}
    changes = diff(before, now) if log else []
    for c in changes:
        conn.execute("""INSERT INTO dt_audit.ddl_log (username, client_addr, application, command_tag, object_type, schema_name,
                                                      object_identity, query)
                        VALUES (NULL, NULL, %s, %s, %s, %s, %s, %s)""", (APPLICATION, *c))
    if changes or not log or set(before) != set(now):
        conn.execute("DELETE FROM dt_audit.schema_snapshot")
        for r in now.values():
            conn.execute("""INSERT INTO dt_audit.schema_snapshot (rel_oid, schema_name, object_name, kind, columns)
                            VALUES (%s, %s, %s, %s, %s::jsonb)""",
                         (r["rel_oid"], r["schema_name"], r["object_name"], r["kind"], r["columns"]))
    conn.commit()
    return len(changes)
