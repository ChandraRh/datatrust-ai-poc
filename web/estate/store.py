"""
Portal store: who can log in, which sources are connected, access requests and the portal's own audit log.
Everything lives in schema `portal` of the POC database (config.py / .env PG_*). Created on first start.

Passwords of connected sources are NEVER stored here: a connection keeps the NAME of a .env variable
(e.g. CONN_SALES_PASSWORD) and the password is read from .env when the portal connects.
"""
import base64
import hashlib
import hmac
import json
import os
import secrets
import sys
import time
from pathlib import Path

import psycopg
from psycopg.rows import dict_row

ROOT = next(f for f in Path(__file__).resolve().parents if (f / "config.py").exists())
sys.path.insert(0, str(ROOT))
import config  # noqa: E402

SESSION_HOURS = 8


def auth_enabled() -> bool:
    return os.getenv("PORTAL_AUTH_ENABLED", "true").strip().lower() not in ("0", "false", "no", "off")


def db():
    """Connection to the portal's own database (the POC database)."""
    return psycopg.connect(sslmode=config.PG_SSLMODE, host=config.PG_HOST, port=config.PG_PORT, dbname=config.PG_DATABASE,
                           user=config.PG_USER, password=config.PG_PASSWORD, row_factory=dict_row, connect_timeout=5)


def init() -> None:
    """Create the portal tables, the first admin and the default connection (safe to run every start)."""
    with db() as conn:
        conn.execute("""
            CREATE SCHEMA IF NOT EXISTS portal;
            CREATE TABLE IF NOT EXISTS portal.settings (key TEXT PRIMARY KEY, value TEXT);
            CREATE TABLE IF NOT EXISTS portal.users (
                username      TEXT PRIMARY KEY,
                password_hash TEXT NOT NULL,
                role          TEXT NOT NULL CHECK (role IN ('admin', 'viewer')),
                active        BOOLEAN DEFAULT TRUE,
                created_by    TEXT,
                created_at    TIMESTAMP DEFAULT now());
            ALTER TABLE portal.users ADD COLUMN IF NOT EXISTS email TEXT;
            ALTER TABLE portal.users ADD COLUMN IF NOT EXISTS email_verified BOOLEAN NOT NULL DEFAULT FALSE;
            ALTER TABLE portal.users ADD COLUMN IF NOT EXISTS google_sub TEXT;
            ALTER TABLE portal.users ADD COLUMN IF NOT EXISTS session_version INTEGER NOT NULL DEFAULT 0;
            CREATE UNIQUE INDEX IF NOT EXISTS portal_users_email_unique ON portal.users (lower(email)) WHERE email IS NOT NULL;
            CREATE UNIQUE INDEX IF NOT EXISTS portal_users_google_sub_unique ON portal.users (google_sub) WHERE google_sub IS NOT NULL;
            CREATE TABLE IF NOT EXISTS portal.auth_tokens (
                token_hash  TEXT PRIMARY KEY,
                username    TEXT NOT NULL REFERENCES portal.users(username) ON DELETE CASCADE,
                purpose     TEXT NOT NULL CHECK (purpose IN ('verify_email', 'reset_password')),
                expires_at  TIMESTAMPTZ NOT NULL,
                consumed_at TIMESTAMPTZ,
                created_at  TIMESTAMPTZ NOT NULL DEFAULT now());
            CREATE TABLE IF NOT EXISTS portal.connections (
                connection_id SERIAL PRIMARY KEY,
                name          TEXT UNIQUE NOT NULL,
                source_type   TEXT NOT NULL DEFAULT 'postgresql',
                host          TEXT NOT NULL,
                port          INT  NOT NULL,
                database      TEXT NOT NULL,
                username      TEXT NOT NULL,
                password_env  TEXT NOT NULL,          -- name of the .env variable holding the password
                created_by    TEXT,
                created_at    TIMESTAMP DEFAULT now());
            ALTER TABLE portal.connections ADD COLUMN IF NOT EXISTS settings JSONB DEFAULT '{}'::jsonb;
            CREATE TABLE IF NOT EXISTS portal.access_requests (
                request_id    SERIAL PRIMARY KEY,
                connection_id INT REFERENCES portal.connections(connection_id),
                database      TEXT,
                request_type  TEXT,                   -- CREATE_USER / GRANT / REVOKE / GRANT_ROLE
                grantee       TEXT,
                details       JSONB,                  -- privileges, object type, object name ...
                sql_preview   TEXT,                   -- the statements that will run (passwords masked)
                reason        TEXT,
                status        TEXT DEFAULT 'PENDING', -- PENDING / APPLIED / REJECTED / FAILED
                requested_by  TEXT,
                requested_at  TIMESTAMP DEFAULT now(),
                decided_by    TEXT,
                decided_at    TIMESTAMP,
                error         TEXT);
            CREATE TABLE IF NOT EXISTS portal.people (       -- Connect: data owners / stewards people can contact
                person_id   SERIAL PRIMARY KEY,
                name        TEXT NOT NULL,
                email       TEXT UNIQUE NOT NULL,
                team        TEXT,
                title       TEXT,
                phone       TEXT,
                chat_link   TEXT,                       -- optional Google Chat link to the person's direct chat
                owns        TEXT[] DEFAULT '{}',        -- data they own: silver.payments, gold.*, payments ...
                active      BOOLEAN DEFAULT TRUE,
                created_by  TEXT,
                created_at  TIMESTAMP DEFAULT now(),
                updated_at  TIMESTAMP DEFAULT now());
            CREATE TABLE IF NOT EXISTS portal.audit_log (
                audit_id   BIGSERIAL PRIMARY KEY,
                at         TIMESTAMP DEFAULT now(),
                username   TEXT,
                action     TEXT,
                target     TEXT,
                detail     TEXT);
        """)
        if not conn.execute("SELECT 1 FROM portal.users LIMIT 1").fetchone():
            user = os.getenv("PORTAL_ADMIN_USER", "admin")
            password = os.getenv("PORTAL_ADMIN_PASSWORD") or secrets.token_urlsafe(9)
            email = (os.getenv("PORTAL_ADMIN_EMAIL") or "").strip().lower() or None
            conn.execute("""INSERT INTO portal.users
                            (username, password_hash, role, email, email_verified, created_by)
                            VALUES (%s, %s, 'admin', %s, %s, 'setup')""",
                         (user, hash_password(password), email, bool(email)))
            if not os.getenv("PORTAL_ADMIN_PASSWORD"):
                print("\n" + "=" * 70 + f"\n  Data Estate portal: first admin created\n  user: {user}   password: {password}\n"
                      "  (shown once; change it on the Access page, or set PORTAL_ADMIN_PASSWORD in .env)\n" + "=" * 70 + "\n")
        admin_email = (os.getenv("PORTAL_ADMIN_EMAIL") or "").strip().lower()
        if admin_email:
            admin_user = os.getenv("PORTAL_ADMIN_USER", "admin")
            conn.execute("""UPDATE portal.users SET email = %s, email_verified = TRUE
                            WHERE username = %s AND (email IS NULL OR lower(email) = %s)""",
                         (admin_email, admin_user, admin_email))
        if not conn.execute("SELECT 1 FROM portal.connections LIMIT 1").fetchone():
            conn.execute("""INSERT INTO portal.connections (name, host, port, database, username, password_env, created_by)
                            VALUES ('DataTrust POC (local)', %s, %s, %s, %s, 'PG_PASSWORD', 'setup')""",
                         (config.PG_HOST, config.PG_PORT, config.PG_DATABASE, config.PG_USER))
        conn.commit()


# ============================================================================ passwords and sessions
def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 200_000)
    return "pbkdf2$" + base64.b64encode(salt).decode() + "$" + base64.b64encode(digest).decode()


def check_password(password: str, stored: str) -> bool:
    try:
        _, salt, digest = stored.split("$")
        test = hashlib.pbkdf2_hmac("sha256", password.encode(), base64.b64decode(salt), 200_000)
        return hmac.compare_digest(test, base64.b64decode(digest))
    except Exception:
        return False


def _secret() -> bytes:
    """Signing key for session cookies: .env PORTAL_SECRET, else one generated once and kept in portal.settings."""
    if os.getenv("PORTAL_SECRET"):
        return os.getenv("PORTAL_SECRET").encode()
    with db() as conn:
        row = conn.execute("SELECT value FROM portal.settings WHERE key = 'session_secret'").fetchone()
        if row:
            return row["value"].encode()
        value = secrets.token_hex(32)
        conn.execute("INSERT INTO portal.settings (key, value) VALUES ('session_secret', %s) ON CONFLICT DO NOTHING", (value,))
        conn.commit()
        return conn.execute("SELECT value FROM portal.settings WHERE key = 'session_secret'").fetchone()["value"].encode()


def session_secret() -> str:
    return _secret().decode()


def make_session(username: str, role: str, session_version: int = 0) -> str:
    payload = base64.urlsafe_b64encode(json.dumps({"u": username, "r": role,
                                                   "v": session_version,
                                                   "exp": int(time.time()) + SESSION_HOURS * 3600}).encode()).decode()
    sig = hmac.new(_secret(), payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}.{sig}"


def read_session(token: str):
    """Return the public user identity only when the account remains active and verified."""
    try:
        payload, sig = token.split(".")
        if not hmac.compare_digest(sig, hmac.new(_secret(), payload.encode(), hashlib.sha256).hexdigest()):
            return None
        data = json.loads(base64.urlsafe_b64decode(payload))
        if data["exp"] < time.time():
            return None
        with db() as conn:
            row = conn.execute("""SELECT username, role, email, email_verified, session_version
                                  FROM portal.users WHERE username = %s AND active""",
                               (data["u"],)).fetchone()
        if not row or not row["email_verified"] or data.get("v", 0) != row["session_version"]:
            return None
        return {"username": row["username"], "role": row["role"]}
    except Exception:
        return None


def login(username: str, password: str):
    identifier = username.strip().lower()
    with db() as conn:
        row = conn.execute("""SELECT username, role, password_hash, email, email_verified, session_version
                              FROM portal.users
                              WHERE (lower(email) = %s OR lower(username) = %s) AND active
                              ORDER BY (lower(username) = %s) DESC
                              LIMIT 1""",
                           (identifier, identifier, identifier)).fetchone()
    if not row or not check_password(password, row["password_hash"]):
        audit(username, "LOGIN_FAILED", "portal", "wrong user or password")
        return None
    if not row["email_verified"]:
        audit(username, "LOGIN_FAILED", "portal", "email is not verified")
        return {"unverified": True}
    audit(username, "LOGIN", "portal", row["role"])
    return {"username": row["username"], "role": row["role"], "session_version": row["session_version"]}


# ============================================================================ portal users
def list_users():
    with db() as conn:
        return conn.execute("SELECT username, role, active, created_by, created_at FROM portal.users ORDER BY username").fetchall()


def save_user(username: str, role: str, password: str, by: str, active: bool = True):
    if role not in ("admin", "viewer"):
        raise ValueError("role must be admin or viewer")
    email = username.strip().lower()
    if "@" not in email or len(email) > 254 or len(password) < 12:
        raise ValueError("enter a valid email address and a password of at least 12 characters")
    with db() as conn:
        current = conn.execute("SELECT role, active FROM portal.users WHERE username = %s", (email,)).fetchone()
        if current and current["role"] == "admin" and (role != "admin" or not active):
            if email == by:
                raise ValueError("you cannot remove your own admin role; ask another admin")
            others = conn.execute("""SELECT COUNT(*) AS n FROM portal.users
                                     WHERE role = 'admin' AND active AND username <> %s""", (email,)).fetchone()["n"]
            if not others:
                raise ValueError("the portal needs at least one active admin")
        conn.execute("""INSERT INTO portal.users
                        (username, password_hash, role, active, created_by, email, email_verified)
                        VALUES (%s, %s, %s, %s, %s, %s, %s)
                        ON CONFLICT (username) DO UPDATE SET password_hash = EXCLUDED.password_hash,
                               role = EXCLUDED.role, active = EXCLUDED.active,
                               email = COALESCE(portal.users.email, EXCLUDED.email),
                               email_verified = portal.users.email_verified OR EXCLUDED.email_verified""",
                     (email, hash_password(password), role, active, by, email, True))
        conn.commit()
    audit(by, "PORTAL_USER_SAVED", email, role)


def register_email_user(email: str, password: str) -> tuple[str | None, bool]:
    """Create an unverified viewer account, or request verification for an existing unverified account."""
    email = email.strip().lower()
    configured_admins = {
        value.strip().lower()
        for value in os.getenv("PORTAL_ADMIN_EMAILS", "").split(",")
        if value.strip()
    }
    role = "admin" if email in configured_admins else "viewer"
    with db() as conn:
        created = conn.execute("""INSERT INTO portal.users
                                  (username, email, password_hash, role, active, created_by)
                                  VALUES (%s, %s, %s, %s, TRUE, 'self-signup')
                                  ON CONFLICT DO NOTHING RETURNING username""",
                               (email, email, hash_password(password), role)).fetchone()
        if created:
            return email, True
        email_row = conn.execute("""SELECT username, email, email_verified, active FROM portal.users
                                    WHERE lower(email) = %s""", (email,)).fetchone()
        username_row = conn.execute("""SELECT username, email, email_verified, active FROM portal.users
                                       WHERE lower(username) = %s""", (email,)).fetchone()
        if email_row and username_row and email_row["username"] != username_row["username"]:
            return None, False
        row = email_row or username_row
        if row and row["email"] and row["email"].lower() == email and not row["email_verified"] and row["active"]:
            return row["username"], True
        return None, False


def register_google_user(email: str, google_sub: str) -> dict | None:
    email = email.strip().lower()
    configured_admins = {
        value.strip().lower()
        for value in os.getenv("PORTAL_ADMIN_EMAILS", "").split(",")
        if value.strip()
    }
    configured_admin = (os.getenv("PORTAL_ADMIN_EMAIL") or "").strip().lower()
    role = "admin" if email in configured_admins or email == configured_admin else "viewer"
    with db() as conn:
        email_row = conn.execute("""SELECT username, role, email, google_sub, session_version
                                    FROM portal.users WHERE lower(email) = %s""", (email,)).fetchone()
        subject_row = conn.execute("""SELECT username, role, email, google_sub, session_version
                                      FROM portal.users WHERE google_sub = %s""", (google_sub,)).fetchone()
        if email_row and subject_row and email_row["username"] != subject_row["username"]:
            return None
        row = email_row or subject_row
        if row:
            if ((row["email"] and row["email"].lower() != email)
                    or (row["google_sub"] and row["google_sub"] != google_sub)):
                return None
            conn.execute("""UPDATE portal.users SET email = %s, email_verified = TRUE,
                            google_sub = %s, role = CASE WHEN %s = 'admin' THEN 'admin' ELSE role END
                            WHERE username = %s""", (email, google_sub, role, row["username"]))
            username = row["username"]
        else:
            username = email
            conn.execute("""INSERT INTO portal.users
                            (username, email, email_verified, google_sub, password_hash, role, active, created_by)
                            VALUES (%s, %s, TRUE, %s, %s, %s, TRUE, 'google-sso')""",
                         (username, email, google_sub, hash_password(secrets.token_urlsafe(32)), role))
        result = conn.execute("""SELECT username, role, session_version FROM portal.users
                                 WHERE username = %s AND active AND email_verified""", (username,)).fetchone()
        conn.commit()
    if not result:
        return None
    audit(username, "GOOGLE_LOGIN", "portal", "Google OpenID Connect")
    return result


def issue_auth_token(username: str, purpose: str, minutes: int = 30) -> str:
    if purpose not in ("verify_email", "reset_password"):
        raise ValueError("unsupported authentication token purpose")
    token = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    with db() as conn:
        conn.execute("""UPDATE portal.auth_tokens SET consumed_at = now()
                        WHERE username = %s AND purpose = %s AND consumed_at IS NULL""", (username, purpose))
        conn.execute("""INSERT INTO portal.auth_tokens (token_hash, username, purpose, expires_at)
                        VALUES (%s, %s, %s, now() + (%s * interval '1 minute'))""",
                     (token_hash, username, purpose, minutes))
        conn.commit()
    return token


def consume_auth_token(token: str, purpose: str) -> str | None:
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    with db() as conn:
        row = conn.execute("""UPDATE portal.auth_tokens SET consumed_at = now()
                              WHERE token_hash = %s AND purpose = %s AND consumed_at IS NULL
                                AND expires_at > now()
                              RETURNING username""", (token_hash, purpose)).fetchone()
        if not row:
            return None
        if purpose == "verify_email":
            conn.execute("UPDATE portal.users SET email_verified = TRUE WHERE username = %s", (row["username"],))
        conn.commit()
    return row["username"]


def reset_user_password(token: str, password: str) -> str | None:
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    with db() as conn:
        row = conn.execute("""UPDATE portal.auth_tokens SET consumed_at = now()
                              WHERE token_hash = %s AND purpose = 'reset_password' AND consumed_at IS NULL
                                AND expires_at > now()
                              RETURNING username""", (token_hash,)).fetchone()
        if not row:
            return None
        updated = conn.execute("""UPDATE portal.users SET password_hash = %s, session_version = session_version + 1
                                  WHERE username = %s AND active AND email_verified
                                  RETURNING username""",
                               (hash_password(password), row["username"])).fetchone()
        if not updated:
            conn.commit()
            return None
        conn.execute("""UPDATE portal.auth_tokens SET consumed_at = now()
                        WHERE username = %s AND consumed_at IS NULL""", (row["username"],))
        conn.commit()
    audit(row["username"], "PASSWORD_RESET", "portal", "password changed using a single-use email token")
    return row["username"]


# ============================================================================ connections
def list_connections():
    with db() as conn:
        return conn.execute("""SELECT connection_id, name, source_type, host, port, database, username, password_env,
                                      settings, created_by, created_at FROM portal.connections ORDER BY connection_id""").fetchall()


def get_connection(connection_id: int) -> dict:
    with db() as conn:
        row = conn.execute("SELECT * FROM portal.connections WHERE connection_id = %s", (connection_id,)).fetchone()
    if not row:
        raise ValueError(f"connection {connection_id} not found")
    return row


def add_connection(name, host, port, database, username, password_env, by, source_type="postgresql", settings=None):
    from .source_types import DEFAULT_PORT, SOURCE_TYPES
    if source_type not in SOURCE_TYPES:
        raise ValueError(f"unknown source type {source_type}")
    if not name.strip() or not host.strip() or (not (username or "").strip() and source_type != "sqlserver"):
        raise ValueError("name, host/account and login user are required")      # SQL Server: empty user = Windows login
    if not password_env or not password_env.replace("_", "").isalnum():
        raise ValueError("password_env must be the NAME of a variable in .env, e.g. CONN_SALES_PASSWORD")
    with db() as conn:
        row = conn.execute("""INSERT INTO portal.connections (name, source_type, host, port, database, username, password_env,
                                     settings, created_by)
                              VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING connection_id""",
                           (name.strip(), source_type, host.strip(), int(port or DEFAULT_PORT[source_type]), (database or "").strip(),
                            (username or "").strip(), password_env, json.dumps(settings or {}), by)).fetchone()
        conn.commit()
    audit(by, "CONNECTION_ADDED", name, f"{source_type} {username}@{host}:{port}/{database}")
    return row["connection_id"]


def delete_connection(connection_id: int, by: str):
    with db() as conn:
        conn.execute("DELETE FROM portal.access_requests WHERE connection_id = %s AND status = 'PENDING'", (connection_id,))
        conn.execute("UPDATE portal.access_requests SET connection_id = NULL WHERE connection_id = %s", (connection_id,))
        conn.execute("DELETE FROM portal.connections WHERE connection_id = %s", (connection_id,))
        conn.commit()
    audit(by, "CONNECTION_REMOVED", str(connection_id), "")


def resolve_password(password_env: str) -> str:
    """Read the password from .env every time (a newly added variable works without a restart)."""
    from dotenv import dotenv_values
    value = dotenv_values(ROOT / ".env").get(password_env) or os.getenv(password_env)
    if value is None and password_env == "PG_PASSWORD":
        value = config.PG_PASSWORD                     # the POC's own database (default from config.py)
    if value is None:
        raise ValueError(f"{password_env} is not set (add it to .env, or as an environment variable / secret in the cloud)")
    return value


# ============================================================================ audit
def audit(username, action, target, detail=""):
    try:
        with db() as conn:
            conn.execute("INSERT INTO portal.audit_log (username, action, target, detail) VALUES (%s, %s, %s, %s)",
                         (username, action, target, str(detail)[:2000]))
            conn.commit()
    except Exception as e:
        print(f"!! portal audit failed: {e}")


def audit_log(limit: int = 200):
    with db() as conn:
        return conn.execute("SELECT at, username, action, target, detail FROM portal.audit_log ORDER BY audit_id DESC LIMIT %s",
                            (limit,)).fetchall()


# ============================================================================ Connect: people directory
EMAIL_RE = r"^[A-Za-z0-9._%+'-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$"
ASSET_RE = r"^[A-Za-z0-9_][A-Za-z0-9_.\-]*(\.\*)?$"


def list_people():
    with db() as conn:
        return conn.execute("""SELECT person_id, name, email, team, title, phone, chat_link, owns, created_by, updated_at
                               FROM portal.people WHERE active ORDER BY lower(name)""").fetchall()


def save_person(data: dict, by: str) -> int:
    """Add or update a person (matched by e-mail). Values are checked so nothing unsafe reaches the page."""
    import re
    name = (data.get("name") or "").strip()
    email = (data.get("email") or "").strip().lower()
    if not name or len(name) > 120:
        raise ValueError("name is required (max 120 characters)")
    if not re.match(EMAIL_RE, email):
        raise ValueError("a valid company e-mail address is required")
    domains = [d.strip().lower().lstrip("@") for d in os.getenv("PORTAL_COMPANY_DOMAIN", "").split(",") if d.strip()]
    if domains and email.split("@")[1] not in domains:
        raise ValueError(f"only company e-mail addresses are allowed ({', '.join('@' + d for d in domains)})")
    phone = (data.get("phone") or "").strip()
    if phone and not re.match(r"^\+?[0-9 ()\-]{6,20}$", phone):
        raise ValueError("phone: digits, spaces, + ( ) - only")
    chat = (data.get("chat_link") or "").strip()
    if chat and not re.match(r"^https://(chat\.google\.com|mail\.google\.com/chat)/", chat):
        raise ValueError("Google Chat link must start with https://chat.google.com/ or https://mail.google.com/chat/")
    owns = data.get("owns") or []
    if isinstance(owns, str):
        owns = owns.split(",")
    owns = sorted({o.strip() for o in owns if o and o.strip()})[:50]
    bad = [o for o in owns if not re.match(ASSET_RE, o)]
    if bad:
        raise ValueError(f"data owned: use names like silver.payments, gold.* or payments (not {bad[0]})")
    team, title = (data.get("team") or "").strip()[:80], (data.get("title") or "").strip()[:80]
    with db() as conn:
        row = conn.execute("""INSERT INTO portal.people (name, email, team, title, phone, chat_link, owns, created_by)
                              VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                              ON CONFLICT (email) DO UPDATE SET name = EXCLUDED.name, team = EXCLUDED.team, title = EXCLUDED.title,
                                     phone = EXCLUDED.phone, chat_link = EXCLUDED.chat_link, owns = EXCLUDED.owns,
                                     active = TRUE, updated_at = now()
                              RETURNING person_id""", (name, email, team, title, phone, chat, owns, by)).fetchone()
        conn.commit()
    audit(by, "PERSON_SAVED", email, f"{name}; owns {', '.join(owns) or '-'}")
    return row["person_id"]


def remove_person(person_id: int, by: str):
    with db() as conn:
        row = conn.execute("UPDATE portal.people SET active = FALSE, updated_at = now() WHERE person_id = %s RETURNING email",
                           (person_id,)).fetchone()
        conn.commit()
    if not row:
        raise ValueError("person not found")
    audit(by, "PERSON_REMOVED", row["email"], "")


def match_people(text: str):
    """People who own something mentioned in the text (an incident, a table, a lineage box).
    'silver.payments' matches the full name or just 'payments'; 'gold.*' matches anything in schema gold."""
    import re
    low = (text or "").lower()
    norm = lambda w: w[:-1] if w.endswith("s") and len(w) > 3 else w            # payment / payments  # noqa: E731
    words = {norm(w) for w in re.findall(r"[a-z0-9_]+", low)}
    hits = []
    for p in list_people():
        matched = []
        for a in p["owns"] or []:
            a_low = a.lower()
            if a_low.endswith(".*"):
                schema = a_low[:-2]
                ok = f"{schema}." in low or norm(schema) in words
            elif "." in a_low:
                ok = a_low in low or norm(a_low.split(".")[-1]) in words
            else:
                ok = norm(a_low) in words
            if ok:
                matched.append(a)
        if matched:
            hits.append({**p, "matched": matched})
    return sorted(hits, key=lambda p: -len(p["matched"]))
