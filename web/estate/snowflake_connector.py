"""
Snowflake connector for the Data Estate portal.

Same functions and the same answer shapes as postgres_connector.PostgresSource, so the portal pages work unchanged:
databases, schemas, objects, object detail, search, who-changed-what, failures, users/roles, access requests.

Login: a password from .env, or a key pair (settings.private_key_file = path to the .p8 file; the .env variable then
holds the key's passphrase and may be empty). Snowflake is phasing out password-only logins for service users, so a
key pair is the recommended setup for DataTrust.

Cost: browsing uses SHOW / DESCRIBE only. They run in Snowflake's cloud services and do not start the warehouse.
Rules (checks/engine.py), "who changed what" and "what failed" read data or query history, so they use the warehouse.
Every statement carries QUERY_TAG 'DataTrust' and a statement time limit that Snowflake itself enforces.

Writes happen only in apply(): the statements of an APPROVED access request (GRANT / REVOKE / roles).
Names typed by users are checked against SHOW output first and always written as "quoted" identifiers.
"""
import json
import os
from datetime import date, datetime, time
from decimal import Decimal

from . import store

PRIVILEGES = {                      # what can be granted on each object type (Snowflake names; granted to ROLES)
    "DATABASE": ["USAGE", "MONITOR", "CREATE SCHEMA"],
    "SCHEMA": ["USAGE", "MONITOR", "CREATE TABLE", "CREATE VIEW"],
    "TABLE": ["SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES"],
    "VIEW": ["SELECT", "REFERENCES"],
}
NAMES = {                           # what each page reads, shown as "Source:" in the estate assistant
    "databases": "SHOW DATABASES", "schemas": "SHOW SCHEMAS", "objects": "SHOW TABLES / VIEWS / FUNCTIONS / PROCEDURES",
    "columns": "DESCRIBE TABLE", "roles": "SHOW ROLES, SHOW USERS", "grants": "SHOW GRANTS",
    "changes": "SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY", "failures": "query history, task history",
    "health": "query history",
}
TRACKING_NOTE = ("Snowflake records every statement itself, so nothing is installed. Changes are read from "
                 "SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY when the role may read it (GRANT IMPORTED PRIVILEGES ON DATABASE "
                 "SNOWFLAKE TO ROLE ...; up to 45 min delay), otherwise from the last 7 days of history this role can see.")
SYSTEM_DBS = ("SNOWFLAKE",)
SYSTEM_ROLES = ("ACCOUNTADMIN", "SECURITYADMIN", "SYSADMIN", "USERADMIN", "ORGADMIN", "PUBLIC")
ADMIN_ROLES = ("ACCOUNTADMIN", "SECURITYADMIN", "ORGADMIN")
DDL_TYPES = ("CREATE%", "ALTER%", "DROP%", "GRANT%", "REVOKE%", "RENAME%", "TRUNCATE%", "UNDROP%", "COMMENT%")
BROWSE_SECONDS = 30
TABLE_KINDS = {"TABLE": "TABLE", "TRANSIENT": "TABLE", "TEMPORARY": "TABLE", "DYNAMIC": "DYNAMIC TABLE",
               "EXTERNAL": "EXTERNAL TABLE", "ICEBERG": "TABLE", "HYBRID": "TABLE"}


def plain(v):
    if isinstance(v, Decimal):
        return float(v)
    if isinstance(v, datetime):
        return v.isoformat(sep=" ")
    if isinstance(v, (date, time)):
        return v.isoformat()
    if isinstance(v, (bytes, bytearray, memoryview)):
        return None
    return v


def lower_keys(row: dict) -> dict:
    """Snowflake returns unquoted aliases in upper case (AS n -> N). Keys that are fully upper case are lowered, so the
    rest of DataTrust can read r["n"]; a quoted mixed-case column keeps its spelling."""
    return {(k.lower() if isinstance(k, str) and k == k.upper() else k): v for k, v in row.items()}


def clean(rows):
    return [{k: plain(v) for k, v in lower_keys(r).items()} for r in rows]


def qn(name: str) -> str:
    """"quoted" Snowflake identifier (exact case)."""
    return '"' + str(name).replace('"', '""') + '"'


def lit(text: str) -> str:
    """'quoted' string literal, for SHOW ... LIKE and GET_DDL, where bind values are not accepted everywhere."""
    return "'" + str(text).replace("\\", "\\\\").replace("'", "''") + "'"


def account_of(host: str) -> str:
    """Accept what people paste: myorg-myacct, myorg-myacct.snowflakecomputing.com or the full https:// URL."""
    a = (host or "").strip()
    for prefix in ("https://", "http://"):
        if a.lower().startswith(prefix):
            a = a[len(prefix):]
    a = a.split("/")[0]
    if a.lower().endswith(".snowflakecomputing.com"):
        a = a[: -len(".snowflakecomputing.com")]
    return a


def check_name(name: str):
    ok = name and len(name) <= 255 and all(ch.isalnum() or ch in "_$" for ch in name)
    if not ok:
        raise ValueError("name: letters, digits, _ and $ only, max 255")


def _yes(v) -> bool:
    return str(v).strip().upper() in ("Y", "YES", "TRUE", "T", "1")


def _type_text(data_type: str) -> str:
    """SHOW COLUMNS gives JSON ({"type":"FIXED","precision":38,"scale":0}); DESCRIBE gives NUMBER(38,0). Accept both."""
    try:
        t = json.loads(data_type)
    except (TypeError, ValueError):
        return data_type or ""
    kind = t.get("type", "")
    if kind == "FIXED":
        return f"NUMBER({t.get('precision')},{t.get('scale')})"
    if kind == "TEXT":
        return f"VARCHAR({t.get('length')})"
    return kind


class SnowflakeSource:
    NAMES = NAMES
    TRACKING_NOTE = TRACKING_NOTE

    def __init__(self, conn_cfg: dict):
        self.cfg = conn_cfg
        self._history_source = None

    # ------------------------------------------------------------------ connections
    def login_args(self) -> dict:
        s = self.cfg.get("settings") or {}
        args = {"account": account_of(self.cfg["host"]), "user": self.cfg["username"], "application": "DataTrust",
                "warehouse": s.get("warehouse") or None, "role": s.get("role") or None, "login_timeout": 20}
        key_file = (s.get("private_key_file") or "").strip()
        if key_file:
            if not os.path.exists(key_file):
                raise ValueError(f"private key file not found: {key_file}")
            args.update(authenticator="SNOWFLAKE_JWT", private_key_file=key_file)
            passphrase = None
            try:
                passphrase = store.password_of(self.cfg) if (self.cfg.get("password") or self.cfg.get("password_env")) else None
            except ValueError:
                passphrase = None                                  # an unencrypted key needs no passphrase
            if passphrase:
                args["private_key_password"] = passphrase
        else:
            args["password"] = store.password_of(self.cfg)
        return args

    def connect(self, database: str = None, read_only: bool = True, seconds: int = None):
        try:
            import snowflake.connector
        except ImportError:
            raise ValueError("Snowflake driver not installed: pip install snowflake-connector-python")
        args = self.login_args()
        db = database or self.cfg.get("database") or None
        if db:
            args["database"] = db
        args["session_parameters"] = {"QUERY_TAG": "DataTrust", "STATEMENT_TIMEOUT_IN_SECONDS": int(seconds or BROWSE_SECONDS)}
        c = snowflake.connector.connect(**args)
        if not read_only:
            c.autocommit(False)
        return c

    def q(self, query, params=None, database=None):
        import snowflake.connector
        c = self.connect(database)
        try:
            cur = c.cursor(snowflake.connector.DictCursor)
            cur.execute(query, params or None)
            return clean(cur.fetchall()) if cur.description else []
        finally:
            c.close()

    def test(self) -> dict:
        r = self.q("SELECT CURRENT_VERSION() AS version, CURRENT_USER() AS login_user, CURRENT_DATABASE() AS database, "
                   "CURRENT_ROLE() AS role, CURRENT_WAREHOUSE() AS warehouse")[0]
        r["version"] = f"Snowflake {r['version']}"
        if not r.get("warehouse"):
            r["note"] = "No warehouse in use: browsing works, but rules need one (set Warehouse)."
        return r

    # ------------------------------------------------------------------ catalog (SHOW / DESCRIBE: no warehouse)
    def databases(self):
        rows = self.q("SHOW DATABASES")
        out = [{"name": r["name"], "owner": r.get("owner"), "size_bytes": None, "can_connect": True,
                "state": r.get("kind") or "STANDARD", "created": r.get("created_on"), "comment": r.get("comment")}
               for r in rows if r["name"] not in SYSTEM_DBS]
        return sorted(out, key=lambda r: r["name"])

    def schemas(self, database):
        d = qn(database)
        schemas = self.q(f"SHOW SCHEMAS IN DATABASE {d}", database=database)
        count = {}

        def tally(sql, key):
            try:
                for r in self.q(sql, database=database):
                    if key == "routines" and _yes(r.get("is_builtin")):
                        continue
                    k = (r.get("schema_name"), key)
                    count[k] = count.get(k, 0) + 1
            except Exception:                                          # noqa: BLE001  e.g. no privilege on one kind
                pass

        tally(f"SHOW TABLES IN DATABASE {d}", "tables")
        tally(f"SHOW VIEWS IN DATABASE {d}", "views")
        tally(f"SHOW USER FUNCTIONS IN DATABASE {d}", "routines")
        tally(f"SHOW PROCEDURES IN DATABASE {d}", "routines")
        return [{"name": s["name"], "owner": s.get("owner"), "tables": count.get((s["name"], "tables"), 0),
                 "views": count.get((s["name"], "views"), 0), "routines": count.get((s["name"], "routines"), 0)}
                for s in schemas if s["name"] != "INFORMATION_SCHEMA"]

    def objects(self, database, schema):
        where = f"IN SCHEMA {qn(database)}.{qn(schema)}"
        out = []
        for r in self.q(f"SHOW TABLES {where}", database=database):
            out.append({"name": r["name"], "type": TABLE_KINDS.get(str(r.get("kind") or "TABLE").upper(), "TABLE"),
                        "owner": r.get("owner"), "est_rows": r.get("rows"), "size_bytes": r.get("bytes"), "indexes": 0,
                        "comment": r.get("comment") or None})
        for r in self.q(f"SHOW VIEWS {where}", database=database):
            out.append({"name": r["name"], "type": "MATERIALIZED VIEW" if _yes(r.get("is_materialized")) else "VIEW",
                        "owner": r.get("owner"), "est_rows": None, "size_bytes": None, "indexes": 0, "comment": r.get("comment") or None})
        for sql, kind in ((f"SHOW USER FUNCTIONS {where}", "FUNCTION"), (f"SHOW PROCEDURES {where}", "PROCEDURE")):
            try:
                rows = self.q(sql, database=database)
            except Exception:                                          # noqa: BLE001
                rows = []
            for r in rows:
                if _yes(r.get("is_builtin")):
                    continue
                args = str(r.get("arguments") or r["name"]).split(" RETURN ")[0]
                out.append({"name": args, "type": kind, "owner": r.get("owner"), "est_rows": None, "size_bytes": None,
                            "indexes": None, "comment": r.get("description") or None})
        return sorted(out, key=lambda r: (r["type"], r["name"]))

    def _show_one(self, kind, database, schema, name):
        rows = self.q(f"SHOW {kind} LIKE {lit(name)} IN SCHEMA {qn(database)}.{qn(schema)}", database=database)
        return next((r for r in rows if r["name"] == name), None)

    def object_detail(self, database, schema, name, obj_type):
        """Columns, constraints, definition, grants, lineage and recent changes of one object."""
        full = f"{qn(database)}.{qn(schema)}.{qn(name.split('(')[0])}"
        routine = obj_type in ("FUNCTION", "PROCEDURE")
        info, columns, constraints, grants, definition = {}, [], [], [], None
        if routine:
            kind = "USER FUNCTIONS" if obj_type == "FUNCTION" else "PROCEDURES"
            rows = self.q(f"SHOW {kind} LIKE {lit(name.split('(')[0])} IN SCHEMA {qn(database)}.{qn(schema)}", database=database)
            info = next((r for r in rows if str(r.get("arguments") or "").split(" RETURN ")[0] == name), rows[0] if rows else {})
            try:
                definition = self.q(f"SELECT GET_DDL({lit(obj_type)}, {lit(f'{qn(database)}.{qn(schema)}.' + name)}) AS ddl",
                                    database=database)[0]["ddl"]
            except Exception:                                          # noqa: BLE001
                definition = None
        else:
            info = self._show_one("TABLES", database, schema, name) or self._show_one("VIEWS", database, schema, name) or {}
            if not info:
                raise ValueError(f"{schema}.{name} not found (or this role cannot see it)")
            is_view = "text" in info
            definition = info.get("text") if is_view else None
            for r in self.q(f"DESCRIBE {'VIEW' if is_view else 'TABLE'} {full}", database=database):
                if str(r.get("kind", "COLUMN")).upper() != "COLUMN":
                    continue
                columns.append({"column": r["name"], "data_type": _type_text(r.get("type")), "nullable": _yes(r.get("null?")),
                                "default_value": r.get("default") or (("AS " + r["expression"]) if r.get("expression") else None),
                                "comment": r.get("comment") or None})
            constraints = self._constraints(full, database)
            if definition is None:
                try:
                    definition = self.q(f"SELECT GET_DDL('TABLE', {lit(full)}) AS ddl", database=database)[0]["ddl"]
                except Exception:                                      # noqa: BLE001  needs a warehouse; optional
                    definition = None
        try:
            on = obj_type if obj_type in ("TABLE", "VIEW") else ("VIEW" if "text" in info else "TABLE")
            target = full if not routine else f"{qn(database)}.{qn(schema)}.{name}"
            for g in self.q(f"SHOW GRANTS ON {'FUNCTION' if obj_type == 'FUNCTION' else 'PROCEDURE' if routine else on} {target}",
                            database=database):
                grants.append({"grantee": g.get("grantee_name"), "privilege_type": g.get("privilege") + (" (WITH GRANT)" if _yes(g.get("grant_option")) else ""),
                               "grantor": g.get("granted_by"), "granted_on": str(g.get("granted_to") or "").lower() or "object"})
        except Exception:                                              # noqa: BLE001
            pass
        uses, used_by = self._lineage(database, schema, name)
        kind = obj_type if obj_type in ("TABLE", "VIEW", "FUNCTION", "PROCEDURE") else ("VIEW" if "text" in info else "TABLE")
        result = {"type": kind, "name": f"{schema}.{name}", "owner": info.get("owner"),
                  "comment": info.get("comment") or info.get("description") or None, "definition": definition,
                  "size_bytes": info.get("bytes"), "row_count": info.get("rows"),
                  "columns": columns, "indexes": [], "constraints": constraints, "grants": grants, "uses": uses, "used_by": used_by,
                  "changes": self.changes(database, object_name=f"{schema}.{name.split('(')[0]}", limit=20)}
        if routine:
            result["language"] = info.get("language") or "SQL"
        return result

    def _constraints(self, full, database):
        out = []
        for sql, label in ((f"SHOW PRIMARY KEYS IN TABLE {full}", "PRIMARY KEY"), (f"SHOW UNIQUE KEYS IN TABLE {full}", "UNIQUE")):
            try:
                rows = self.q(sql, database=database)
            except Exception:                                          # noqa: BLE001
                continue
            by = {}
            for r in sorted(rows, key=lambda r: int(r.get("key_sequence") or 0)):
                by.setdefault(r.get("constraint_name"), []).append(r.get("column_name"))
            out += [{"constraint": k, "type": label, "definition": ", ".join(v)} for k, v in by.items()]
        try:
            fks = {}
            for r in self.q(f"SHOW IMPORTED KEYS IN TABLE {full}", database=database):
                f = fks.setdefault(r.get("fk_name"), {"cols": [], "ref": f"{r.get('pk_schema_name')}.{r.get('pk_table_name')}", "refcols": []})
                f["cols"].append(r.get("fk_column_name"))
                f["refcols"].append(r.get("pk_column_name"))
            out += [{"constraint": k, "type": "FOREIGN KEY", "definition": f"({', '.join(v['cols'])}) REFERENCES {v['ref']} ({', '.join(v['refcols'])})"}
                    for k, v in fks.items()]
        except Exception:                                              # noqa: BLE001
            pass
        return out

    def _lineage(self, database, schema, name):
        """Views and tables this object reads, and what reads it (SNOWFLAKE.ACCOUNT_USAGE.OBJECT_DEPENDENCIES, when readable)."""
        try:
            rows = self.q("""SELECT referencing_schema || '.' || referencing_object_name AS referencing,
                                    referenced_schema || '.' || referenced_object_name AS referenced,
                                    referencing_database, referenced_database,
                                    referencing_schema, referencing_object_name, referenced_schema, referenced_object_name
                             FROM SNOWFLAKE.ACCOUNT_USAGE.OBJECT_DEPENDENCIES
                             WHERE (referencing_database = %(d)s AND referencing_schema = %(s)s AND referencing_object_name = %(n)s)
                                OR (referenced_database = %(d)s AND referenced_schema = %(s)s AND referenced_object_name = %(n)s)""",
                          {"d": database, "s": schema, "n": name}, database)
        except Exception:                                              # noqa: BLE001  no IMPORTED PRIVILEGES / no warehouse
            return [], []
        uses = sorted({r["referenced"] for r in rows if r["referencing_object_name"] == name and r["referencing_schema"] == schema})
        used_by = sorted({r["referencing"] for r in rows if r["referenced_object_name"] == name and r["referenced_schema"] == schema})
        return uses, used_by

    def search(self, database, text):
        pattern = lit(f"%{text}%")
        d = qn(database)
        out = []
        for sql, kind in ((f"SHOW TABLES LIKE {pattern} IN DATABASE {d}", "TABLE"), (f"SHOW VIEWS LIKE {pattern} IN DATABASE {d}", "VIEW")):
            out += [{"schema": r["schema_name"], "name": r["name"], "type": kind} for r in self.q(sql, database=database)
                    if r.get("schema_name") != "INFORMATION_SCHEMA"]
        try:
            out += [{"schema": r["schema_name"], "name": f"{r['table_name']}  (column {r['column_name']})", "type": "COLUMN"}
                    for r in self.q(f"SHOW COLUMNS LIKE {pattern} IN DATABASE {d}", database=database)
                    if r.get("schema_name") != "INFORMATION_SCHEMA"]
        except Exception:                                              # noqa: BLE001
            pass
        return sorted(out, key=lambda r: (r["schema"], r["name"]))[:100]

    # ------------------------------------------------------------------ who changed what (Snowflake's own query history)
    def tracking_enabled(self, database) -> bool:
        return True                                                    # Snowflake keeps the history itself

    def enable_change_tracking(self, database):
        return None                                                    # nothing to install

    def _history(self, database, where: str, params: dict, limit: int, failed: bool):
        """Query history rows, newest first: ACCOUNT_USAGE (whole account, needs IMPORTED PRIVILEGES) when readable,
        else the INFORMATION_SCHEMA table function (last 7 days, what this role can see)."""
        status = "execution_status = 'FAILED_WITH_ERROR'" if failed else "execution_status = 'SUCCESS'"
        cols = ("start_time AS event_time, user_name AS username, query_type AS command_tag, schema_name, "
                "query_text AS query, error_code, error_message, role_name, warehouse_name")
        params = {**params, "db": database, "n": int(limit)}
        sources = []
        if self._history_source in (None, "account"):
            sources.append(("account", f"""SELECT {cols} FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY
                WHERE start_time > DATEADD(day, -30, CURRENT_TIMESTAMP()) AND database_name = %(db)s AND {status} {where}
                ORDER BY start_time DESC LIMIT %(n)s"""))
        sources.append(("schema", f"""SELECT {cols} FROM TABLE({qn(database)}.INFORMATION_SCHEMA.QUERY_HISTORY(
                    END_TIME_RANGE_START => DATEADD(day, -7, CURRENT_TIMESTAMP()), RESULT_LIMIT => 10000))
                WHERE database_name = %(db)s AND {status} {where}
                ORDER BY start_time DESC LIMIT %(n)s"""))
        last = None
        for name, sql in sources:
            try:
                rows = self.q(sql, params, database)
                self._history_source = name
                return rows
            except Exception as e:                                     # noqa: BLE001
                last = e
        raise last

    def changes(self, database, object_name=None, username=None, limit=200):
        where, params = [" AND (" + " OR ".join(f"query_type ILIKE %(t{i})s" for i in range(len(DDL_TYPES))) + ")"], \
            {f"t{i}": t.replace("%", "") + "%" for i, t in enumerate(DDL_TYPES)}
        if object_name:
            where.append(" AND REPLACE(query_text, '\"', '') ILIKE %(o)s")
            params["o"] = f"%{object_name}%"
        if username:
            where.append(" AND user_name = %(u)s")
            params["u"] = username
        try:
            rows = self._history(database, "".join(where), params, limit, failed=False)
        except Exception:                                              # noqa: BLE001  no warehouse / no rights: no history
            return []
        return [{"event_time": r["event_time"], "username": r["username"], "command_tag": r["command_tag"], "object_type": None,
                 "schema_name": r["schema_name"], "object_identity": " ".join(str(r["query"] or "").split())[:160],
                 "client_addr": None, "application": r.get("role_name"), "query": r["query"]} for r in rows]

    # ------------------------------------------------------------------ what failed
    def failures(self, database, limit=100):
        """Failed statements of this database (query history) and failed Snowflake tasks."""
        out = {"pipeline": [], "warnings": [], "jobs": [], "health": None,
               "note": "Failed statements on this database (Snowflake query history) and failed tasks (task history). "
                       "Reading history uses the warehouse."}
        try:
            out["pipeline"] = [{"logged_at": r["event_time"], "batch_id": None, "layer": r.get("schema_name"),
                                "pipeline_id": r.get("command_tag"), "status": "FAILED", "error_type": r.get("error_code"),
                                "sql_state": None, "error_message": str(r.get("error_message") or "")[:500]}
                               for r in self._history(database, "", {}, limit, failed=True)]
        except Exception as e:                                         # noqa: BLE001
            out["note"] += f" Query history could not be read: {type(e).__name__}."
        try:
            out["jobs"] = self.q(f"""SELECT name AS job, schema_name AS step, scheduled_time AS run_at, LEFT(error_message, 500) AS error_message
                                     FROM TABLE({qn(database)}.INFORMATION_SCHEMA.TASK_HISTORY(
                                         SCHEDULED_TIME_RANGE_START => DATEADD(day, -7, CURRENT_TIMESTAMP()), RESULT_LIMIT => 1000))
                                     WHERE state = 'FAILED' ORDER BY scheduled_time DESC LIMIT %(n)s""", {"n": int(limit)}, database)
        except Exception:                                              # noqa: BLE001
            out["jobs"] = []
        out["health"] = {"deadlocks": None, "lock_timeouts": None, "rollbacks": None, "stats_reset": None,
                         "failed_statements": len(out["pipeline"]), "failed_tasks": len(out["jobs"])}
        return out

    # ------------------------------------------------------------------ users, roles, access
    def roles(self, database=None):
        """Roles (granted privileges) and users (who get roles). Snowflake roles are account-wide."""
        out = []
        for r in self.q("SHOW ROLES"):
            out.append({"name": r["name"], "kind": "ROLE", "can_login": False, "superuser": r["name"] in ADMIN_ROLES,
                        "login": None, "fixed_role": r["name"] in SYSTEM_ROLES, "member_of": [], "valid_until": None,
                        "comment": r.get("comment")})
        try:
            users = self.q("SHOW USERS")                               # needs MANAGE GRANTS or ownership of the users
        except Exception:                                              # noqa: BLE001
            users = []
        for u in users[:200]:
            member_of = []
            if len(users) <= 50:
                try:
                    member_of = sorted(g["role"] for g in self.q(f"SHOW GRANTS TO USER {qn(u['name'])}") if g.get("role"))
                except Exception:                                      # noqa: BLE001
                    member_of = []
            out.append({"name": u["name"], "kind": "USER", "can_login": not _yes(u.get("disabled")),
                        "superuser": any(m in ADMIN_ROLES for m in member_of), "login": u.get("login_name"), "fixed_role": False,
                        "member_of": member_of or ([u["default_role"]] if u.get("default_role") else []),
                        "valid_until": u.get("expires_at_time")})
        out.sort(key=lambda r: (not r["can_login"], r["name"]))
        return out

    def _role_grants(self, role):
        return self.q(f"SHOW GRANTS TO ROLE {qn(role)}")

    def role_access(self, database, role):
        """What a role (or a user, through its roles) can do in one database: database, schema and object privileges."""
        principals = {r["name"]: r for r in self.roles(database)}
        p = principals.get(role)
        roles = [role] if not p or not p["can_login"] else list(p["member_of"])
        roles = list(dict.fromkeys(roles + ["PUBLIC"]))
        db_rights, schemas, objects, owned = set(), {}, {}, []
        prefix = f"{database}."
        for r in roles:
            try:
                grants = self._role_grants(r)
            except Exception:                                          # noqa: BLE001
                continue
            for g in grants:
                on, name, priv = str(g.get("granted_on") or "").upper(), str(g.get("name") or ""), g.get("privilege")
                bare = name.replace('"', "")
                if on == "DATABASE" and bare == database:
                    db_rights.add(priv)
                elif on == "SCHEMA" and bare.startswith(prefix):
                    schemas.setdefault(bare[len(prefix):], set()).add(priv)
                elif on in ("TABLE", "VIEW", "MATERIALIZED_VIEW", "DYNAMIC_TABLE", "EXTERNAL_TABLE") and bare.startswith(prefix):
                    sch, _, obj = bare[len(prefix):].partition(".")
                    key = (sch, obj, on.replace("_", " "))
                    objects.setdefault(key, {"privs": set(), "public": False})
                    objects[key]["privs"].add(priv)
                    objects[key]["public"] = objects[key]["public"] or r == "PUBLIC"
                    if priv == "OWNERSHIP":
                        owned.append({"schema": sch, "object": obj})
        return {"database": {"connect": "USAGE" in db_rights or "OWNERSHIP" in db_rights,
                             "create_schemas": "CREATE SCHEMA" in db_rights or "OWNERSHIP" in db_rights,
                             "roles": ", ".join(x for x in roles if x != "PUBLIC")},
                "schemas": [{"schema": s, "privileges": ", ".join(sorted(v))} for s, v in sorted(schemas.items())],
                "objects": [{"schema": k[0], "object": k[1], "type": k[2], "privileges": ", ".join(sorted(v["privs"])),
                             "via_public": v["public"]} for k, v in sorted(objects.items())],
                "owns": sorted(owned, key=lambda r: (r["schema"], r["object"]))[:500]}

    # ------------------------------------------------------------------ building access changes (no execution)
    def build_statements(self, request_type, database, details, grantee, password=None):
        """-> (statements, preview). Privileges go to ROLES; users get roles. Names are checked against SHOW output first."""
        principals = {r["name"]: r for r in self.roles(database)}
        roles = {n for n, r in principals.items() if r["kind"] == "ROLE"}
        users = {n for n, r in principals.items() if r["kind"] == "USER"}
        stmts = []
        if request_type == "CREATE_USER":
            raise ValueError("create Snowflake users in Snowflake itself (they need MFA or a key pair); "
                             "then give them a role here with 'grant role'")
        if request_type == "CREATE_ROLE":
            check_name(grantee)
            if grantee in roles:
                raise ValueError(f"role {grantee} already exists")
            stmts.append(f"CREATE ROLE {qn(grantee)}")
            for member in details.get("members", []):
                if member not in users:
                    raise ValueError(f"user {member} does not exist")
                stmts.append(f"GRANT ROLE {qn(grantee)} TO USER {qn(member)}")
        elif request_type in ("GRANT_ROLE", "REVOKE_ROLE"):
            role = details.get("role")
            if role not in roles:
                raise ValueError(f"role {role} does not exist")
            if grantee in users:
                to = f"USER {qn(grantee)}"
            elif grantee in roles:
                to = f"ROLE {qn(grantee)}"
            else:
                raise ValueError(f"{grantee} is not a user or role")
            if request_type == "REVOKE_ROLE" and grantee in users and role not in principals[grantee]["member_of"]:
                raise ValueError(f"{grantee} does not have role {role}")
            stmts.append(f"GRANT ROLE {qn(role)} TO {to}" if request_type == "GRANT_ROLE" else f"REVOKE ROLE {qn(role)} FROM {to}")
        elif request_type == "DROP_PRINCIPAL":
            if grantee not in roles:
                raise ValueError("only roles can be removed from the portal; disable users in Snowflake itself")
            if principals[grantee]["fixed_role"]:
                raise ValueError(f"{grantee} is a built-in Snowflake role and cannot be removed")
            stmts.append(f"DROP ROLE {qn(grantee)}")
        elif request_type in ("GRANT", "REVOKE"):
            if grantee in users:
                raise ValueError(f"{grantee} is a user. In Snowflake privileges go to a role: grant them to a role, "
                                 "then give the user that role.")
            if grantee not in roles and grantee != "PUBLIC":
                raise ValueError(f"{grantee} is not a role")
            otype = details["object_type"]
            privs = details.get("privileges") or []
            if otype not in PRIVILEGES or not privs or any(p not in PRIVILEGES[otype] for p in privs):
                raise ValueError(f"privileges {privs} are not valid for {otype}")
            schema, obj = details.get("schema"), details.get("object")
            if otype == "DATABASE":
                target = f"DATABASE {qn(database)}"
            elif otype == "SCHEMA":
                if not any(s["name"] == schema for s in self.q(f"SHOW SCHEMAS IN DATABASE {qn(database)}", database=database)):
                    raise ValueError(f"schema {schema} does not exist")
                target = f"SCHEMA {qn(database)}.{qn(schema)}"
            else:
                if not self._show_one("TABLES" if otype == "TABLE" else "VIEWS", database, schema, obj):
                    raise ValueError(f"{schema}.{obj} does not exist")
                target = f"{otype} {qn(database)}.{qn(schema)}.{qn(obj)}"
            verb, prep = ("GRANT", "TO") if request_type == "GRANT" else ("REVOKE", "FROM")
            stmts.append(f"{verb} {', '.join(privs)} ON {target} {prep} ROLE {qn(grantee)}")
        else:
            raise ValueError(f"unknown request type {request_type}")
        return stmts, ";\n".join(stmts) + ";"

    def apply(self, request_type, database, details, grantee):
        """Run an approved request. Snowflake commits each GRANT / CREATE ROLE on its own, so statements run in order and
        stop at the first error (the error names it). Needs a role with MANAGE GRANTS (e.g. SECURITYADMIN)."""
        stmts, _ = self.build_statements(request_type, database, details, grantee)
        c = self.connect(database, seconds=60)
        try:
            cur = c.cursor()
            for s in stmts:
                cur.execute(s)
        finally:
            c.close()
        return None
