"""
Source types the portal knows: the fields the "Add a source" wizard asks for, and a connection test per type.

Browsing (catalog, changes, access) needs a full connector (service.CONNECTORS): PostgreSQL, SQL Server, Snowflake.
SAP HANA can already be added and tested; its connector comes next.
The Python driver for each type is imported only when that type is used, so nothing extra is needed until then.
"""
from . import store

SOURCE_TYPES = {
    "postgresql": {
        "label": "PostgreSQL", "browse": True, "driver": "psycopg (already installed)",
        "fields": [
            {"key": "host", "label": "Host", "placeholder": "e.g. 10.20.30.45 or localhost", "required": True},
            {"key": "port", "label": "Port", "default": "5432", "required": True},
            {"key": "database", "label": "Database", "placeholder": "e.g. sales_dw", "required": True},
            {"key": "username", "label": "Login user", "required": True},
        ],
    },
    "snowflake": {
        "label": "Snowflake", "browse": True, "driver": "snowflake-connector-python (pip install snowflake-connector-python)",
        "password_label": "Password or key passphrase",
        "password_hint": ("Password login: enter the user's password. Key-pair login (recommended for a service user): fill in "
                          "the private key file and enter the key's passphrase here (leave it empty for an unencrypted key)."),
        "fields": [
            {"key": "host", "label": "Account identifier", "placeholder": "e.g. myorg-myaccount (from your Snowflake URL)", "required": True},
            {"key": "username", "label": "Login user", "placeholder": "e.g. DATATRUST_SVC", "required": True},
            {"key": "warehouse", "label": "Warehouse", "placeholder": "e.g. COMPUTE_WH (an X-Small with auto-suspend is enough)", "required": True},
            {"key": "role", "label": "Role", "placeholder": "e.g. DATATRUST_READ (a read-only role)", "required": False},
            {"key": "database", "label": "Database", "placeholder": "e.g. SALES_DW", "required": True},
            {"key": "private_key_file", "label": "Private key file (key-pair login)", "placeholder": "empty = password login; e.g. C:/keys/datatrust.p8", "required": False},
        ],
    },
    "sqlserver": {
        "label": "SQL Server", "browse": True, "driver": "pip install pyodbc + Microsoft ODBC Driver 18 for SQL Server",
        "fields": [
            {"key": "host", "label": "Server", "placeholder": "e.g. localhost, localhost\\SQLEXPRESS or myserver.database.windows.net", "required": True},
            {"key": "port", "label": "Port", "default": "", "placeholder": "empty = local / named instance; 1433 for a remote server", "required": False},
            {"key": "database", "label": "Database", "placeholder": "e.g. DataTrustPOC", "required": True},
            {"key": "username", "label": "SQL login user (empty = Windows login)", "placeholder": "empty = your Windows login, like SSMS", "required": False},
            {"key": "odbc_driver", "label": "ODBC driver", "default": "ODBC Driver 18 for SQL Server", "required": True},
            {"key": "trust_cert", "label": "Trust server certificate (yes/no)", "default": "yes", "required": False},
        ],
    },
    "saphana": {
        "label": "SAP HANA", "browse": False, "driver": "pip install hdbcli",
        "fields": [
            {"key": "host", "label": "Host", "placeholder": "e.g. hana01.company.local", "required": True},
            {"key": "port", "label": "SQL port", "default": "30015", "placeholder": "3<instance>15, or 443 for HANA Cloud", "required": True},
            {"key": "database", "label": "Tenant database", "placeholder": "optional, e.g. HXE", "required": False},
            {"key": "username", "label": "Login user", "required": True},
            {"key": "encrypt", "label": "Encrypted connection (yes/no)", "default": "yes", "required": False},
        ],
    },
}
DEFAULT_PORT = {"postgresql": 5432, "snowflake": 443, "sqlserver": 0, "saphana": 30015}   # sqlserver 0 = no port (local / named instance)


def test_connection(cfg: dict) -> dict:
    """Connect once and read the version. cfg = connection row (+ settings). Raises with a readable message."""
    t = cfg["source_type"]
    windows_login = t == "sqlserver" and not (cfg.get("username") or "").strip()
    password = None if windows_login or t == "snowflake" else store.password_of(cfg)
    s = cfg.get("settings") or {}
    if t == "postgresql":
        from .postgres_connector import PostgresSource
        return PostgresSource(cfg).test()
    if t == "snowflake":
        from .snowflake_connector import SnowflakeSource
        return SnowflakeSource(cfg).test()
    if t == "sqlserver":
        from .sqlserver_connector import SqlServerSource
        return SqlServerSource(cfg).test()
    if t == "saphana":
        try:
            from hdbcli import dbapi
        except ImportError:
            raise ValueError("SAP HANA driver not installed: pip install hdbcli")
        kw = {"address": cfg["host"], "port": int(cfg["port"]), "user": cfg["username"], "password": password,
              "encrypt": str(s.get("encrypt", "yes")).lower().startswith("y")}
        if cfg.get("database"):
            kw["databaseName"] = cfg["database"]
        con = dbapi.connect(**kw)
        try:
            cur = con.cursor()
            cur.execute("SELECT VERSION, CURRENT_USER, DATABASE_NAME FROM M_DATABASE")
            row = cur.fetchone()
        finally:
            con.close()
        return {"version": f"SAP HANA {row[0]}", "login_user": row[1], "database": row[2]}
    raise ValueError(f"unknown source type {t}")
