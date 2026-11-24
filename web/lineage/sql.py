"""SQL understanding for the lineage map: which tables a piece of SQL reads, which it writes, which functions it calls.

    analyze(sql, dialect)   plain SQL (an ADF Script step, a Lookup query, a view, a LANGUAGE sql function):
                            parsed with sqlglot (PostgreSQL, T-SQL, Snowflake); a statement it cannot parse falls back
                            to the patterns below
    procedural(body)        procedure / function code (PL/pgSQL, T-SQL): statement patterns (INSERT INTO x, UPDATE x,
                            FROM x, JOIN x ...); dynamic SQL (EXECUTE format(...)) cannot be read and is counted

Names come back as (schema or None, name), lower case, without quotes. The collectors keep only names that exist
in the database's catalog, so a variable or an alias that looks like a table never becomes one.
"""
from __future__ import annotations

import re

try:
    import logging
    import sqlglot
    from sqlglot import exp
    logging.getLogger("sqlglot").setLevel(logging.ERROR)              # "falling back to Command" is expected, not news
except ImportError:                                                   # the patterns still work without it
    sqlglot = None

DIALECT = {"postgresql": "postgres", "sqlserver": "tsql", "snowflake": "snowflake"}
_PART = r'(?:\[[^\]]+\]|"[^"]+"|[A-Za-z_][\w$]*)'
NAME = rf"({_PART}(?:\s*\.\s*{_PART}){{0,2}})"
WRITE_RE = re.compile(rf"\b(?:INSERT\s+INTO|UPDATE|MERGE\s+INTO|TRUNCATE\s+(?:TABLE\s+)?(?:ONLY\s+)?|DELETE\s+FROM|COPY)\s+(?:ONLY\s+)?{NAME}", re.I)
READ_RE = re.compile(rf"\b(?:FROM|JOIN|USING)\s+(?:ONLY\s+|LATERAL\s+)?{NAME}", re.I)
CALL_RE = re.compile(rf"\b(?:PERFORM|SELECT|CALL|FROM|EXEC(?:UTE)?)\s+(?:\*\s+FROM\s+)?{NAME}\s*\(", re.I)
EXEC_RE = re.compile(rf"\bEXEC(?:UTE)?\s+{NAME}\b(?!\s*\()", re.I)            # T-SQL: EXEC dbo.load_orders @day = ...
DYNAMIC_RE = re.compile(r"\bEXECUTE\s+(?:format\s*\(|'|\w+\s*(?:USING\b|;|$))|\bsp_executesql\b|\bEXEC(?:UTE)?\s*\(", re.I)
NOT_NAMES = {"select", "values", "lateral", "only", "unnest", "generate_series", "jsonb_to_recordset", "json_to_recordset",
             "dual", "set", "where", "into", "table", "the"}


def _clean(text: str) -> str:
    text = re.sub(r"--[^\n]*", " ", text)
    text = re.sub(r"/\*.*?\*/", " ", text, flags=re.S)
    return text


def _no_strings(text: str) -> str:
    return re.sub(r"'(?:[^']|'')*'", "''", text)


def split_name(raw: str) -> tuple | None:
    parts = [p.strip().strip('"[]').lower() for p in re.split(r"\s*\.\s*", raw.strip()) if p.strip()]
    if not parts or parts[-1] in NOT_NAMES or parts[-1].startswith(("@", "$", "v_", "p_")):
        return None
    return (parts[-2] if len(parts) >= 2 else None, parts[-1])


def _patterns(text: str) -> dict:
    out = {"reads": set(), "writes": set(), "calls": set()}
    body = _no_strings(_clean(text))
    for m in WRITE_RE.finditer(body):
        n = split_name(m.group(1))
        if n:
            out["writes"].add(n)
    for m in CALL_RE.finditer(body):
        n = split_name(m.group(1))
        if n:
            out["calls"].add(n)
    for m in EXEC_RE.finditer(body):
        n = split_name(m.group(1))
        if n and n[1] not in ("format", "immediate"):
            out["calls"].add(n)
    reads_text = re.sub(r"\bDELETE\s+FROM\b", "DELETE_ ", body, flags=re.I)   # the table a DELETE empties is written, not read
    for m in READ_RE.finditer(reads_text):
        n = split_name(m.group(1))
        if n and n not in out["calls"]:
            out["reads"].add(n)
    return out


def _table_name(t) -> tuple | None:
    if not isinstance(t, exp.Table) or not t.name:
        return None
    return ((t.db or None) and t.db.lower(), t.name.lower())


def _sqlglot(statement, dialect: str) -> dict | None:
    try:
        trees = sqlglot.parse(statement, read=dialect)
    except Exception:                                                 # noqa: BLE001  not this dialect / not SQL
        return None
    out = {"reads": set(), "writes": set(), "calls": set()}
    for t in trees:
        if t is None:
            continue
        if isinstance(t, exp.Command):                                # EXEC, CALL ...: what sqlglot leaves as text
            return None
        ctes = {c.alias.lower() for c in t.find_all(exp.CTE) if c.alias}
        target = None
        if isinstance(t, (exp.Insert, exp.Update, exp.Delete, exp.Merge, exp.Create)):
            target = t.this.this if isinstance(t.this, exp.Schema) else t.this
            n = _table_name(target)
            if n:
                out["writes"].add(n)
        for f in t.find_all(exp.Anonymous):                           # schema.fn(...) and fn(...)
            parent = f.parent
            if isinstance(parent, exp.Dot):
                out["calls"].add((parent.this.name.lower() or None, f.name.lower()))
            elif isinstance(parent, exp.Table):                       # FROM quality.validate_payments(...)
                out["calls"].add(((parent.db or None) and parent.db.lower(), f.name.lower()))
            else:
                out["calls"].add((None, f.name.lower()))
        for tb in t.find_all(exp.Table):
            if tb is target or isinstance(tb.this, exp.Anonymous):
                continue
            n = _table_name(tb)
            if n and not (n[0] is None and n[1] in ctes):
                out["reads"].add(n)
        if isinstance(t, exp.Delete):
            out["reads"].discard(_table_name(target))
    return out


def analyze(sql: str, dialect: str = "postgres") -> dict:
    """Tables read / written and functions called by plain SQL. ADF parameters (@x, @{...}) are taken as values."""
    text = re.sub(r"@\{[^}]*\}|@@?\w+(?:\(\))?(?:\.[\w\[\]'()]+)*", "NULL", _clean(sql or ""))
    out = {"reads": set(), "writes": set(), "calls": set(), "dynamic": 0, "parsed_by": set()}
    for statement in (s for s in re.split(r";\s*(?:\n|$)", text) if s.strip()):
        got = _sqlglot(statement, dialect) if sqlglot else None
        out["parsed_by"].add("sqlglot" if got is not None else "patterns")
        got = got if got is not None else _patterns(statement)
        for k in ("reads", "writes", "calls"):
            out[k] |= got[k]
    out["dynamic"] = len(DYNAMIC_RE.findall(sql or ""))
    return out


def procedural(body: str) -> dict:
    """Tables a procedure / function reads and writes, functions it calls, and how much dynamic SQL it runs."""
    out = _patterns(body or "")
    out["dynamic"] = len(DYNAMIC_RE.findall(body or ""))
    out["parsed_by"] = {"patterns"}
    return out
