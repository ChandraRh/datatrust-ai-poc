"""
SQL Server connector for the Data Estate portal (SQL Server 2017+ and Azure SQL Database).

Same functions and the same answer shapes as postgres_connector.PostgresSource, so the portal pages work unchanged:
databases, schemas, objects, object detail, search, who-changed-what, failures, users/roles, access requests.

Login: Windows login (empty user, like SSMS on your laptop) or a SQL login whose password is read from .env.
Reads run with a 15 s query timeout and a 10 s lock timeout, and only SELECT from catalog views.
Writes happen only in two places, both started by an admin from the portal:
  * enable_change_tracking()  installs schema dt_audit + one database DDL trigger in ONE database
  * apply()                   runs the statements of an APPROVED access request in one transaction
Names typed by users are checked against the catalog first and always written as [quoted] identifiers.
"""
import secrets
import string
from contextlib import closing
from datetime import date, datetime, time
from decimal import Decimal

from . import store

PRIVILEGES = {                      # what can be granted on each object type (SQL Server names)
    "DATABASE": ["CONNECT", "SELECT", "INSERT", "UPDATE", "DELETE", "EXECUTE", "CREATE TABLE", "CREATE VIEW",
                 "CREATE PROCEDURE", "VIEW DEFINITION", "SHOWPLAN"],
    "SCHEMA": ["SELECT", "INSERT", "UPDATE", "DELETE", "EXECUTE", "ALTER", "VIEW DEFINITION", "CONTROL"],
    "TABLE": ["SELECT", "INSERT", "UPDATE", "DELETE", "REFERENCES", "ALTER", "VIEW DEFINITION", "CONTROL"],
    "VIEW": ["SELECT", "INSERT", "UPDATE", "DELETE", "ALTER", "VIEW DEFINITION", "CONTROL"],
    "FUNCTION": ["EXECUTE", "SELECT", "ALTER", "VIEW DEFINITION", "CONTROL"],
    "PROCEDURE": ["EXECUTE", "ALTER", "VIEW DEFINITION", "CONTROL"],
}
NAMES = {                           # catalog names shown as "Source:" in the estate assistant
    "databases": "sys.databases", "schemas": "sys.schemas", "objects": "sys.objects", "columns": "sys.columns",
    "roles": "sys.database_principals", "grants": "sys.database_permissions", "changes": "dt_audit.ddl_log",
    "failures": "audit.error_log, msdb job history", "health": "sys.dm_os_performance_counters",
}
TRACKING_NOTE = ("Tracking installs schema dt_audit and one database DDL trigger in that database, so every "
                 "CREATE / ALTER / DROP / GRANT / DENY / REVOKE is logged with login, time and client. Needs db_owner.")
TYPE_CODES = {"U": "TABLE", "V": "VIEW", "P": "PROCEDURE", "PC": "PROCEDURE", "FN": "FUNCTION", "IF": "FUNCTION",
              "TF": "FUNCTION", "FS": "FUNCTION", "FT": "FUNCTION", "SO": "SEQUENCE", "SN": "SYNONYM"}
SYSTEM_DBS = ("master", "tempdb", "model", "msdb")
HIDDEN_SCHEMAS = "('sys', 'INFORMATION_SCHEMA', 'guest')"


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


def clean(rows):
    return [{k: plain(v) for k, v in r.items()} for r in rows]


def qn(name: str) -> str:
    """[quoted] SQL Server identifier."""
    return "[" + str(name).replace("]", "]]") + "]"


def _odbc(value) -> str:
    """ODBC connection-string value; put in braces only when it contains characters that could break the string."""
    v = str(value)
    if any(ch in v for ch in ";{}=") or v != v.strip():
        return "{" + v.replace("}", "}}") + "}"
    return v


def conn_str(cfg: dict, database: str = None, password: str = None) -> str:
    """Connection string for a portal connection row. Empty user = Windows login (Trusted_Connection)."""
    s = cfg.get("settings") or {}
    driver = s.get("odbc_driver") or "ODBC Driver 18 for SQL Server"
    trust = "yes" if str(s.get("trust_cert", "yes")).lower().startswith("y") else "no"
    server = f"{cfg['host']},{cfg['port']}" if cfg.get("port") else cfg["host"]       # no port: local / named instance
    parts = [f"DRIVER={{{driver}}}", f"SERVER={_odbc(server)}", f"DATABASE={_odbc(database or cfg.get('database') or 'master')}",
             "Encrypt=yes", f"TrustServerCertificate={trust}", "APP=DataTrust estate portal"]
    if (cfg.get("username") or "").strip():
        parts += [f"UID={_odbc(cfg['username'])}", f"PWD={_odbc(password or '')}"]
    else:
        parts.append("Trusted_Connection=yes")
    return ";".join(parts)


def _new_password() -> str:
    """16 characters with upper, lower and digits: passes SQL Server's password policy."""
    alphabet = string.ascii_letters + string.digits
    while True:
        p = "".join(secrets.choice(alphabet) for _ in range(16))
        if any(c.islower() for c in p) and any(c.isupper() for c in p) and any(c.isdigit() for c in p):
            return p


def check_name(name: str, allow_domain: bool = False):
    """User and role names: letters, digits, _ . - (and one \\ for a Windows account), max 128."""
    ok = name and len(name) <= 128 and all(ch.isalnum() or ch in "_.-" or (allow_domain and ch == "\\") for ch in name)
    if not ok or name.count("\\") > 1:
        raise ValueError("name: letters, digits, _ . - only" + (" (Windows account as DOMAIN\\name)" if allow_domain else "") + ", max 128")


def login_type_of(details: dict, grantee: str) -> str:
    """sql = login with a generated password, windows = DOMAIN\\name, none = database user without login."""
    t = (details or {}).get("login_type") or ("windows" if "\\" in grantee else "sql")
    if t not in ("sql", "windows", "none"):
        raise ValueError("login type must be sql, windows or none")
    return t


def _type_text(r) -> str:
    """sys.columns row -> 'nvarchar(50)', 'decimal(14,2)', 'datetime2(6)' ..."""
    t, n = r["type_name"], r["max_length"]
    if t in ("varchar", "char", "varbinary", "binary"):
        return f"{t}({'max' if n == -1 else n})"
    if t in ("nvarchar", "nchar"):
        return f"{t}({'max' if n == -1 else n // 2})"
    if t in ("decimal", "numeric"):
        return f"{t}({r['precision']},{r['scale']})"
    if t in ("datetime2", "time", "datetimeoffset"):
        return f"{t}({r['scale']})"
    return t


class SqlServerSource:
    NAMES = NAMES
    TRACKING_NOTE = TRACKING_NOTE

    def __init__(self, conn_cfg: dict):
        self.cfg = conn_cfg

    # ------------------------------------------------------------------ connections
    def connect(self, database: str = None, read_only: bool = True):
        try:
            import pyodbc
        except ImportError:
            raise ValueError("SQL Server driver not installed: pip install pyodbc (and Microsoft ODBC Driver 18 for SQL Server)")
        windows = not (self.cfg.get("username") or "").strip()
        password = None if windows else store.resolve_password(self.cfg["password_env"])
        c = pyodbc.connect(conn_str(self.cfg, database, password), autocommit=read_only, timeout=10)
        c.timeout = 15 if read_only else 60                    # seconds per statement
        c.cursor().execute("SET LOCK_TIMEOUT 10000")
        return c

    def q(self, query, params=(), database=None):
        with closing(self.connect(database)) as c:
            cur = c.cursor()
            cur.execute(query, params)
            while cur.description is None and cur.nextset():  # skip row counts of SET / temp statements
                pass
            if cur.description is None:
                return []
            cols = [d[0] for d in cur.description]
            return clean([dict(zip(cols, row)) for row in cur.fetchall()])

    def test(self) -> dict:
        r = self.q("SELECT @@VERSION AS version, SUSER_SNAME() AS login_user, DB_NAME() AS [database]")[0]
        r["version"] = r["version"].split("\n")[0].strip()
        return r

    # ------------------------------------------------------------------ catalog
    def databases(self):
        """Every user database on the server (new databases show up on the next refresh), plus the default one."""
        size = "(SELECT SUM(CAST(f.size AS bigint)) * 8192 FROM sys.master_files f WHERE f.database_id = d.database_id)"
        query = """
            SELECT d.name, SUSER_SNAME(d.owner_sid) AS owner,
                   {size} AS size_bytes,
                   CAST(CASE WHEN d.state_desc = 'ONLINE' AND HAS_DBACCESS(d.name) = 1 THEN 1 ELSE 0 END AS bit) AS can_connect,
                   d.state_desc AS state, d.create_date AS created
            FROM sys.databases d
            WHERE d.name NOT IN ('master', 'tempdb', 'model', 'msdb') OR d.name = ?
            ORDER BY d.name"""
        params = (self.cfg.get("database") or "",)
        try:
            rows = self.q(query.format(size=size), params)
        except Exception:                                      # Azure SQL Database has no sys.master_files
            rows = self.q(query.format(size="CAST(NULL AS bigint)"), params)
        for r in rows:
            r["can_connect"] = bool(r["can_connect"])
        return rows

    def schemas(self, database):
        return self.q(f"""
            SELECT s.name, USER_NAME(s.principal_id) AS owner,
                   (SELECT COUNT(*) FROM sys.objects o WHERE o.schema_id = s.schema_id AND o.type = 'U' AND o.is_ms_shipped = 0) AS tables,
                   (SELECT COUNT(*) FROM sys.objects o WHERE o.schema_id = s.schema_id AND o.type = 'V' AND o.is_ms_shipped = 0) AS views,
                   (SELECT COUNT(*) FROM sys.objects o WHERE o.schema_id = s.schema_id
                        AND o.type IN ('P','PC','FN','IF','TF','FS','FT') AND o.is_ms_shipped = 0) AS routines
            FROM sys.schemas s
            WHERE s.schema_id < 16384 AND s.name NOT IN {HIDDEN_SCHEMAS}
            ORDER BY s.name""", database=database)

    def objects(self, database, schema):
        rows = self.q("""
            SELECT o.name, RTRIM(o.type) AS type_code, USER_NAME(COALESCE(o.principal_id, s.principal_id)) AS owner,
                   (SELECT SUM(p.rows) FROM sys.partitions p WHERE p.object_id = o.object_id AND p.index_id IN (0, 1)) AS est_rows,
                   (SELECT SUM(CAST(a.total_pages AS bigint)) * 8192 FROM sys.partitions p
                        JOIN sys.allocation_units a ON a.container_id = p.partition_id WHERE p.object_id = o.object_id) AS size_bytes,
                   (SELECT COUNT(*) FROM sys.indexes i WHERE i.object_id = o.object_id AND i.index_id > 0) AS indexes,
                   (SELECT CAST(ep.value AS nvarchar(4000)) FROM sys.extended_properties ep
                        WHERE ep.class = 1 AND ep.major_id = o.object_id AND ep.minor_id = 0 AND ep.name = 'MS_Description') AS comment
            FROM sys.objects o JOIN sys.schemas s ON s.schema_id = o.schema_id
            WHERE s.name = ? AND o.is_ms_shipped = 0 AND o.type IN ('U','V','P','PC','FN','IF','TF','FS','FT','SO','SN')
            ORDER BY o.type, o.name""", (schema,), database)
        out = []
        for r in rows:
            r["type"] = TYPE_CODES.get(r.pop("type_code"), "OBJECT")
            if r["type"] != "TABLE":
                r["est_rows"] = r["size_bytes"] = None
            out.append(r)
        return sorted(out, key=lambda r: (r["type"], r["name"]))

    def _object_id(self, c, schema, name):
        row = c.cursor().execute("SELECT OBJECT_ID(QUOTENAME(?) + '.' + QUOTENAME(?)) AS id", (schema, name)).fetchone()
        if not row or row[0] is None:
            raise ValueError(f"{schema}.{name} not found")
        return row[0]

    def object_detail(self, database, schema, name, obj_type):
        """Columns, indexes, constraints, definition, grants, lineage and recent changes of one object."""
        full = f"{schema}.{name}"

        def rows(c, query, params=()):
            cur = c.cursor()
            cur.execute(query, params)
            cols = [d[0] for d in cur.description]
            return clean([dict(zip(cols, r)) for r in cur.fetchall()])

        with closing(self.connect(database)) as c:
            oid = self._object_id(c, schema, name)
            info = rows(c, """
                SELECT USER_NAME(COALESCE(o.principal_id, s.principal_id)) AS owner, RTRIM(o.type) AS type_code,
                       o.create_date AS created, o.modify_date AS modified, OBJECT_DEFINITION(o.object_id) AS definition,
                       (SELECT CAST(ep.value AS nvarchar(4000)) FROM sys.extended_properties ep
                            WHERE ep.class = 1 AND ep.major_id = o.object_id AND ep.minor_id = 0 AND ep.name = 'MS_Description') AS comment,
                       (SELECT SUM(CAST(a.total_pages AS bigint)) * 8192 FROM sys.partitions p
                            JOIN sys.allocation_units a ON a.container_id = p.partition_id WHERE p.object_id = o.object_id) AS size_bytes,
                       (SELECT SUM(p.rows) FROM sys.partitions p WHERE p.object_id = o.object_id AND p.index_id IN (0, 1)) AS row_count
                FROM sys.objects o JOIN sys.schemas s ON s.schema_id = o.schema_id WHERE o.object_id = ?""", (oid,))[0]
            kind = TYPE_CODES.get(info.pop("type_code"), obj_type)
            columns = rows(c, """
                SELECT c.name AS [column], TYPE_NAME(c.user_type_id) AS type_name, c.max_length, c.precision, c.scale,
                       c.is_nullable AS nullable, dc.definition AS default_value, c.is_identity, cc.definition AS computed_as,
                       (SELECT CAST(ep.value AS nvarchar(4000)) FROM sys.extended_properties ep
                            WHERE ep.class = 1 AND ep.major_id = c.object_id AND ep.minor_id = c.column_id AND ep.name = 'MS_Description') AS comment
                FROM sys.columns c
                LEFT JOIN sys.default_constraints dc ON dc.object_id = c.default_object_id
                LEFT JOIN sys.computed_columns cc ON cc.object_id = c.object_id AND cc.column_id = c.column_id
                WHERE c.object_id = ? ORDER BY c.column_id""", (oid,))
            for col in columns:
                col["data_type"] = _type_text(col) + (" IDENTITY" if col.pop("is_identity") else "")
                if col.get("computed_as"):
                    col["default_value"] = "AS " + col["computed_as"]
                for k in ("type_name", "max_length", "precision", "scale", "computed_as"):
                    col.pop(k, None)
            columns = [{k: col[k] for k in ("column", "data_type", "nullable", "default_value", "comment")} for col in columns]
            indexes = rows(c, """
                SELECT i.name AS [index],
                       i.type_desc COLLATE DATABASE_DEFAULT + ' (' + STUFF((SELECT ', ' + COL_NAME(ic.object_id, ic.column_id) COLLATE DATABASE_DEFAULT + CASE WHEN ic.is_descending_key = 1 THEN ' DESC' ELSE '' END
                                                   FROM sys.index_columns ic WHERE ic.object_id = i.object_id AND ic.index_id = i.index_id
                                                     AND ic.is_included_column = 0 ORDER BY ic.key_ordinal FOR XML PATH('')), 1, 2, '') + ')'
                       + ISNULL(' INCLUDE (' + STUFF((SELECT ', ' + COL_NAME(ic.object_id, ic.column_id) COLLATE DATABASE_DEFAULT FROM sys.index_columns ic
                                                      WHERE ic.object_id = i.object_id AND ic.index_id = i.index_id AND ic.is_included_column = 1
                                                      FOR XML PATH('')), 1, 2, '') + ')', '')
                       + ISNULL(' WHERE ' + i.filter_definition COLLATE DATABASE_DEFAULT, '') AS definition,
                       i.is_unique, i.is_primary_key AS is_primary
                FROM sys.indexes i WHERE i.object_id = ? AND i.index_id > 0 ORDER BY i.name""", (oid,))
            constraints = rows(c, """
                SELECT k.name COLLATE DATABASE_DEFAULT AS [constraint], CASE k.type WHEN 'PK' THEN 'PRIMARY KEY' ELSE 'UNIQUE' END AS type,
                       STUFF((SELECT ', ' + COL_NAME(ic.object_id, ic.column_id) COLLATE DATABASE_DEFAULT FROM sys.index_columns ic
                              WHERE ic.object_id = k.parent_object_id AND ic.index_id = k.unique_index_id ORDER BY ic.key_ordinal
                              FOR XML PATH('')), 1, 2, '') AS definition
                FROM sys.key_constraints k WHERE k.parent_object_id = ?
                UNION ALL
                SELECT f.name COLLATE DATABASE_DEFAULT, 'FOREIGN KEY',
                       '(' + STUFF((SELECT ', ' + COL_NAME(fc.parent_object_id, fc.parent_column_id) COLLATE DATABASE_DEFAULT FROM sys.foreign_key_columns fc
                                    WHERE fc.constraint_object_id = f.object_id ORDER BY fc.constraint_column_id FOR XML PATH('')), 1, 2, '')
                       + ') REFERENCES ' + OBJECT_SCHEMA_NAME(f.referenced_object_id) COLLATE DATABASE_DEFAULT + '.' + OBJECT_NAME(f.referenced_object_id) COLLATE DATABASE_DEFAULT
                FROM sys.foreign_keys f WHERE f.parent_object_id = ?
                UNION ALL
                SELECT ck.name COLLATE DATABASE_DEFAULT, 'CHECK', ck.definition COLLATE DATABASE_DEFAULT FROM sys.check_constraints ck WHERE ck.parent_object_id = ?
                ORDER BY 1""", (oid, oid, oid))
            grants = rows(c, """
                SELECT USER_NAME(p.grantee_principal_id) COLLATE DATABASE_DEFAULT AS grantee,
                       p.permission_name COLLATE DATABASE_DEFAULT + CASE WHEN p.state = 'D' THEN ' (DENY)' WHEN p.state = 'W' THEN ' (WITH GRANT)' ELSE '' END
                       + CASE WHEN p.minor_id > 0 THEN ' on column ' + COL_NAME(p.major_id, p.minor_id) COLLATE DATABASE_DEFAULT ELSE '' END AS privilege_type,
                       USER_NAME(p.grantor_principal_id) COLLATE DATABASE_DEFAULT AS grantor, 'object' AS granted_on
                FROM sys.database_permissions p WHERE p.class = 1 AND p.major_id = ?
                UNION ALL
                SELECT USER_NAME(p.grantee_principal_id) COLLATE DATABASE_DEFAULT,
                       p.permission_name COLLATE DATABASE_DEFAULT + CASE WHEN p.state = 'D' THEN ' (DENY)' ELSE '' END,
                       USER_NAME(p.grantor_principal_id) COLLATE DATABASE_DEFAULT, 'schema ' + SCHEMA_NAME(p.major_id) COLLATE DATABASE_DEFAULT
                FROM sys.database_permissions p WHERE p.class = 3 AND p.major_id = SCHEMA_ID(?)
                ORDER BY 1, 2""", (oid, schema))
            uses = rows(c, """
                SELECT DISTINCT COALESCE(d.referenced_schema_name COLLATE DATABASE_DEFAULT, OBJECT_SCHEMA_NAME(d.referenced_id) COLLATE DATABASE_DEFAULT, 'dbo') + '.' + d.referenced_entity_name COLLATE DATABASE_DEFAULT AS object
                FROM sys.sql_expression_dependencies d WHERE d.referencing_id = ? AND d.referenced_entity_name IS NOT NULL
                ORDER BY 1""", (oid,))
            used_by = rows(c, """
                SELECT DISTINCT OBJECT_SCHEMA_NAME(d.referencing_id) COLLATE DATABASE_DEFAULT + '.' + OBJECT_NAME(d.referencing_id) COLLATE DATABASE_DEFAULT AS object
                FROM sys.sql_expression_dependencies d WHERE d.referenced_id = ? AND d.referencing_id <> d.referenced_id
                ORDER BY 1""", (oid,))
        result = {"type": obj_type if obj_type in ("TABLE", "VIEW", "FUNCTION", "PROCEDURE") else kind, "name": full,
                  "owner": info["owner"], "comment": info["comment"], "definition": info["definition"],
                  "size_bytes": info["size_bytes"] if kind == "TABLE" else None,
                  "row_count": info["row_count"] if kind == "TABLE" else None,
                  "columns": columns, "indexes": indexes, "constraints": constraints, "grants": grants,
                  "uses": [u["object"] for u in uses], "used_by": [u["object"] for u in used_by]}
        if kind in ("FUNCTION", "PROCEDURE"):
            result["language"] = "T-SQL"
        result["changes"] = self.changes(database, object_name=full, limit=20) if self.tracking_enabled(database) else []
        return result

    def search(self, database, text):
        return self.q(f"""
            SELECT TOP 100 * FROM (
                SELECT s.name COLLATE DATABASE_DEFAULT AS [schema], o.name COLLATE DATABASE_DEFAULT AS name,
                       CASE RTRIM(o.type) WHEN 'V' THEN 'VIEW' WHEN 'U' THEN 'TABLE'
                            WHEN 'P' THEN 'PROCEDURE' ELSE 'FUNCTION' END AS type
                FROM sys.objects o JOIN sys.schemas s ON s.schema_id = o.schema_id
                WHERE o.is_ms_shipped = 0 AND o.type IN ('U','V','P','PC','FN','IF','TF') AND o.name LIKE ?
                UNION ALL
                SELECT s.name COLLATE DATABASE_DEFAULT, o.name COLLATE DATABASE_DEFAULT + '  (column ' + c.name COLLATE DATABASE_DEFAULT + ')', 'COLUMN'
                FROM sys.columns c JOIN sys.objects o ON o.object_id = c.object_id JOIN sys.schemas s ON s.schema_id = o.schema_id
                WHERE o.is_ms_shipped = 0 AND o.type IN ('U','V') AND c.name LIKE ?
            ) x WHERE x.[schema] NOT IN {HIDDEN_SCHEMAS} ORDER BY 1, 2""", (f"%{text}%", f"%{text}%"), database)

    # ------------------------------------------------------------------ who changed what (database DDL trigger)
    def tracking_enabled(self, database) -> bool:
        return bool(self.q("SELECT 1 AS ok FROM sys.triggers WHERE parent_class = 0 AND name = 'dt_audit_ddl'", database=database))

    def enable_change_tracking(self, database):
        """Installs dt_audit.ddl_log + a database DDL trigger in this database (needs db_owner).
        The trigger runs as dbo, so users without rights on dt_audit can still run DDL, and it never blocks their
        statement: if logging fails, the change goes through and only the log line is lost."""
        statements = [
            "IF SCHEMA_ID('dt_audit') IS NULL EXEC('CREATE SCHEMA dt_audit')",
            """IF OBJECT_ID('dt_audit.ddl_log') IS NULL
               CREATE TABLE dt_audit.ddl_log (
                   log_id          bigint IDENTITY PRIMARY KEY,
                   event_time      datetime2(3)  NOT NULL DEFAULT SYSDATETIME(),
                   username        nvarchar(256) NULL,
                   client_addr     nvarchar(64)  NULL,
                   application     nvarchar(256) NULL,
                   command_tag     nvarchar(128) NULL,
                   object_type     nvarchar(128) NULL,
                   schema_name     nvarchar(128) NULL,
                   object_identity nvarchar(512) NULL,
                   query           nvarchar(4000) NULL)""",
            "DROP TRIGGER IF EXISTS dt_audit_ddl ON DATABASE",
            """CREATE TRIGGER dt_audit_ddl ON DATABASE WITH EXECUTE AS 'dbo'
               FOR DDL_DATABASE_LEVEL_EVENTS
               AS
               BEGIN
                   SET NOCOUNT ON;
                   SET XACT_ABORT OFF;
                   DECLARE @e xml = EVENTDATA();
                   DECLARE @schema nvarchar(128) = @e.value('(/EVENT_INSTANCE/SchemaName)[1]', 'nvarchar(128)');
                   DECLARE @object nvarchar(256) = @e.value('(/EVENT_INSTANCE/ObjectName)[1]', 'nvarchar(256)');
                   IF @schema = N'dt_audit' RETURN;
                   BEGIN TRY
                       INSERT INTO dt_audit.ddl_log (username, client_addr, application, command_tag, object_type, schema_name,
                                                     object_identity, query)
                       VALUES (ORIGINAL_LOGIN(), CONVERT(nvarchar(64), CONNECTIONPROPERTY('client_net_address')), APP_NAME(),
                               @e.value('(/EVENT_INSTANCE/EventType)[1]', 'nvarchar(128)'),
                               @e.value('(/EVENT_INSTANCE/ObjectType)[1]', 'nvarchar(128)'),
                               @schema,
                               CASE WHEN @schema IS NOT NULL AND @schema <> N'' THEN @schema + N'.' ELSE N'' END + ISNULL(@object, N''),
                               LEFT(@e.value('(/EVENT_INSTANCE/TSQLCommand/CommandText)[1]', 'nvarchar(max)'), 4000));
                   END TRY
                   BEGIN CATCH
                   END CATCH
               END""",
        ]
        with closing(self.connect(database, read_only=False)) as c:
            cur = c.cursor()
            for s in statements:
                cur.execute(s)
            c.commit()

    def changes(self, database, object_name=None, username=None, limit=200):
        if not self.tracking_enabled(database):
            return []
        where, params = [], [int(limit)]
        if object_name:
            where.append("(object_identity LIKE ? OR REPLACE(REPLACE(query, '[', ''), ']', '') LIKE ?)")
            params += [f"%{object_name}%", f"%{object_name}%"]
        if username:
            where.append("username = ?")
            params.append(username)
        return self.q(f"""SELECT TOP (?) event_time, username, command_tag, object_type, schema_name,
                                 COALESCE(NULLIF(object_identity, ''), LEFT(query, 160)) AS object_identity,
                                 client_addr, application, query
                          FROM dt_audit.ddl_log {'WHERE ' + ' AND '.join(where) if where else ''}
                          ORDER BY log_id DESC""", tuple(params), database)

    # ------------------------------------------------------------------ what failed
    def failures(self, database, limit=100):
        """DataTrust pipeline errors (audit.error_log) when the database has it, failed SQL Agent jobs,
        and server health counters (deadlocks, since the last SQL Server start)."""
        out = {"pipeline": [], "warnings": [], "jobs": [], "health": None,
               "note": "DataTrust pipeline log (audit.error_log) when the database has it, failed SQL Agent jobs (msdb), "
                       "and SQL Server counters since the last server start."}
        has_log = self.q("SELECT CASE WHEN OBJECT_ID('audit.error_log') IS NULL THEN 0 ELSE 1 END AS ok", database=database)[0]["ok"]
        if has_log:
            out["pipeline"] = self.q("""SELECT TOP (?) logged_at, batch_id, layer, pipeline_id, status, error_type, sql_state,
                                               LEFT(error_message, 500) AS error_message
                                        FROM audit.error_log WHERE status IN ('FAILED', 'CANCELLED') ORDER BY error_id DESC""",
                                     (limit,), database)
            out["warnings"] = self.q("""SELECT TOP (?) logged_at, batch_id, layer, pipeline_id, error_type, LEFT(error_message, 300) AS message
                                        FROM audit.error_log WHERE status = 'WARNING' ORDER BY error_id DESC""", (limit,), database)
        try:
            out["jobs"] = self.q("""
                SELECT TOP (?) j.name AS job, h.step_name AS step,
                       msdb.dbo.agent_datetime(h.run_date, h.run_time) AS run_at, LEFT(h.message, 500) AS error_message
                FROM msdb.dbo.sysjobhistory h JOIN msdb.dbo.sysjobs j ON j.job_id = h.job_id
                WHERE h.run_status = 0 ORDER BY h.instance_id DESC""", (limit,), database)
        except Exception:
            out["jobs"] = []                                   # Azure SQL Database / no rights on msdb
        try:
            h = self.q("""
                SELECT (SELECT TOP 1 cntr_value FROM sys.dm_os_performance_counters
                         WHERE counter_name LIKE 'Number of Deadlocks/sec%' AND instance_name = '_Total') AS deadlocks,
                       (SELECT TOP 1 cntr_value FROM sys.dm_os_performance_counters
                         WHERE counter_name LIKE 'Lock Timeouts/sec%' AND instance_name = '_Total') AS lock_timeouts,
                       (SELECT sqlserver_start_time FROM sys.dm_os_sys_info) AS stats_reset""", database=database)[0]
        except Exception:
            h = {"deadlocks": None, "lock_timeouts": None, "stats_reset": None}  # needs VIEW SERVER STATE
        h["rollbacks"] = None                                  # SQL Server keeps no simple per-database rollback count
        out["health"] = h
        return out

    # ------------------------------------------------------------------ users, roles, access
    def roles(self, database=None):
        """Users and roles of one database (SQL Server grants are per database)."""
        rows = self.q("""
            SELECT dp.name, dp.type_desc AS kind,
                   CAST(CASE WHEN dp.type = 'R' THEN 0 ELSE 1 END AS bit) AS can_login,
                   CAST(CASE WHEN dp.name = 'dbo' OR IS_SRVROLEMEMBER('sysadmin', SUSER_SNAME(dp.sid)) = 1 THEN 1 ELSE 0 END AS bit) AS superuser,
                   SUSER_SNAME(dp.sid) AS login, CAST(dp.is_fixed_role AS bit) AS fixed_role,
                   (SELECT STRING_AGG(r.name, ',') WITHIN GROUP (ORDER BY r.name) FROM sys.database_role_members m
                        JOIN sys.database_principals r ON r.principal_id = m.role_principal_id
                        WHERE m.member_principal_id = dp.principal_id) AS member_of
            FROM sys.database_principals dp
            WHERE dp.type IN ('S', 'U', 'G', 'E', 'X', 'R')
              AND dp.name NOT IN ('sys', 'INFORMATION_SCHEMA', 'guest', 'public')
              AND dp.name NOT LIKE '##%'
            ORDER BY CASE WHEN dp.type = 'R' THEN 1 ELSE 0 END, dp.name""", database=database or self.cfg.get("database"))
        for r in rows:
            r["can_login"], r["superuser"], r["fixed_role"] = bool(r["can_login"]), bool(r["superuser"]), bool(r.get("fixed_role"))
            r["member_of"] = [x for x in (r["member_of"] or "").split(",") if x]
            r["valid_until"] = None
        return rows

    def role_access(self, database, role):
        """Everything a user or role can do in one database: database, schema and object grants (direct + public)."""
        params = (role,)
        principal = "(SELECT principal_id FROM sys.database_principals WHERE name = ?)"
        objects = self.q(f"""
            SELECT SCHEMA_NAME(o.schema_id) AS [schema], o.name AS object,
                   CASE RTRIM(o.type) WHEN 'U' THEN 'TABLE' WHEN 'V' THEN 'VIEW' WHEN 'P' THEN 'PROCEDURE' ELSE 'FUNCTION' END AS type,
                   STRING_AGG(p.permission_name COLLATE DATABASE_DEFAULT + CASE WHEN p.state = 'D' THEN ' (DENY)' ELSE '' END, ', ') AS privileges,
                   CAST(MAX(CASE WHEN p.grantee_principal_id = 0 THEN 1 ELSE 0 END) AS bit) AS via_public
            FROM sys.database_permissions p JOIN sys.objects o ON o.object_id = p.major_id
            WHERE p.class = 1 AND p.minor_id = 0 AND (p.grantee_principal_id = {principal} OR p.grantee_principal_id = 0)
              AND o.is_ms_shipped = 0
            GROUP BY o.schema_id, o.name, o.type ORDER BY 1, 2""", params, database)
        schemas = self.q(f"""
            SELECT SCHEMA_NAME(p.major_id) AS [schema],
                   STRING_AGG(p.permission_name COLLATE DATABASE_DEFAULT + CASE WHEN p.state = 'D' THEN ' (DENY)' ELSE '' END, ', ') AS privileges
            FROM sys.database_permissions p
            WHERE p.class = 3 AND (p.grantee_principal_id = {principal} OR p.grantee_principal_id = 0)
            GROUP BY p.major_id ORDER BY 1""", params, database)
        owned = self.q(f"""
            SELECT TOP 500 s.name AS [schema], o.name AS object
            FROM sys.objects o JOIN sys.schemas s ON s.schema_id = o.schema_id
            WHERE COALESCE(o.principal_id, s.principal_id) = {principal} AND o.type IN ('U', 'V') AND o.is_ms_shipped = 0
            ORDER BY 1, 2""", params, database)
        rights = self.q(f"""
            SELECT CAST(CASE WHEN EXISTS (SELECT 1 FROM sys.database_permissions p WHERE p.class = 0 AND p.permission_name COLLATE DATABASE_DEFAULT = 'CONNECT'
                                          AND p.state IN ('G', 'W') AND p.grantee_principal_id = {principal}) THEN 1 ELSE 0 END AS bit) AS [connect],
                   CAST(CASE WHEN EXISTS (SELECT 1 FROM sys.database_permissions p WHERE p.class = 0
                                          AND p.permission_name COLLATE DATABASE_DEFAULT IN ('CREATE SCHEMA', 'CONTROL', 'ALTER') AND p.state IN ('G', 'W')
                                          AND p.grantee_principal_id = {principal})
                               OR EXISTS (SELECT 1 FROM sys.database_role_members m
                                          JOIN sys.database_principals r ON r.principal_id = m.role_principal_id
                                          WHERE m.member_principal_id = {principal} AND r.name IN ('db_owner', 'db_ddladmin'))
                             THEN 1 ELSE 0 END AS bit) AS create_schemas,
                   (SELECT STRING_AGG(r.name, ', ') FROM sys.database_role_members m
                        JOIN sys.database_principals r ON r.principal_id = m.role_principal_id
                        WHERE m.member_principal_id = {principal}) AS roles""", (role, role, role, role), database)[0]
        rights["connect"], rights["create_schemas"] = bool(rights["connect"]), bool(rights["create_schemas"])
        if rights.get("roles"):                       # db_datareader etc. give access without object grants
            schemas = schemas + [{"schema": "(all schemas)", "privileges": f"through role(s): {rights['roles']}"}]
        return {"database": rights, "schemas": schemas, "objects": objects, "owns": owned}

    # ------------------------------------------------------------------ building access changes (no execution)
    def build_statements(self, request_type, database, details, grantee, password=None):
        """-> (statements, preview). Validates every name against the catalog of that database first."""
        principals = {r["name"]: r for r in self.roles(database)}
        groups = {n for n, r in principals.items() if not r["can_login"]}      # database roles, incl. db_datareader etc.
        stmts = []
        with closing(self.connect(database)) as c:
            cur = c.cursor()

            def scalar(query, *params):
                row = cur.execute(query, params).fetchone()
                return row[0] if row else None

            if request_type == "CREATE_USER":
                login_type = login_type_of(details, grantee)
                check_name(grantee, allow_domain=login_type == "windows")
                if grantee in principals:
                    raise ValueError(f"{grantee} already exists in {database}")
                if login_type == "none":                               # database user only: no login, cannot connect
                    stmts.append(f"CREATE USER {qn(grantee)} WITHOUT LOGIN")
                else:
                    if login_type == "windows" and "\\" not in grantee:
                        raise ValueError("a Windows account is written DOMAIN\\name, e.g. CORP\\priya.s")
                    if not scalar("SELECT COUNT(*) FROM sys.server_principals WHERE name = ?", grantee):
                        if login_type == "windows":
                            stmts.append(f"CREATE LOGIN {qn(grantee)} FROM WINDOWS")
                        else:
                            stmts.append(f"CREATE LOGIN {qn(grantee)} WITH PASSWORD = N'{password or '********'}', CHECK_POLICY = ON")
                    stmts.append(f"CREATE USER {qn(grantee)} FOR LOGIN {qn(grantee)}")
                for role in details.get("member_of", []):
                    if role not in groups:
                        raise ValueError(f"role {role} does not exist in {database}")
                    stmts.append(f"ALTER ROLE {qn(role)} ADD MEMBER {qn(grantee)}")
            elif request_type == "CREATE_ROLE":
                check_name(grantee)
                if grantee in principals:
                    raise ValueError(f"{grantee} already exists in {database}")
                stmts.append(f"CREATE ROLE {qn(grantee)}")
                for member in details.get("members", []):
                    if member not in principals:
                        raise ValueError(f"user {member} does not exist in {database}")
                    stmts.append(f"ALTER ROLE {qn(grantee)} ADD MEMBER {qn(member)}")
            elif request_type == "REVOKE_ROLE":
                role = details.get("role")
                if grantee not in principals:
                    raise ValueError(f"user {grantee} does not exist in {database}")
                if role not in groups:
                    raise ValueError(f"role {role} does not exist in {database}")
                if role not in principals[grantee]["member_of"]:
                    raise ValueError(f"{grantee} is not a member of {role}")
                stmts.append(f"ALTER ROLE {qn(role)} DROP MEMBER {qn(grantee)}")
            elif request_type == "DROP_PRINCIPAL":
                p = principals.get(grantee)
                if not p:
                    raise ValueError(f"{grantee} does not exist in {database}")
                if p["fixed_role"] or p["superuser"] or grantee.lower() in ("dbo", "guest", "public", "sys", "information_schema"):
                    raise ValueError(f"{grantee} is a built-in or owner account and cannot be removed from the portal")
                if not p["can_login"]:
                    members = [n for n, r in principals.items() if grantee in r["member_of"]]
                    if members:
                        raise ValueError(f"role {grantee} still has members ({', '.join(members)}); remove them first")
                    stmts.append(f"DROP ROLE {qn(grantee)}")
                else:
                    stmts.append(f"DROP USER {qn(grantee)}")             # the server login (if any) is kept
            elif request_type == "GRANT_ROLE":
                role = details.get("role")
                if grantee not in principals:
                    raise ValueError(f"user {grantee} does not exist in {database}")
                if role not in groups:
                    raise ValueError(f"role {role} does not exist in {database}")
                stmts.append(f"ALTER ROLE {qn(role)} ADD MEMBER {qn(grantee)}")
            elif request_type in ("GRANT", "REVOKE"):
                if grantee not in principals and grantee != "PUBLIC":
                    raise ValueError(f"{grantee} is not a user or role in {database}. Add them as a team member first "
                                     "(SQL Server needs a database user before anything can be granted).")
                otype = details["object_type"]
                privs = details.get("privileges") or []
                if otype not in PRIVILEGES or not privs or any(p not in PRIVILEGES[otype] for p in privs):
                    raise ValueError(f"privileges {privs} are not valid for {otype}")
                who = "[public]" if grantee == "PUBLIC" else qn(grantee)
                schema, obj = details.get("schema"), details.get("object")
                if otype == "DATABASE":
                    target = ""
                elif otype == "SCHEMA":
                    if scalar("SELECT SCHEMA_ID(?)", schema) is None:
                        raise ValueError(f"schema {schema} does not exist")
                    target = f" ON SCHEMA::{qn(schema)}"
                else:
                    if scalar("SELECT OBJECT_ID(QUOTENAME(?) + '.' + QUOTENAME(?))", schema, obj) is None:
                        raise ValueError(f"{schema}.{obj} does not exist")
                    target = f" ON OBJECT::{qn(schema)}.{qn(obj)}"
                verb, prep = ("GRANT", "TO") if request_type == "GRANT" else ("REVOKE", "FROM")
                stmts.append(f"{verb} {', '.join(privs)}{target} {prep} {who}")
            else:
                raise ValueError(f"unknown request type {request_type}")
        preview = ";\n".join(s.replace(password, "********") if password else s for s in stmts) + ";"
        return stmts, preview

    def apply(self, request_type, database, details, grantee):
        """Run an approved request in ONE transaction. Returns the generated password for a new SQL login (shown once)."""
        password = _new_password() if request_type == "CREATE_USER" and login_type_of(details, grantee) == "sql" else None
        stmts, _ = self.build_statements(request_type, database, details, grantee, password)
        with closing(self.connect(database, read_only=False)) as c:
            cur = c.cursor()
            try:
                for s in stmts:
                    cur.execute(s)
                c.commit()
            except Exception:
                c.rollback()
                raise
        return password if password and any(s.startswith("CREATE LOGIN") for s in stmts) else None
