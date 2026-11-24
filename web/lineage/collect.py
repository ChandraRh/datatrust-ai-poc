"""Collectors: one lineage graph per source, from what the system itself knows. Read only, nothing is changed anywhere.

    collect(scope)   a source of the picker (scopes.list_scopes) -> model.Graph
    postgres(conn)   catalog: relations, view -> table dependencies (pg_depend), function code (reads / writes / calls)
    sqlserver(conn)  catalog: sys.sql_expression_dependencies, procedure / function code (what it writes)
    adf(source_id)   pipeline definitions: Copy source -> target (table names decided at run time come from recent
                     runs), Script and Lookup SQL (parsed), Stored procedure steps, Execute Pipeline
    poc(g, conn)     the POC pipeline's own configuration: source files -> bronze -> silver (its gold views come from
                     the PostgreSQL catalog like any other database)
"""
from __future__ import annotations

from . import model, sql
from .model import Graph, dataset_id, job_id, norm_host

MAX_RELATIONS = 5000
MAX_FUNCTIONS = 2000


def _connection(cid: int) -> dict:
    from estate import store
    return next((c for c in store.list_connections() if c["connection_id"] == cid), {})


def collect(scope: dict) -> Graph:
    if scope["kind"] == "adf":
        return adf(scope["id"], scope["name"])
    conn = _connection(scope["id"])
    st = conn.get("source_type")
    if st == "postgresql":
        g = postgres(conn, scope)
        if scope.get("poc"):
            poc(g, conn, scope)
        return g
    if st == "sqlserver":
        return sqlserver(conn, scope)
    g = Graph()
    g.note(f"Lineage for {model.SYSTEMS.get(st, st)} sources is not read yet; its tables still appear when another "
           "source (e.g. a Data Factory pipeline) reads or writes them.")
    return g


# ============================================================================ helpers shared by the database collectors
class Catalog:
    """The relations and functions of one database, to turn names found in code into real objects."""

    def __init__(self, system: str, host: str, database: str, default_schema: str):
        self.system, self.host, self.database, self.default = system, host, database, default_schema
        self.relations: dict = {}                       # (schema, name) -> "table" | "view"
        self.functions: set = set()                     # (schema, name)

    def find(self, ref: tuple, pool: dict | set) -> tuple | None:
        schema, name = ref
        if schema:
            return (schema, name) if (schema, name) in pool else None
        if (self.default, name) in pool:
            return (self.default, name)
        hits = [k for k in pool if k[1] == name]
        return hits[0] if len(hits) == 1 else None      # unqualified and ambiguous: not guessed

    def node(self, g: Graph, ref: tuple, scope: dict | None = None) -> str:
        return g.dataset(self.system, self.host, self.database, ref[0], ref[1], kind=self.relations.get(ref, "table"),
                         **_owner(scope))

    def fn_id(self, ref: tuple) -> str:
        return job_id("fn", f"{norm_host(self.host)}/{str(self.database).lower()}", f"{ref[0]}.{ref[1]}")


def _owner(scope: dict | None) -> dict:
    return {"scope": scope["key"], "source_name": scope["name"]} if scope else {}


def _add_code(g: Graph, cat: Catalog, fid: str, label: str, found: dict, scope: dict | None, evidence: str) -> bool:
    """A function / procedure node with the tables its code reads and writes and the functions it calls."""
    reads = {r for r in (cat.find(x, cat.relations) for x in found["reads"]) if r}
    writes = {w for w in (cat.find(x, cat.relations) for x in found["writes"]) if w}
    calls = {c for c in (cat.find(x, cat.functions) for x in found["calls"]) if c}
    if not (reads or writes or calls):
        return False
    g.node(fid, "function", label, system=model.SYSTEMS.get(cat.system, cat.system), database=cat.database,
           dynamic_sql=found.get("dynamic") or None, **_owner(scope))
    for r in reads - writes:
        g.edge(cat.node(g, r, scope), fid, evidence)
    for w in writes:
        g.edge(fid, cat.node(g, w, scope), evidence)
    for c in calls:
        g.edge(fid, cat.fn_id(c), evidence, kind="runs")
    return True


# ============================================================================ PostgreSQL
def postgres(conn: dict, scope: dict | None = None) -> Graph:
    from estate import service
    g = Graph()
    src = service.source(conn["connection_id"])
    cat = Catalog("postgresql", conn["host"], conn["database"], "public")
    for r in src.q(f"""SELECT n.nspname AS s, c.relname AS r, c.relkind AS k FROM pg_class c
                       JOIN pg_namespace n ON n.oid = c.relnamespace
                       WHERE c.relkind IN ('r','p','v','m','f') AND n.nspname NOT IN ('pg_catalog','information_schema')
                         AND n.nspname NOT LIKE 'pg\\_%%' LIMIT {MAX_RELATIONS}"""):
        cat.relations[(r["s"].lower(), r["r"].lower())] = "view" if r["k"] in ("v", "m") else "table"
    # views and materialized views: which relations their query reads (the catalog's own dependency records)
    for r in src.q("""SELECT DISTINCT vn.nspname AS vs, v.relname AS vr, tn.nspname AS ts, t.relname AS tr
                      FROM pg_depend d
                      JOIN pg_rewrite rw ON rw.oid = d.objid
                      JOIN pg_class v ON v.oid = rw.ev_class
                      JOIN pg_namespace vn ON vn.oid = v.relnamespace
                      JOIN pg_class t ON t.oid = d.refobjid
                      JOIN pg_namespace tn ON tn.oid = t.relnamespace
                      WHERE d.classid = 'pg_rewrite'::regclass AND d.refclassid = 'pg_class'::regclass
                        AND v.oid <> t.oid AND v.relkind IN ('v','m') AND t.relkind IN ('r','p','v','m','f')
                        AND vn.nspname NOT IN ('pg_catalog','information_schema')"""):
        view, table = (r["vs"].lower(), r["vr"].lower()), (r["ts"].lower(), r["tr"].lower())
        g.edge(cat.node(g, table, scope), cat.node(g, view, scope), "PostgreSQL catalog: the view's query reads it")
    # functions and procedures written in SQL / PL/pgSQL (not those an extension installed)
    funcs = src.q(f"""SELECT n.nspname AS s, p.proname AS f, l.lanname AS lang, p.prosrc AS body
                      FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace JOIN pg_language l ON l.oid = p.prolang
                      WHERE n.nspname NOT IN ('pg_catalog','information_schema') AND n.nspname NOT LIKE 'pg\\_%%'
                        AND l.lanname IN ('sql','plpgsql')
                        AND NOT EXISTS (SELECT 1 FROM pg_depend d WHERE d.objid = p.oid AND d.deptype = 'e')
                      LIMIT {MAX_FUNCTIONS}""")
    cat.functions = {(f["s"].lower(), f["f"].lower()) for f in funcs}
    for f in funcs:
        ref = (f["s"].lower(), f["f"].lower())
        found = sql.analyze(f["body"], "postgres") if f["lang"] == "sql" else sql.procedural(f["body"])
        _add_code(g, cat, cat.fn_id(ref), f"{ref[0]}.{ref[1]}()", found, scope,
                  f"function code ({'SQL' if f['lang'] == 'sql' else 'PL/pgSQL'}) read from the catalog")
    g.catalog = cat                                                  # the POC layer and tests use it
    return g


# ============================================================================ SQL Server
def sqlserver(conn: dict, scope: dict | None = None) -> Graph:
    from estate import service
    g = Graph()
    src = service.source(conn["connection_id"])
    cat = Catalog("sqlserver", conn["host"], conn["database"], "dbo")
    objects = src.q(f"""SELECT TOP {MAX_RELATIONS} SCHEMA_NAME(o.schema_id) AS s, o.name AS n, RTRIM(o.type) AS t
                        FROM sys.objects o WHERE o.is_ms_shipped = 0 AND o.type IN ('U','V','P','FN','IF','TF')""")
    for o in objects:
        ref = (o["s"].lower(), o["n"].lower())
        if o["t"] in ("U", "V"):
            cat.relations[ref] = "view" if o["t"] == "V" else "table"
        else:
            cat.functions.add(ref)
    deps = src.q("""SELECT SCHEMA_NAME(o.schema_id) AS rs, o.name AS rn, RTRIM(o.type) AS rt,
                           COALESCE(d.referenced_schema_name, 'dbo') AS ts, d.referenced_entity_name AS tn
                    FROM sys.sql_expression_dependencies d JOIN sys.objects o ON o.object_id = d.referencing_id
                    WHERE d.referenced_entity_name IS NOT NULL AND d.referenced_database_name IS NULL""")
    code = {(m["s"].lower(), m["n"].lower()): m["definition"] or "" for m in src.q(
        """SELECT SCHEMA_NAME(o.schema_id) AS s, o.name AS n, m.definition FROM sys.sql_modules m
           JOIN sys.objects o ON o.object_id = m.object_id WHERE RTRIM(o.type) IN ('P','FN','IF','TF')""")}
    for d in deps:
        who, what = (d["rs"].lower(), d["rn"].lower()), (d["ts"].lower(), d["tn"].lower())
        if d["rt"] == "V" and what in cat.relations:
            g.edge(cat.node(g, what, scope), cat.node(g, who, scope), "SQL Server catalog: the view's query reads it")
    for ref, body in code.items():
        found = sql.procedural(body)
        found["reads"] |= {(d["ts"].lower(), d["tn"].lower()) for d in deps if (d["rs"].lower(), d["rn"].lower()) == ref}
        _add_code(g, cat, cat.fn_id(ref), f"{ref[0]}.{ref[1]}", found, scope, "procedure code read from the catalog")
    g.catalog = cat
    return g


# ============================================================================ Azure Data Factory
def _asset_node(g: Graph, a: dict, seen_table: tuple | None = None) -> str | None:
    """An ADF dataset asset -> a node id. Database tables get the same id the database's own collector gives them."""
    if not a:
        return None
    kind = a.get("kind")
    if kind == "postgres":
        schema, table = seen_table or (a.get("schema"), a.get("table"))
        if not table or "@" in f"{schema}{table}":
            return None
        return g.dataset("postgresql", a.get("host"), a.get("database"), schema, table, via_adf=True)
    if kind == "sql":
        schema, table = seen_table or (a.get("schema") or "dbo", a.get("table"))
        if not table or "@" in f"{schema}{table}":
            return None
        return g.dataset("sqlserver", a.get("host"), a.get("database"), schema, table, via_adf=True)
    if kind == "storage":
        path = f"{a.get('container') or '?'}/{a.get('path') or ''}".rstrip("/")
        return g.node(model.file_id("adls", a.get("account"), path), "file", _pretty_path(path),
                      system=a.get("system") or "Storage", account=a.get("account"), control=False)
    return g.node(f"other:{a.get('uri') or a.get('name')}", "file", a.get("name") or "?", system=a.get("system"), control=False)


def _pretty_path(path: str) -> str:
    """landing/@pipeline().parameters.p_batch_id/x -> landing/{p_batch_id}/x"""
    import re
    return re.sub(r"@\{?(?:pipeline\(\)\.parameters\.|activity\('[^']+'\)\.output\.[\w\[\]\.]*?\.?)?([\w]+)\}?", r"{\1}", path)


def _script_texts(act: dict) -> list:
    out = []
    for s in act.get("scripts") or []:
        t = s.get("text")
        t = t.get("value") if isinstance(t, dict) else t
        if isinstance(t, str) and t.strip():
            out.append(t)
    return out


def _db_of(info: dict) -> tuple | None:
    if not info or info.get("kind") not in ("postgres", "sql") or not info.get("host"):
        return None
    return ("postgresql" if info["kind"] == "postgres" else "sqlserver", info["host"], info.get("database"))


def adf(source_id: int, source_name: str = "") -> Graph:
    from adf import insights, resolve
    g = Graph()
    defs = insights._definitions(source_id)
    scope = {"key": f"adf:{source_id}", "name": source_name}
    for name in sorted(defs["pipeline"]):
        pid = g.node(job_id("adf", str(source_id), name), "pipeline", name, system="Azure Data Factory", **_owner(scope))
        ctx = insights._default_ctx(defs, name)
        acts = {a["name"]: a for a in resolve.flatten_activities(((defs["pipeline"][name].get("properties") or {}).get("activities")))}
        for e in insights._with_seen_tables(source_id, name, insights._activity_lineage(defs, name, ctx)):
            act = acts.get(e["activity"], {})
            if e["type"] == "Copy":
                src_id = _asset_node(g, e.get("source"))
                tgt = e.get("target") or {}
                seen = e.get("seen_tables") or []
                targets = [_asset_node(g, tgt, tuple(t.split(".", 1))) for t in seen] if seen else [_asset_node(g, tgt)]
                why = f"Copy step {e['activity']}" + (" (table names from recent runs)" if seen else "")
                if src_id:
                    g.edge(src_id, pid, why)
                for t in filter(None, targets):
                    g.edge(pid, t, why)
                if not any(targets):
                    g.note(f"{name}: the target of {e['activity']} is only known at run time and no recent run named it")
            elif e["type"] == "ExecutePipeline":
                g.edge(pid, job_id("adf", str(source_id), e["child_pipeline"]), f"Execute Pipeline step {e['activity']}", kind="runs")
            elif e["type"] in ("Script", "Lookup", "SqlServerStoredProcedure"):
                db = _db_of(e.get("touches") or {})
                if not db:
                    continue
                system, host, database = db
                dialect = sql.DIALECT.get(system, "postgres")
                if e["type"] == "Script":
                    texts = _script_texts(act)
                elif e["type"] == "Lookup":
                    q = (act.get("source") or {}).get("query") or (act.get("source") or {}).get("sqlReaderQuery")
                    texts = [q.get("value") if isinstance(q, dict) else q] if q else []
                else:
                    sp = act.get("stored_procedure")
                    sp = sp.get("value") if isinstance(sp, dict) else sp
                    texts = [f"EXEC {sp}"] if sp else []
                for text in filter(None, texts):
                    found = sql.analyze(text, dialect)
                    why = f"{e['type']} step {e['activity']} (SQL parsed)"
                    for r in found["reads"] - found["writes"]:
                        if r[0]:
                            g.edge(g.dataset(system, host, database, r[0], r[1], via_adf=True), pid, why)
                    for w in found["writes"]:
                        if w[0]:
                            g.edge(pid, g.dataset(system, host, database, w[0], w[1], via_adf=True), why)
                    for c in found["calls"]:                    # only schema-qualified calls: built-ins (now(), count()) are not
                        if c[0] and c[0] not in ("pg_catalog", "sys"):
                            g.edge(pid, job_id("fn", f"{norm_host(host)}/{str(database).lower()}", f"{c[0]}.{c[1]}"), why, kind="runs")
    return g


# ============================================================================ the POC pipeline's own layers
def poc(g: Graph, conn: dict, scope: dict) -> None:
    """Source file -> bronze -> silver, one lane per table (each table is its own Spark step), from the pipeline's
    configuration; rows failing a quality rule go to silver.rejected_records. Gold views come from the catalog."""
    import data_service as ds
    host, db = conn["host"], conn["database"]
    rejected = g.dataset("postgresql", host, db, "silver", "rejected_records", **_owner(scope))
    for entity, (file_name, bronze_t, silver_t, _key, scd) in ds.ENTITIES.items():
        f = g.node(model.file_id("file", "poc-source", file_name), "file", file_name, system="Source files", control=False, **_owner(scope))
        b = g.dataset("postgresql", host, db, *bronze_t.split("."), **_owner(scope))
        s = g.dataset("postgresql", host, db, *silver_t.split("."), **_owner(scope))
        g.edge(f, b, "POC pipeline: bronze load (Spark), rows landed as delivered")
        g.edge(b, s, f"POC pipeline: silver step (Spark), quality rules then {'SCD2 history' if scd == 2 else 'merge'}")
        g.edge(b, rejected, "POC pipeline: silver step, rows failing a quality rule are held back here")
