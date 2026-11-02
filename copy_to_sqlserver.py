"""
Copy the POC data from PostgreSQL into SQL Server (one-way copy; the pipeline keeps running on PostgreSQL).

    pip install pyodbc                                   (once, inside the project's .venv)
    python copy_to_sqlserver.py                          localhost, your Windows login, database DataTrustPOC
    python copy_to_sqlserver.py --dry-run                only show what would be created; SQL Server is not touched
    python copy_to_sqlserver.py --schemas silver gold    only these schemas
    python copy_to_sqlserver.py --server "localhost\\SQLEXPRESS" --database DataTrustPOC

Optional settings in .env (command-line options win):
    MSSQL_SERVER=localhost          MSSQL_DATABASE=DataTrustPOC
    MSSQL_DRIVER=ODBC Driver 18 for SQL Server      (default: newest one installed)
    MSSQL_USER= / MSSQL_PASSWORD=   SQL Server login; leave empty to use your Windows login (like SSMS)

What it does for schemas bronze, silver, gold, audit:
  * creates the database and the schemas if they are missing
  * copies every table AND every view as a table. A view arrives as its current result, because
    PostgreSQL view SQL does not run on SQL Server unchanged.
  * drops and re-creates each table on every run, so running it again gives a fresh, complete copy
  * keeps NOT NULL and primary keys of tables
  * compares row counts PostgreSQL vs SQL Server at the end
PostgreSQL is only read; nothing there is changed.
"""
import argparse
import datetime as dt
import decimal
import json
import math
import os
import re
import sys
import time

import psycopg
from psycopg import sql

import config

DEFAULT_SCHEMAS = ["bronze", "silver", "gold", "audit"]
BATCH = 5000
KIND = {"r": "table", "p": "table", "v": "view", "m": "mat. view", "f": "foreign table"}


def q(name: str) -> str:
    """SQL Server quoted identifier: [name]."""
    return "[" + name.replace("]", "]]") + "]"


# ============================================================================ PostgreSQL side
def pg_connect():
    return psycopg.connect(host=config.PG_HOST, port=config.PG_PORT, dbname=config.PG_DATABASE,
                           user=config.PG_USER, password=config.PG_PASSWORD, connect_timeout=10)


def pg_objects(conn, schemas):
    """Tables, views and materialized views with their columns (in column order) and primary keys."""
    objs = conn.execute("""
        SELECT n.nspname, c.relname, c.relkind
        FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = ANY(%s) AND c.relkind IN ('r','p','v','m','f') AND NOT c.relispartition
        ORDER BY array_position(%s::text[], n.nspname::text), CASE WHEN c.relkind IN ('r','p') THEN 0 ELSE 1 END, c.relname
    """, (schemas, schemas)).fetchall()
    cols = conn.execute("""
        SELECT n.nspname, c.relname, a.attname, format_type(a.atttypid, a.atttypmod), a.attnotnull
        FROM pg_attribute a JOIN pg_class c ON c.oid = a.attrelid JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = ANY(%s) AND a.attnum > 0 AND NOT a.attisdropped
        ORDER BY n.nspname, c.relname, a.attnum
    """, (schemas,)).fetchall()
    pks = conn.execute("""
        SELECT n.nspname, c.relname, array_agg(a.attname::text ORDER BY k.ord)
        FROM pg_constraint con JOIN pg_class c ON c.oid = con.conrelid JOIN pg_namespace n ON n.oid = c.relnamespace
        CROSS JOIN LATERAL unnest(con.conkey) WITH ORDINALITY AS k(attnum, ord)
        JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum = k.attnum
        WHERE con.contype = 'p' AND n.nspname = ANY(%s)
        GROUP BY n.nspname, c.relname, con.conname
    """, (schemas,)).fetchall()
    colmap, pkmap = {}, {(s, t): list(c) for s, t, c in pks}
    for s, t, name, typ, notnull in cols:
        colmap.setdefault((s, t), []).append({"name": name, "pg": typ, "notnull": notnull})
    result = []
    for s, t, kind in objs:
        is_table = kind in ("r", "p")
        pk = pkmap.get((s, t), []) if is_table else []
        columns = colmap.get((s, t), [])
        for c in columns:
            c["key"] = c["name"] in pk
            c["ms"] = mssql_type(c["pg"], c["key"])
            c["notnull"] = (c["notnull"] and is_table) or c["key"]
        result.append({"schema": s, "name": t, "kind": KIND[kind], "columns": columns, "pk": pk})
    return result


# ============================================================================ type mapping
def mssql_type(pg_type: str, in_key: bool = False) -> str:
    """PostgreSQL type (as format_type prints it) -> SQL Server type."""
    t = pg_type.lower().strip()
    long_text = "nvarchar(450)" if in_key else "nvarchar(max)"      # a key column cannot be (max)
    if t.endswith("[]"):
        return "nvarchar(max)"                                        # arrays arrive as JSON text
    m = re.fullmatch(r"(?:character varying|varchar)\((\d+)\)", t)
    if m:
        n = int(m.group(1))
        return f"nvarchar({n})" if n <= (450 if in_key else 4000) else long_text
    m = re.fullmatch(r"(?:character|char|bpchar)\((\d+)\)", t)
    if m:
        n = int(m.group(1))
        return f"nchar({n})" if n <= 4000 else long_text
    m = re.fullmatch(r"numeric\((\d+),(\d+)\)", t)
    if m:
        p, s = int(m.group(1)), int(m.group(2))
        if p > 38:
            s, p = min(s, 10), 38
        return f"decimal({p},{s})"
    if re.fullmatch(r"timestamp(\(\d\))? (without|with) time zone", t):
        return "datetime2(6)"                                         # "with time zone" is stored as UTC
    if re.fullmatch(r"time(\(\d\))? without time zone", t):
        return "time(6)"
    simple = {"smallint": "smallint", "integer": "int", "bigint": "bigint", "real": "real",
              "double precision": "float", "boolean": "bit", "date": "date", "uuid": "uniqueidentifier",
              "bytea": "varbinary(max)", "money": "decimal(19,4)", "numeric": "decimal(38,10)", "oid": "bigint"}
    return simple.get(t, long_text)       # text, json, jsonb, interval, inet, ... -> text


def converter(ms_type: str):
    """Python value from psycopg -> value pyodbc / SQL Server accepts for that column."""
    if ms_type.startswith("decimal"):
        scale = int(re.search(r",(\d+)\)", ms_type).group(1))
        step = decimal.Decimal(1).scaleb(-scale)
        ctx = decimal.Context(prec=60)

        def to_dec(v):
            if v is None or not isinstance(v, decimal.Decimal):
                return v
            if not v.is_finite():
                return None
            return v.quantize(step, context=ctx)
        return to_dec
    if ms_type in ("float", "real"):
        return lambda v: None if isinstance(v, float) and not math.isfinite(v) else v
    if ms_type.startswith("datetime2"):
        # timestamptz arrives timezone-aware: store it as UTC without the zone
        return lambda v: v.astimezone(dt.timezone.utc).replace(tzinfo=None) if isinstance(v, dt.datetime) and v.tzinfo else v
    if ms_type == "uniqueidentifier":
        return lambda v: None if v is None else str(v)
    if ms_type.startswith("time"):
        return lambda v: v.replace(tzinfo=None) if isinstance(v, dt.time) and v.tzinfo else v
    if ms_type.startswith(("nvarchar", "nchar")):
        def to_text(v):
            if v is None or isinstance(v, str):
                return v
            if isinstance(v, (dict, list)):
                return json.dumps(v, default=str, ensure_ascii=False)
            return str(v)                         # interval, inet, ...
        return to_text
    return lambda v: v


def create_table_sql(obj) -> str:
    lines = [f"    {q(c['name'])} {c['ms']}{' NOT NULL' if c['notnull'] else ' NULL'}" for c in obj["columns"]]
    return f"CREATE TABLE {q(obj['schema'])}.{q(obj['name'])} (\n" + ",\n".join(lines) + "\n)"


def pk_sql(obj):
    if not obj["pk"]:
        return None
    name = f"PK_{obj['schema']}_{obj['name']}"[:128]
    return (f"ALTER TABLE {q(obj['schema'])}.{q(obj['name'])} ADD CONSTRAINT {q(name)} "
            f"PRIMARY KEY ({', '.join(q(c) for c in obj['pk'])})")


# ============================================================================ SQL Server side
def pick_driver(pyodbc, wanted):
    installed = pyodbc.drivers()
    if wanted:
        if wanted not in installed:
            sys.exit(f"ODBC driver '{wanted}' is not installed. Installed: {', '.join(installed)}")
        return wanted
    for d in ("ODBC Driver 18 for SQL Server", "ODBC Driver 17 for SQL Server"):
        if d in installed:
            return d
    sys.exit("No 'ODBC Driver 17/18 for SQL Server' found. Install it from Microsoft (msodbcsql) and run again.")


def conn_str(driver, server, database, user, password):
    parts = [f"DRIVER={{{driver}}}", f"SERVER={server}", f"DATABASE={database}",
             "TrustServerCertificate=yes", "APP=DataTrust copy_to_sqlserver"]
    if user:
        parts += [f"UID={user}", f"PWD={{{password}}}"]
    else:
        parts.append("Trusted_Connection=yes")                 # Windows login, same as SSMS
    return ";".join(parts)


def ms_connect(pyodbc, driver, server, database, user, password, autocommit=False):
    try:
        return pyodbc.connect(conn_str(driver, server, database, user, password), autocommit=autocommit, timeout=15)
    except pyodbc.Error as e:
        sys.exit(f"Cannot connect to SQL Server '{server}' (database {database}).\n  {e}\n"
                 "  Check: the server name is the one SSMS connects to (e.g. localhost or localhost\\SQLEXPRESS),\n"
                 "  and the SQL Server service is running (Start > Services > SQL Server (...)).")


def insert_rows(pyodbc, ms, obj, rows):
    """Insert one batch; fast mode first, row-by-row fallback if the driver refuses fast mode."""
    cols = ", ".join(q(c["name"]) for c in obj["columns"])
    marks = ", ".join("?" for _ in obj["columns"])
    stmt = f"INSERT INTO {q(obj['schema'])}.{q(obj['name'])} ({cols}) VALUES ({marks})"
    cur = ms.cursor()
    try:
        cur.fast_executemany = True
        cur.executemany(stmt, rows)
        ms.commit()
    except (pyodbc.Error, MemoryError):
        ms.rollback()
        cur = ms.cursor()
        cur.fast_executemany = False
        cur.executemany(stmt, rows)
        ms.commit()


def copy_object(pg, pyodbc, ms, obj):
    full = f"{q(obj['schema'])}.{q(obj['name'])}"
    cur = ms.cursor()
    cur.execute(f"DROP TABLE IF EXISTS {full}")
    cur.execute(create_table_sql(obj))
    ms.commit()

    convs = [converter(c["ms"]) for c in obj["columns"]]
    select = sql.SQL("SELECT {} FROM {}.{}").format(
        sql.SQL(", ").join(sql.Identifier(c["name"]) for c in obj["columns"]),
        sql.Identifier(obj["schema"]), sql.Identifier(obj["name"]))
    copied = 0
    with pg.cursor(name=f"copy_{obj['schema']}_{obj['name']}"[:60]) as src:
        src.itersize = BATCH
        src.execute(select)
        while True:
            batch = src.fetchmany(BATCH)
            if not batch:
                break
            rows = [tuple(f(v) for f, v in zip(convs, r)) for r in batch]
            insert_rows(pyodbc, ms, obj, rows)
            copied += len(rows)
    pg.commit()

    note = ""
    stmt = pk_sql(obj)
    if stmt:
        try:
            cur.execute(stmt)
            ms.commit()
            note = "PK"
        except pyodbc.Error as e:
            ms.rollback()
            note = f"no PK ({str(e).split('[SQL Server]')[-1][:80]})"
    return copied, note


# ============================================================================ main
def main():
    ap = argparse.ArgumentParser(description="Copy PostgreSQL schemas into SQL Server")
    ap.add_argument("--server", default=os.getenv("MSSQL_SERVER", "localhost"))
    ap.add_argument("--database", default=os.getenv("MSSQL_DATABASE", "DataTrustPOC"))
    ap.add_argument("--driver", default=os.getenv("MSSQL_DRIVER", ""))
    ap.add_argument("--schemas", nargs="+", default=DEFAULT_SCHEMAS)
    ap.add_argument("--dry-run", action="store_true", help="print the CREATE TABLE statements and row counts only")
    a = ap.parse_args()
    user, password = os.getenv("MSSQL_USER", ""), os.getenv("MSSQL_PASSWORD", "")

    try:
        pg = pg_connect()
    except Exception as e:
        sys.exit(f"Cannot connect to PostgreSQL {config.PG_HOST}:{config.PG_PORT}/{config.PG_DATABASE}: {e}")
    objs = pg_objects(pg, a.schemas)
    if not objs:
        sys.exit(f"Nothing found in schemas {a.schemas} of {config.PG_DATABASE}.")
    src_label = f"PostgreSQL {config.PG_HOST}:{config.PG_PORT}/{config.PG_DATABASE}"

    if a.dry_run:
        print(f"{src_label}: {len(objs)} objects in {', '.join(a.schemas)} (dry run, SQL Server not contacted)\n")
        for o in objs:
            n = pg.execute(sql.SQL("SELECT count(*) FROM {}.{}").format(sql.Identifier(o["schema"]), sql.Identifier(o["name"]))).fetchone()[0]
            print(f"-- {o['schema']}.{o['name']} ({o['kind']}, {n:,} rows)")
            print(create_table_sql(o) + ";")
            if pk_sql(o):
                print(pk_sql(o) + ";")
            print()
        return

    try:
        import pyodbc
    except ImportError:
        sys.exit("pyodbc is not installed. Run:  pip install pyodbc")
    driver = pick_driver(pyodbc, a.driver)
    print(f"{src_label}\n  -> SQL Server {a.server} / {a.database}   ({driver}, {'SQL login ' + user if user else 'Windows login'})\n")

    master = ms_connect(pyodbc, driver, a.server, "master", user, password, autocommit=True)
    if master.cursor().execute("SELECT DB_ID(?)", a.database).fetchone()[0] is None:
        master.cursor().execute(f"CREATE DATABASE {q(a.database)}")
        print(f"created database {a.database}")
    master.close()
    ms = ms_connect(pyodbc, driver, a.server, a.database, user, password)
    for s in a.schemas:
        if ms.cursor().execute("SELECT SCHEMA_ID(?)", s).fetchone()[0] is None:
            ms.cursor().execute(f"CREATE SCHEMA {q(s)}")          # must be alone in its batch: no parameters here
    ms.commit()

    results, failed, t_all = [], [], time.time()
    width = max(len(f"{o['schema']}.{o['name']}") for o in objs)
    for i, o in enumerate(objs, 1):
        label = f"{o['schema']}.{o['name']}"
        print(f"[{i:>2}/{len(objs)}] {label:<{width}}  {o['kind']:<9} ", end="", flush=True)
        t0 = time.time()
        try:
            copied, note = copy_object(pg, pyodbc, ms, o)
            print(f"{copied:>10,} rows  {time.time() - t0:5.1f}s  {note}")
            results.append((o, copied))
        except Exception as e:
            pg.rollback()
            try:
                ms.rollback()
            except Exception:
                pass
            msg = str(e).split("[SQL Server]")[-1].strip()[:300]
            print(f"FAILED: {msg}")
            failed.append((label, msg))

    print("\nRow count check (PostgreSQL vs SQL Server):")
    mismatch = 0
    for o, _ in results:
        n_pg = pg.execute(sql.SQL("SELECT count(*) FROM {}.{}").format(sql.Identifier(o["schema"]), sql.Identifier(o["name"]))).fetchone()[0]
        n_ms = ms.cursor().execute(f"SELECT COUNT_BIG(*) FROM {q(o['schema'])}.{q(o['name'])}").fetchone()[0]
        ok = n_pg == n_ms
        mismatch += not ok
        if not ok:
            print(f"  DIFFERENT  {o['schema']}.{o['name']}: PostgreSQL {n_pg:,}  SQL Server {n_ms:,}")
    if not mismatch:
        print(f"  all {len(results)} copied objects have the same row count")
    pg.close()
    ms.close()

    print(f"\nDone in {time.time() - t_all:.0f}s: {len(results)} copied, {len(failed)} failed.")
    for label, msg in failed:
        print(f"  failed: {label}: {msg}")
    print(f"Open SSMS > {a.server} > Databases > {a.database} (right-click Databases > Refresh if it is not shown).")
    sys.exit(1 if failed or mismatch else 0)


if __name__ == "__main__":
    main()
