"""
Turn ADF definitions into concrete assets: which storage path a dataset points at, which PostgreSQL table, etc.

Datasets and linked services are often parameterised (@dataset().fileName, @pipeline().parameters.runDate,
@{formatDateTime(pipeline().TriggerTime, 'yyyy/MM/dd')} ...). A small expression evaluator resolves the common
functions using the values of the actual run (its parameters and start time). Anything it cannot work out is kept
as the original expression and listed in asset["unresolved"], so the page says so instead of guessing.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone


class Unresolved(Exception):
    pass


# ============================================================================ expression evaluator
TOKEN = re.compile(r"\s*(?:(?P<str>'(?:[^']|'')*')|(?P<num>-?\d+(?:\.\d+)?)|(?P<id>[A-Za-z_][A-Za-z0-9_]*)|(?P<op>[().,\[\]]))")


def _tokens(text: str) -> list:
    out, pos = [], 0
    while pos < len(text):
        if text[pos:].strip() == "":
            break
        m = TOKEN.match(text, pos)
        if not m:
            raise Unresolved(f"cannot read expression near: {text[pos:pos + 20]}")
        pos = m.end()
        kind = m.lastgroup
        val = m.group(kind)
        if kind == "str":
            val = val[1:-1].replace("''", "'")
        elif kind == "num":
            val = float(val) if "." in val else int(val)
        out.append((kind, val))
    return out


NET_FORMAT = [("yyyy", "%Y"), ("MM", "%m"), ("dd", "%d"), ("HH", "%H"), ("mm", "%M"), ("ss", "%S"), ("yy", "%y")]


def _net_format(dt: datetime, fmt: str) -> str:
    out, i = "", 0
    while i < len(fmt):
        for net, py in NET_FORMAT:
            if fmt.startswith(net, i):
                out += dt.strftime(py)
                i += len(net)
                break
        else:
            if fmt[i] in "fFtzKhMdHmsy":
                raise Unresolved(f"date format '{fmt}' not supported")
            out += fmt[i]
            i += 1
    return out


def _as_dt(v) -> datetime:
    if isinstance(v, datetime):
        return v
    try:
        s = str(v).replace("Z", "+00:00")
        s = re.sub(r"(\.\d{6})\d+", r"\1", s)
        dt = datetime.fromisoformat(s)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        raise Unresolved(f"not a date: {v}")


def _call(name: str, args: list, ctx: dict):
    n = name.lower()
    if n == "pipeline":
        if not ctx.get("pipeline"):
            raise Unresolved("pipeline() not known here")
        return ctx["pipeline"]
    if n == "dataset":
        if "dataset" not in ctx:
            raise Unresolved("dataset() not known here")
        return ctx["dataset"]
    if n == "linkedservice":
        if "linkedservice" not in ctx:
            raise Unresolved("linkedService() not known here")
        return ctx["linkedservice"]
    if n in ("item", "activity", "variables", "trigger"):
        raise Unresolved(f"{name}() is only known while the pipeline runs")
    if n == "concat":
        return "".join("" if a is None else str(a) for a in args)
    if n == "utcnow":
        dt = ctx.get("now")
        if dt is None:
            raise Unresolved("utcNow() depends on when the activity ran")
        return _net_format(dt, args[0]) if args else dt.strftime("%Y-%m-%dT%H:%M:%S.%f0Z")
    if n == "formatdatetime":
        return _net_format(_as_dt(args[0]), args[1] if len(args) > 1 else "yyyy-MM-ddTHH:mm:ss")
    if n in ("adddays", "addhours", "addminutes"):
        unit = {"adddays": "days", "addhours": "hours", "addminutes": "minutes"}[n]
        dt = _as_dt(args[0]) + timedelta(**{unit: int(args[1])})
        return _net_format(dt, args[2]) if len(args) > 2 else dt.isoformat()
    if n == "toupper":
        return str(args[0]).upper()
    if n == "tolower":
        return str(args[0]).lower()
    if n == "trim":
        return str(args[0]).strip()
    if n == "string":
        return "" if args[0] is None else str(args[0])
    if n == "replace":
        return str(args[0]).replace(str(args[1]), str(args[2]))
    if n == "substring":
        s = str(args[0])
        return s[int(args[1]):int(args[1]) + int(args[2])] if len(args) > 2 else s[int(args[1]):]
    if n == "coalesce":
        return next((a for a in args if a not in (None, "")), None)
    if n == "if":
        return args[1] if args[0] in (True, "true", "True") else args[2]
    if n == "equals":
        return args[0] == args[1]
    raise Unresolved(f"function {name}() not supported")


def _eval_tokens(toks: list, i: int, ctx: dict):
    kind, val = toks[i]
    if kind in ("str", "num"):
        value, i = val, i + 1
    elif kind == "id":
        if val.lower() in ("true", "false") and (i + 1 >= len(toks) or toks[i + 1] != ("op", "(")):
            return val.lower() == "true", i + 1
        if i + 1 >= len(toks) or toks[i + 1] != ("op", "("):
            raise Unresolved(f"unexpected name {val}")
        i += 2
        args = []
        while toks[i] != ("op", ")"):
            arg, i = _eval_tokens(toks, i, ctx)
            args.append(arg)
            if toks[i] == ("op", ","):
                i += 1
        i += 1
        value = _call(val, args, ctx)
    else:
        raise Unresolved(f"unexpected '{val}'")
    while i < len(toks) and toks[i] in (("op", "."), ("op", "[")):          # .property  ['property']
        if toks[i] == ("op", "."):
            key = toks[i + 1][1]
            i += 2
        else:
            key, i = _eval_tokens(toks, i + 1, ctx)
            i += 1
        if not isinstance(value, dict):
            raise Unresolved(f"cannot read .{key}")
        match = next((k for k in value if str(k).lower() == str(key).lower()), None)
        if match is None:
            raise Unresolved(f"{key} has no value in this run")
        value = value[match]
    return value, i


def evaluate(expr: str, ctx: dict):
    toks = _tokens(expr)
    if not toks:
        return ""
    try:
        value, i = _eval_tokens(toks, 0, ctx)
    except (IndexError, TypeError, ValueError) as e:
        raise Unresolved(f"cannot evaluate: {e}")
    if i != len(toks):
        raise Unresolved("expression has extra text")
    return value


def resolve_value(value, ctx: dict):
    """A property value from a definition -> its value for this run. Raises Unresolved when it cannot be known."""
    if isinstance(value, dict) and value.get("type") == "Expression":
        value = value.get("value", "")
    if isinstance(value, dict) and "value" in value and value.get("type") in ("SecureString", "AzureKeyVaultSecret"):
        raise Unresolved("secret value")
    if not isinstance(value, str):
        return value
    if value.startswith("@@"):
        return value[1:]
    if value.startswith("@") and not value.startswith("@{"):
        return evaluate(value[1:], ctx)
    if "@{" in value:                                                     # string interpolation
        def sub(m):
            v = evaluate(m.group(1), ctx)
            return "" if v is None else str(v)
        return re.sub(r"@\{([^}]*)\}", sub, value)
    return value


def run_context(run: dict | None, pipeline_name: str = "", factory: str = "") -> dict:
    """pipeline() values of one run, from the stored telemetry.pipeline_runs row."""
    if not run:
        return {}
    raw = run.get("raw") or {}
    start = run.get("run_start")
    start_dt = _as_dt(start) if start else None
    inv = raw.get("invokedBy") or {}
    return {"pipeline": {"parameters": run.get("parameters") or {}, "RunId": run.get("run_id"),
                         "Pipeline": pipeline_name or run.get("pipeline_name"), "DataFactory": factory,
                         "GroupId": run.get("run_group_id"), "TriggerType": inv.get("invokedByType"),
                         "TriggerName": inv.get("name"), "TriggerId": inv.get("id"),
                         "TriggerTime": start_dt.isoformat() if start_dt else None,
                         "TriggeredByPipelineRunId": inv.get("pipelineRunId")},
            "now": start_dt}


# ============================================================================ definitions -> assets
STORAGE_LOCATIONS = {"AzureBlobFSLocation": "ADLS Gen2", "AzureBlobStorageLocation": "Blob Storage",
                     "AzureFileStorageLocation": "Azure Files", "AzureDataLakeStoreLocation": "ADLS Gen1"}
POSTGRES_DATASETS = {"AzurePostgreSqlTable", "PostgreSqlTable", "PostgreSqlV2Table"}
POSTGRES_LINKED = {"AzurePostgreSql", "PostgreSql", "PostgreSqlV2"}
SQL_DATASETS = {"AzureSqlTable", "SqlServerTable", "AzureSqlDWTable", "AzureSqlMITable"}


def _conn_string_parts(cs: str) -> dict:
    parts = {}
    for piece in re.split(r";", cs or ""):
        if "=" in piece:
            k, v = piece.split("=", 1)
            parts[k.strip().lower().replace(" ", "")] = v.strip()
    return parts


def _try(value, ctx, unresolved: list, label: str):
    try:
        return resolve_value(value, ctx)
    except Unresolved:
        unresolved.append(label)
        raw = value.get("value") if isinstance(value, dict) else value
        return raw if isinstance(raw, str) else None


def linked_service_info(ls: dict | None, ls_params: dict, ctx: dict) -> dict:
    """Account / host / database of a linked service (secrets are masked by ADF and never needed here)."""
    if not ls:
        return {}
    props = ls.get("properties") or {}
    tp = props.get("typeProperties") or {}
    ltype = props.get("type")
    unresolved = []
    lctx = {**ctx, "linkedservice": ls_params or {}}
    info = {"linked_service": ls.get("name"), "linked_service_type": ltype, "unresolved": unresolved,
            "connect_via": ((props.get("connectVia") or {}).get("referenceName"))}
    if ltype in ("AzureBlobFS", "AzureBlobStorage", "AzureDataLakeStore"):
        url = _try(tp.get("url") or tp.get("serviceEndpoint") or tp.get("sasUri") or "", lctx, unresolved, "storage url")
        account = None
        if isinstance(url, str):
            m = re.match(r"^https?://([a-z0-9]+)\.(?:dfs|blob)\.", url)
            account = m.group(1) if m else None
        if not account:
            cs = tp.get("connectionString")
            cs = cs.get("value") if isinstance(cs, dict) else cs
            account = _conn_string_parts(cs if isinstance(cs, str) else "").get("accountname")
        info.update(kind="storage", account=account, auth=tp.get("authenticationType") or
                    ("ManagedIdentity" if not (tp.get("accountKey") or tp.get("connectionString") or tp.get("sasUri")
                                                or tp.get("servicePrincipalId")) else "key/SAS/service principal"))
    elif ltype in POSTGRES_LINKED:
        cs = tp.get("connectionString")
        cs_val = cs.get("value") if isinstance(cs, dict) and cs.get("type") != "SecureString" else (cs if isinstance(cs, str) else "")
        parts = _conn_string_parts(cs_val if isinstance(cs_val, str) else "")
        host = tp.get("server") or parts.get("host") or parts.get("server")
        db = tp.get("database") or parts.get("database")
        user = tp.get("username") or parts.get("uid") or parts.get("userid") or parts.get("username") or parts.get("user")
        port = tp.get("port") or parts.get("port") or 5432
        info.update(kind="postgres", host=_try(host, lctx, unresolved, "server") if host else None,
                    database=_try(db, lctx, unresolved, "database") if db else None,
                    username=_try(user, lctx, unresolved, "user name") if user else None,
                    port=int(port) if str(port).isdigit() else 5432,
                    ssl_mode=tp.get("sslMode") or parts.get("sslmode") or parts.get("encryptionmethod"))
        if not host:
            info["note"] = "server name not readable (connection string is stored as a secret in ADF)"
    else:
        info["kind"] = "other"
    return info


def dataset_asset(ds: dict | None, ds_params: dict, ctx: dict, linked: dict) -> dict:
    """A dataset reference (with its parameters) -> the asset it points at in this run."""
    if not ds:
        return {"kind": "unknown", "name": "?", "unresolved": ["dataset definition not found"]}
    props = ds.get("properties") or {}
    tp = props.get("typeProperties") or {}
    dctx = {**ctx, "dataset": {}}
    unresolved: list = []
    # dataset parameter values: the reference's values (evaluated in the pipeline context), else the defaults
    for pname, pdef in (props.get("parameters") or {}).items():
        if pname in (ds_params or {}):
            dctx["dataset"][pname] = _try(ds_params[pname], ctx, unresolved, f"parameter {pname}")
        elif isinstance(pdef, dict) and "defaultValue" in pdef:
            dctx["dataset"][pname] = pdef["defaultValue"]
    ls_ref = props.get("linkedServiceName") or {}
    ls_params = {k: _try(v, dctx, unresolved, f"linked service parameter {k}") for k, v in (ls_ref.get("parameters") or {}).items()}
    ls = linked_service_info(linked.get(ls_ref.get("referenceName")), ls_params, ctx)
    unresolved += ls.get("unresolved", [])
    asset = {"dataset": ds.get("name"), "dataset_type": props.get("type"), "linked_service": ls.get("linked_service"),
             "linked_service_type": ls.get("linked_service_type"), "unresolved": unresolved}
    loc = tp.get("location") or {}
    if loc.get("type") in STORAGE_LOCATIONS or ls.get("kind") == "storage":
        container = loc.get("fileSystem") or loc.get("container")
        asset.update(kind="storage", system=STORAGE_LOCATIONS.get(loc.get("type"), "Storage"), account=ls.get("account"),
                     container=_try(container, dctx, unresolved, "container") if container else None,
                     folder=_try(loc.get("folderPath"), dctx, unresolved, "folder") if loc.get("folderPath") else "",
                     file=_try(loc.get("fileName"), dctx, unresolved, "file name") if loc.get("fileName") else "",
                     format={"type": props.get("type"),
                             "header": tp.get("firstRowAsHeader", False) in (True, "true"),
                             "delimiter": tp.get("columnDelimiter", ","), "compression": tp.get("compressionCodec"),
                             "encoding": tp.get("encodingName")},
                     auth=ls.get("auth"))
        path = "/".join(p.strip("/") for p in (asset["folder"] or "", asset["file"] or "") if p and p.strip("/"))
        asset["path"] = path
        asset["uri"] = f"{'abfss' if asset['system'] == 'ADLS Gen2' else 'wasbs'}://{asset['container'] or '?'}@{asset['account'] or '?'}/{path}"
        asset["name"] = f"{asset['container'] or '?'}/{path}" if path else f"{asset['container'] or '?'}/"
    elif props.get("type") in POSTGRES_DATASETS or ls.get("kind") == "postgres":
        schema = tp.get("schema")
        table = tp.get("table")
        if not table and tp.get("tableName"):
            full = _try(tp["tableName"], dctx, unresolved, "table name")
            if isinstance(full, str) and "." in full:
                schema, table = full.split(".", 1)
            else:
                table = full
        schema = _try(schema, dctx, unresolved, "schema") if schema else "public"
        table = _try(table, dctx, unresolved, "table") if table else None
        clean = lambda s: s.strip('"') if isinstance(s, str) else s                          # noqa: E731
        asset.update(kind="postgres", system="Azure Database for PostgreSQL" if ls.get("linked_service_type") == "AzurePostgreSql"
                     or "Azure" in (props.get("type") or "") else "PostgreSQL",
                     host=ls.get("host"), port=ls.get("port"), database=ls.get("database"), username=ls.get("username"),
                     schema=clean(schema), table=clean(table), note=ls.get("note"))
        asset["name"] = f"{asset['database'] or '?'}.{asset['schema']}.{asset['table'] or '?'}"
        asset["uri"] = f"postgresql://{asset['host'] or '?'}/{asset['database'] or '?'}/{asset['schema']}.{asset['table'] or '?'}"
    elif props.get("type") in SQL_DATASETS:
        asset.update(kind="sql", system="SQL Server", schema=tp.get("schema"), table=tp.get("table"),
                     name=f"{tp.get('schema') or 'dbo'}.{tp.get('table') or tp.get('tableName') or '?'}")
        asset["uri"] = asset["name"]
    else:
        asset.update(kind="other", system=props.get("type"), name=ds.get("name"), uri=f"dataset:{ds.get('name')}")
    return asset


# ============================================================================ pipeline structure
CONTAINERS = {"ForEach": ["activities"], "Until": ["activities"], "IfCondition": ["ifTrueActivities", "ifFalseActivities"],
              "Switch": ["defaultActivities"]}


def flatten_activities(activities: list, parent: str | None = None, branch: str | None = None) -> list:
    """Every activity of a pipeline definition, including those inside ForEach / If / Switch / Until."""
    out = []
    for a in activities or []:
        tp = a.get("typeProperties") or {}
        out.append({"name": a.get("name"), "type": a.get("type"), "parent": parent, "branch": branch,
                    "description": a.get("description"),
                    "depends_on": [{"activity": d.get("activity"), "conditions": d.get("dependencyConditions") or []}
                                   for d in a.get("dependsOn") or []],
                    "inputs": a.get("inputs") or [], "outputs": a.get("outputs") or [],
                    "policy": a.get("policy") or {}, "state": a.get("state"),
                    "linked_service": (a.get("linkedServiceName") or {}).get("referenceName"),
                    "child_pipeline": ((tp.get("pipeline") or {}).get("referenceName")) if a.get("type") == "ExecutePipeline" else None,
                    "source": tp.get("source"), "sink": tp.get("sink"), "translator": tp.get("translator"),
                    "skip_incompatible": tp.get("enableSkipIncompatibleRow"),
                    "scripts": tp.get("scripts") if a.get("type") == "Script" else None,
                    "stored_procedure": tp.get("storedProcedureName"),
                    "dataflow": ((tp.get("dataflow") or tp.get("dataFlow") or {}).get("referenceName"))})
        for key in CONTAINERS.get(a.get("type"), []):
            out += flatten_activities(tp.get(key), a.get("name"), key)
        if a.get("type") == "Switch":
            for case in tp.get("cases") or []:
                out += flatten_activities(case.get("activities"), a.get("name"), f"case {case.get('value')}")
    return out


def copy_assets(activity_def: dict, datasets: dict, linked: dict, ctx: dict) -> dict:
    """Source and sink asset of one Copy activity, resolved for one run (or with defaults when ctx is empty)."""
    out = {}
    for side, refs in (("source", activity_def.get("inputs")), ("target", activity_def.get("outputs"))):
        if refs:
            ref = refs[0]
            asset = dataset_asset(datasets.get(ref.get("referenceName")), ref.get("parameters") or {}, ctx, linked)
            settings = (activity_def.get(side if side == "source" else "sink") or {})
            store = settings.get("storeSettings") or {}
            if side == "source" and asset.get("kind") == "storage":
                for key, label in (("wildcardFolderPath", "wildcard_folder"), ("wildcardFileName", "wildcard_file")):
                    if store.get(key):
                        asset[label] = _try(store[key], ctx, asset["unresolved"], key)
                asset["recursive"] = store.get("recursive")
            if side == "target":
                asset["write"] = {"method": settings.get("writeMethod") or settings.get("writeBehavior"),
                                  "pre_copy_script": (settings.get("preCopyScript") if isinstance(settings.get("preCopyScript"), str)
                                                      else (settings.get("preCopyScript") or {}).get("value")),
                                  "table_option": settings.get("tableOption"), "type": settings.get("type")}
            out[side] = asset
    return out
