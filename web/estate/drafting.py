"""
Turn a plain-words access request into a portal access request (no SQL typing needed).

    "give test_nalyst read access to the gold schema"   -> GRANT SELECT ON SCHEMA::[gold] TO [test_nalyst]
    "create role reporting_readers and add test_nalyst" -> CREATE ROLE ... + ALTER ROLE ... ADD MEMBER ...

Safety model:
  * this module only DRAFTS a request (type, user, details). It never runs anything.
  * the SQL shown to the user is always built by the connector's build_statements(), which checks every name
    against the live catalog. An AI model (if configured) may only fill in the same request fields.
  * the request is stored as PENDING and runs only after an admin clicks Approve.
Step 1 is a built-in parser (free, offline). Step 2, only if it cannot understand the text and LLM_PROVIDER is
set (ollama / anthropic), asks the model for the request fields as JSON.
"""
import difflib
import json
import os
import re

TOKEN = r"[\w.\-\\]+"
EXAMPLES = [
    "Give test_nalyst read access to the gold schema",
    "Give test_nalyst read access to the whole database",
    "Give test_nalyst read access to gold.daily_revenue",
    "Create user audit_bot without login",
    "Create user priya with a login and add her to db_datareader",
    "Create role reporting_readers and add test_nalyst",
    "Add test_nalyst to role reporting_readers",
    "Remove test_nalyst from role reporting_readers",
    "Revoke read access on the gold schema from test_nalyst",
    "Remove user test_nalyst",
]
WORD_PRIVS = [                                   # plain words -> privileges (checked against the source type later)
    (r"\bfull (access|control|rights?)\b|\ball (privileges|rights|permissions)\b", ["FULL"]),
    (r"\bread[\s-]?(and|&|/)[\s-]?write\b|\bread[\s-]?write\b", ["SELECT", "INSERT", "UPDATE", "DELETE"]),
    (r"\bwrite\b", ["INSERT", "UPDATE", "DELETE"]),
    (r"\b(read|select|view data|see|query|read[\s-]?only|look at|report)\w*", ["SELECT"]),
    (r"\binsert\w*|\badd rows\b|\bload\b", ["INSERT"]),
    (r"\bupdate\w*|\bmodify\b|\bedit\b", ["UPDATE"]),
    (r"\bdelete\w*", ["DELETE"]),
    (r"\b(execute|run|call)\b", ["EXECUTE"]),
    (r"\bview definition\b|\bsee the (code|definition|structure)\b", ["VIEW DEFINITION"]),
]


class Draft(Exception):
    """Raised when the text cannot be turned into a request; carries a readable message."""


def _plan(src, stype, database, text, privileges):
    """Built-in parser -> (request_type, grantee, details) or None if the text is not understood."""
    t = " ".join(text.strip().split())
    low = t.lower()
    principals = src.roles(database)
    names = {p["name"].lower(): p["name"] for p in principals}
    groups = {p["name"].lower(): p["name"] for p in principals if not p["can_login"]}
    schemas = {s["name"].lower(): s["name"] for s in src.schemas(database)}

    def real(name, pool=names):
        return pool.get(name.lower().strip(".,;:!?'\""))

    def mentioned(pool, skip=()):
        for tok in re.findall(TOKEN, t):
            hit = real(tok, pool)
            if hit and hit not in skip and "." not in tok.strip("."):
                return hit
        return None

    # ---- create a role (optionally with members)
    m = re.search(rf"\b(?:create|make|set up|add|new)\b[^.]*?\b(?:group\s+)?role\s+(?:called\s+|named\s+)?({TOKEN})", t, re.I)
    if m and not re.search(r"\b(?:to|into|from|of)\s+(?:the\s+)?(?:group\s+)?role\b", low[:m.start(1)]):
        role = m.group(1).strip(".,")
        members = [real(x) for x in re.findall(TOKEN, t[m.end():]) if real(x) and real(x) != role]
        return "CREATE_ROLE", role, {"members": members}

    # ---- remove someone from a role
    m = re.search(rf"\b(?:remove|take|drop|kick)\s+(?:user\s+)?({TOKEN})\s+(?:out\s+of|from)\s+(?:the\s+)?(?:group\s+)?(?:role\s+)?({TOKEN})", t, re.I)
    if m and real(m.group(2), groups):
        return "REVOKE_ROLE", real(m.group(1)) or m.group(1), {"role": real(m.group(2), groups)}

    # ---- add someone to a role
    m = (re.search(rf"\b(?:add|put|include|assign)\s+(?:user\s+)?({TOKEN})\s+(?:to|into|in)\s+(?:the\s+)?(?:group\s+)?(?:role\s+)?({TOKEN})", t, re.I)
         or re.search(rf"\bmake\s+({TOKEN})\s+(?:a\s+)?member\s+of\s+(?:the\s+)?(?:role\s+)?({TOKEN})", t, re.I))
    if m and real(m.group(1)) and real(m.group(2), groups):
        return "GRANT_ROLE", real(m.group(1)), {"role": real(m.group(2), groups)}

    # ---- remove a user or role completely
    m = re.search(rf"\b(?:drop|delete|remove|offboard)\s+(?:the\s+)?(?:database\s+)?(?:user|role|login|account|member)\s+({TOKEN})"
                  rf"(?:\s+(?:from|in)\s+(?:the\s+)?(?:database|db|portal)\b.*)?\s*$", t, re.I)
    if m:
        return "DROP_PRINCIPAL", real(m.group(1)) or m.group(1).strip(".,"), {}

    # ---- create a user / team member
    m = re.search(rf"\b(?:create|add|onboard|new|set up)\b[^.]*?\b(?:user|login|team member|member|account|person)\s+(?:called\s+|named\s+|for\s+)?({TOKEN})", t, re.I)
    if m and not real(m.group(1)):
        user = m.group(1).strip(".,")
        if re.search(r"\b(without|no)\s+(a\s+)?login\b|\bdatabase user only\b|\bno password\b", low):
            login_type = "none"
        elif "\\" in user or re.search(r"\bwindows\b", low):
            login_type = "windows"
        else:
            login_type = "sql"
        member_of = []
        rm = re.search(rf"\b(?:to|in|into)\s+(?:the\s+)?(?:role\s+)?({TOKEN})", t[m.end():], re.I)
        if rm and real(rm.group(1), groups):
            member_of.append(real(rm.group(1), groups))
        elif stype == "sqlserver" and re.search(r"\bread[\s-]?only\b|\bread access\b", low) and "db_datareader" in groups:
            member_of.append("db_datareader")
        details = {"member_of": member_of}
        if stype == "sqlserver" or login_type == "none":
            details["login_type"] = login_type
        return "CREATE_USER", user, details

    # ---- grant / revoke
    revoke = bool(re.search(r"\b(revoke|withdraw|take away|remove|cancel|stop|no longer)\b", low))
    if not revoke and not re.search(r"\b(grant|give|allow|provide|let|enable|access|permission|rights?)\b", low):
        return None
    if re.search(r"\b(everyone|everybody|all users|public)\b", low):
        grantee = "PUBLIC"
    else:
        grantee = mentioned(names)
        if not grantee:
            words = [w for w in re.findall(r"[\w.\-\\]+", t) if len(w) > 3]
            close = next((c for w in words for c in difflib.get_close_matches(w.lower(), list(names), n=1, cutoff=0.8)), None)
            raise Draft((f"Did you mean {names[close]}? " if close else "") +
                        "I could not find which user or role this is for. Use the exact name from the "
                        "'Users and roles' list (create the user first if it does not exist).")
    # privilege words are read from the text WITHOUT user, role and schema names (so 'reporting_team' is not 'report')
    plain_words = " ".join(w for w in re.findall(r"[\w.\-\\]+", low)
                           if w not in names and w not in schemas and not re.match(r"\w+\.\w+", w))
    privs = []
    for pattern, p in WORD_PRIVS:
        if re.search(pattern, plain_words):
            privs += [x for x in p if x not in privs]
            if p == ["FULL"]:
                break
    privs = privs or ["SELECT"]

    otype = schema = obj = None
    dotted = [x for x in re.findall(r"\b([A-Za-z_]\w*)\.([A-Za-z_]\w*)\b", t) if x[0].lower() in schemas]
    if dotted:
        schema, obj_name = schemas[dotted[0][0].lower()], dotted[0][1]
        found = [o for o in src.objects(database, schema) if o["name"].split("(")[0].lower() == obj_name.lower()]
        if not found:
            raise Draft(f"{schema}.{obj_name} was not found in {database}.")
        obj, kind = found[0]["name"], found[0]["type"]
        otype = {"MATERIALIZED VIEW": "VIEW"}.get(kind, kind)
    elif re.search(r"\b(whole|entire|full|complete|all of the)\s+(database|db)\b|\bevery(thing| table| schema)\b|\ball (tables|schemas|data)\b"
                   r"|\b(the|this)\s+(database|db)\b", low):
        if stype == "postgresql":
            raise Draft("PostgreSQL gives table access per schema. Say which schema, e.g. "
                        "'give test_nalyst read access to schema gold' (repeat for other schemas).")
        otype = "DATABASE"
    else:
        sm = re.search(r"\bschema\s+([A-Za-z_]\w*)|\b([A-Za-z_]\w*)\s+schema\b", t, re.I)
        cand = (sm.group(1) or sm.group(2)) if sm else None
        if not cand or cand.lower() not in schemas:
            cand = next((tok for tok in re.findall(r"[A-Za-z_]\w*", t) if tok.lower() in schemas), None)
        if not cand:
            raise Draft(f"Say what the access is on: a schema (e.g. 'schema gold'), an object (e.g. gold.daily_revenue)"
                        + (" or 'the whole database'." if stype == "sqlserver" else "."))
        schema, otype = schemas[cand.lower()], "SCHEMA"

    if stype == "postgresql" and otype == "SCHEMA" and any(p in ("SELECT", "INSERT", "UPDATE", "DELETE") for p in privs):
        otype = "ALL_TABLES_IN_SCHEMA"                       # PostgreSQL: table rights for every table in the schema
    if "FULL" in privs:
        privs = ["ALL"] if "ALL" in privileges[otype] else ["CONTROL"]
    allowed = privileges[otype]
    privs = [p for p in privs if p in allowed]
    if not privs:
        raise Draft(f"Those rights do not apply to a {otype.lower().replace('_', ' ')}. Possible: {', '.join(allowed)}.")
    details = {"object_type": otype, "schema": schema, "object": obj, "privileges": privs}
    return ("REVOKE" if revoke else "GRANT"), grantee, details


def _ask_model(src, stype, database, text, privileges):
    """Ask the configured AI model for the request fields (JSON). Returns None when no model is configured."""
    provider = os.getenv("LLM_PROVIDER", "none").lower()
    if provider not in ("ollama", "anthropic"):
        return None
    import requests
    principals = src.roles(database)
    catalog = {
        "source_type": stype, "database": database,
        "users": [p["name"] for p in principals if p["can_login"]][:200],
        "roles": [p["name"] for p in principals if not p["can_login"]][:200],
        "schemas": [s["name"] for s in src.schemas(database)][:100],
        "privileges_per_object_type": privileges,
    }
    system = (
        "You convert one database access request written in plain English into JSON. Reply with JSON only, no text.\n"
        'Shape: {"request_type": one of GRANT, REVOKE, CREATE_USER, CREATE_ROLE, GRANT_ROLE, REVOKE_ROLE, DROP_PRINCIPAL, '
        '"grantee": user or role name (the new name for CREATE_USER / CREATE_ROLE), '
        '"details": {...}} where details is: for GRANT/REVOKE {"object_type", "schema", "object", "privileges": [...]}; '
        'for CREATE_USER {"login_type": "sql"|"windows"|"none", "member_of": [roles]}; for CREATE_ROLE {"members": [users]}; '
        'for GRANT_ROLE / REVOKE_ROLE {"role"}; for DROP_PRINCIPAL {}.\n'
        "Use only names that exist in this catalog (except the new name when creating) and only the privileges listed for "
        f"the object type. If the request is unclear, reply {{\"error\": \"what is missing\"}}.\nCatalog: {json.dumps(catalog)}")
    if provider == "ollama":
        r = requests.post(os.getenv("OLLAMA_URL", "http://localhost:11434") + "/api/chat", timeout=120, json={
            "model": os.getenv("OLLAMA_MODEL", "qwen2.5:7b"), "stream": False, "format": "json", "options": {"temperature": 0},
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": text}]})
        r.raise_for_status()
        content = r.json()["message"]["content"]
    else:
        r = requests.post("https://api.anthropic.com/v1/messages", timeout=60, headers={
            "x-api-key": os.environ["ANTHROPIC_API_KEY"], "anthropic-version": "2023-06-01", "content-type": "application/json"},
            json={"model": os.getenv("ANTHROPIC_MODEL", "claude-sonnet-5-5"), "max_tokens": 600, "system": system,
                  "messages": [{"role": "user", "content": text}]})
        r.raise_for_status()
        content = "".join(b.get("text", "") for b in r.json()["content"])
    m = re.search(r"\{.*\}", content, re.S)
    data = json.loads(m.group(0)) if m else {}
    if data.get("error") or not data.get("request_type"):
        raise Draft(data.get("error") or "The AI model could not turn this into an access request.")
    return data["request_type"].upper(), str(data.get("grantee") or ""), data.get("details") or {}


def describe(stype, database, request_type, grantee, d) -> str:
    """The request in plain words (built from the request itself, not from the model)."""
    who = "everyone (PUBLIC)" if grantee == "PUBLIC" else grantee
    if request_type in ("GRANT", "REVOKE"):
        ot = d.get("object_type")
        target = {"DATABASE": f"the whole database {database}" + (" (every schema, table and view)" if stype == "sqlserver" else ""),
                  "SCHEMA": f"schema {d.get('schema')}" + (" (every table and view in it)" if stype == "sqlserver" else ""),
                  "ALL_TABLES_IN_SCHEMA": f"every existing table and view in schema {d.get('schema')}"}.get(ot, f"{(ot or '').lower()} {d.get('schema')}.{d.get('object')}")
        rights = ", ".join(d.get("privileges") or [])
        return f"Give {who} {rights} on {target}." if request_type == "GRANT" else f"Take {rights} on {target} away from {who}."
    if request_type == "CREATE_USER":
        kind = {"none": "a database user without a login (cannot sign in; useful for testing or as an owner)",
                "windows": "a login for the Windows account and a database user",
                "sql": "a login with a generated password (shown once to the approver) and a database user"}[d.get("login_type") or "sql"]
        return f"Create {grantee}: {kind} in {database}" + (f", member of {', '.join(d['member_of'])}" if d.get("member_of") else "") + "."
    if request_type == "CREATE_ROLE":
        return f"Create role {grantee} in {database}" + (f" with members {', '.join(d['members'])}" if d.get("members") else "") + \
               ". Then grant rights to the role once, and add people to it."
    if request_type == "GRANT_ROLE":
        return f"Add {grantee} to role {d.get('role')}."
    if request_type == "REVOKE_ROLE":
        return f"Remove {grantee} from role {d.get('role')}."
    if request_type == "DROP_PRINCIPAL":
        return f"Remove {grantee} from {database}" + (" (the server login is kept)." if stype == "sqlserver" else ".")
    return request_type


def draft(src, stype, database, text, privileges) -> dict:
    """-> {ok, request, sql_preview, summary, mode} or {ok: False, message, examples}."""
    if not text or not text.strip():
        return {"ok": False, "message": "Describe the access you need.", "examples": EXAMPLES}
    mode = "built-in"
    try:
        plan = _plan(src, stype, database, text, privileges)
        if plan is None:
            plan = _ask_model(src, stype, database, text, privileges)
            mode = os.getenv("LLM_PROVIDER", "none").lower()
        if plan is None:
            return {"ok": False, "message": "I could not understand that request. Try one of these:", "examples": EXAMPLES}
        request_type, grantee, details = plan
        _, preview = src.build_statements(request_type, database, details, grantee)     # catalog checks happen here
    except Draft as e:
        return {"ok": False, "message": str(e), "examples": EXAMPLES}
    except ValueError as e:
        return {"ok": False, "message": f"That request cannot be prepared: {e}", "examples": EXAMPLES}
    except Exception as e:                                                             # model unreachable etc.
        return {"ok": False, "message": f"Could not prepare the request ({type(e).__name__}: {e}).", "examples": EXAMPLES}
    return {"ok": True, "mode": mode, "summary": describe(stype, database, request_type, grantee, details), "sql_preview": preview,
            "request": {"request_type": request_type, "grantee": grantee, "details": details}}
