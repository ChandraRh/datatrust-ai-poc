"""
Why did it fail? ADF error -> cause, the system at fault, what to do, and whether a retry can help.

ADF errors are layered: a generic code (2200 = Copy failed, 2011, ...) wraps the connector's own code
(ErrorCode=UserErrorFileNotFound, PathNotFound, ...) which may wrap the database's error (PostgreSQL SQLSTATE 23505
...). The rules below look at all layers, most specific first. Every rule is plain data, so a new cause is one entry.

    diagnose_task(task)                       one failed activity run
    failure_chain(run, tasks, child_lookup)   the activity runs that really caused a pipeline run to fail, following
                                              Execute Pipeline into the child run, skipping containers that only
                                              failed because something inside them failed
"""
from __future__ import annotations

import re

SOURCE, TARGET, DATA, ADF, NETWORK, CREDS, AZURE = ("Source storage", "Target PostgreSQL", "Data", "Data Factory",
                                                     "Network", "Credentials / permissions", "Azure service")

# id, patterns (regex, case-insensitive, matched against code + message + inner codes), system, title,
# explanation (may use {path} {table} {column} {constraint} {account} {container} {row} {file}), fixes, retry_helps
RULES = [
    ("cancelled", [r"^__cancelled__$"], ADF, "Run was cancelled",
     "Someone or something cancelled this run before it finished (a user in ADF Studio, a timeout of the parent, or the "
     "trigger being stopped).", ["Check who cancelled it in ADF Monitor (the run's 'Cancelled by').",
                                 "Re-run the pipeline if the data is still needed."], True),
    ("fail_activity", [r"^__fail_activity__$"], ADF, "The pipeline raised its own failure",
     "A Fail activity in the pipeline stopped the run on purpose with a custom message - usually a validation the "
     "pipeline does itself (no file, row count check, ...).", ["Read the custom message below: it says what the pipeline "
                                                              "checked.", "Fix the condition, then re-run."], False),
    ("source_path_missing", [r"PathNotFound", r"BlobNotFound", r"UserErrorFileNotFound", r"UserErrorSourceBlobNotExist",
                             r"FileNotFound", r"specified (?:path|blob|file) does not exist", r"No (?:file|files) (?:was |were )?found",
                             r"UserErrorNoFileFound", r"ContainerNotFound", r"FilesystemNotFound"],
     SOURCE, "Source file or folder is missing",
     "Data Factory looked for {path} in storage and it was not there. Either the file was not delivered yet, it has a "
     "different name/date than the pipeline expects, or it was moved or deleted.",
     ["Check that the file exists: storage account {account}, container {container}, path {path}.",
      "If the upstream system is late, re-run once the file arrives (or add a Get Metadata / Validation check before the copy).",
      "If the name is built from a date or parameter, check the run parameters on this page - the path above is what was resolved."],
     True),
    ("storage_permission", [r"AuthorizationPermissionMismatch", r"This request is not authorized to perform this operation using this permission"],
     CREDS, "Data Factory has no permission on the storage account",
     "The identity Data Factory uses (its managed identity or the linked service's service principal) can reach the "
     "storage account but is not allowed to read/write this data.",
     ["Give the factory's managed identity the 'Storage Blob Data Reader' role (source) or 'Contributor' role "
      "(if it writes) on the storage account {account} or container {container}.",
      "Role changes take up to 10 minutes to apply; then re-run."], False),
    ("storage_firewall", [r"AuthorizationFailure", r"This request is not authorized to perform this operation\.",
                          r"(?:public network access|firewall).*(?:storage|blob)"],
     NETWORK, "Storage firewall blocked Data Factory",
     "The storage account's network rules (firewall / private endpoint) refused the request, so Data Factory could not "
     "reach the files.", ["On the storage account: Networking -> allow 'trusted Microsoft services' or add a managed "
                          "private endpoint from Data Factory.", "If you use a self-hosted integration runtime, allow its IP."], False),
    ("bad_file_format", [r"DelimitedTextMoreColumnsThanDefined", r"SourceDataContainsMoreColumnsThanDefined",
                         r"DelimitedTextColumnNameNotAllowNull", r"DelimitedTextIncorrectRowDelimiter", r"UserErrorInvalidRowDelimiter",
                         r"column count", r"ParquetInvalidFile", r"JsonInvalidDataFormat", r"UnsupportedCompression",
                         r"InvalidFileFormat", r"Malformed"],
     DATA, "Source file is malformed",
     "Rows in the file do not match the format the dataset expects (extra/missing columns, wrong delimiter or quote, "
     "missing header, or a broken compressed file){row_hint}.",
     ["Open the file and check the row mentioned in the error: delimiter, quotes, header, number of columns.",
      "Fix it at the source and re-deliver, or set fault tolerance on the Copy activity to skip and log bad rows."], False),
    ("type_conversion", [r"TypeConversionFailure", r"UserErrorInvalidDataValue", r"\b22P02\b", r"\b22007\b", r"\b22008\b",
                         r"\b22003\b", r"converting value", r"Input string was not in a correct format", r"invalid input syntax",
                         r"date/time field value out of range", r"numeric field overflow", r"out of range for type"],
     DATA, "A value does not fit the target column type",
     "At least one value{column_hint} could not be converted to the column's data type in PostgreSQL (e.g. text in a "
     "number column, a badly formatted date, a number too large){row_hint}.",
     ["Find the bad value (the error names it) and correct it at the source.",
      "Or adjust the column mapping / target column type, or enable fault tolerance to skip and log incompatible rows."], False),
    ("value_too_long", [r"\b22001\b", r"value too long"], DATA, "A value is too long for the target column",
     "A text value is longer than the target column allows{column_hint}.",
     ["Widen the column in PostgreSQL (e.g. ALTER COLUMN ... TYPE varchar(n) / text) or trim the value in the mapping."], False),
    ("column_missing", [r"ColumnNotFound", r"\b42703\b", r"column \"?[\w.]+\"? (?:of relation \"?[\w.]+\"? )?does not exist",
                        r"InvalidColumnMapping", r"SchemaMappingCannotInferSinkColumnType", r"Column '[^']+' (?:is not found|does not exist)",
                        r"column .* not found in source"],
     DATA, "Schema changed: a column is missing",
     "The column mapping expects a column{column_hint} that the source file or target table no longer has. Someone "
     "renamed, removed or added a column on one side.",
     ["Compare the file's header with the target table's columns.",
      "Update the Copy activity mapping (Mapping tab -> Import schemas) or put the column back."], False),
    ("table_missing", [r"\b42P01\b", r"relation \"?[\w.\"]+\"? does not exist", r"(?:table|object) .* (?:not found|does not exist)",
                       r"SinkTableNotExist", r"Invalid object name"],
     TARGET, "Target table does not exist",
     "The PostgreSQL table {table} could not be found. It was dropped or renamed, the schema name is different, or the "
     "pipeline points at another database.",
     ["Check the table exists in the target database (this page checks it too, see Target checks).",
      "Re-create it, fix the dataset's schema/table name, or set the Copy sink 'Table option' to auto-create."], False),
    ("duplicate_key", [r"\b23505\b", r"duplicate key value violates unique constraint", r"Violation of PRIMARY KEY"],
     TARGET, "Duplicate key in the target table",
     "Rows being loaded have a key that is already in the target table{constraint_hint}. This usually means the same "
     "file was loaded twice, the pipeline was re-run without clearing the previous load, or the source sent duplicates.",
     ["Check whether this data was already loaded (a re-run?).",
      "Use the PostgreSQL sink's Upsert write behaviour with the key columns, or a pre-copy script that deletes the batch first.",
      "If the source itself has duplicates, fix them there."], False),
    ("not_null", [r"\b23502\b", r"null value in column .* violates not-null constraint"], DATA,
     "Empty value in a required column",
     "A row has no value for a column that the target table requires{column_hint}.",
     ["Fix the empty values at the source, or give the column a default / allow NULL if that is correct."], False),
    ("foreign_key", [r"\b23503\b", r"violates foreign key constraint"], TARGET, "Missing parent record (foreign key)",
     "A row refers to a parent record that is not in the target database yet{constraint_hint}.",
     ["Load the parent table first (order the activities, or add a dependency).", "Or check the source for orphaned keys."], False),
    ("check_constraint", [r"\b23514\b", r"violates check constraint"], DATA, "A value breaks a check rule on the table",
     "A value is outside the rule defined on the target table{constraint_hint}.",
     ["Fix the value at the source, or review the check constraint."], False),
    ("pg_firewall", [r"no pg_hba\.conf entry", r"is not allowed to connect", r"firewall rules?.*(?:server|postgres)"],
     NETWORK, "PostgreSQL firewall blocked Data Factory",
     "The PostgreSQL server refused the connection from Data Factory's network address.",
     ["On the Azure Database for PostgreSQL server: Networking -> allow public access from Azure services, add the "
      "integration runtime's IP, or use a managed private endpoint."], False),
    ("pg_auth", [r"\b28P01\b", r"\b28000\b", r"password authentication failed", r"authentication failed for user",
                 r"Login failed for user", r"invalid_password"],
     CREDS, "PostgreSQL login failed",
     "The user name or password in the PostgreSQL linked service is wrong, expired or was changed.",
     ["Update the password in the linked service (or the Key Vault secret it uses), then Test connection in ADF Studio."], False),
    ("pg_permission", [r"\b42501\b", r"permission denied for (?:table|schema|relation|database|sequence)"],
     CREDS, "PostgreSQL user lacks permission",
     "The login Data Factory uses is not allowed to write to {table} (or use its schema/sequence).",
     ["GRANT the needed rights (INSERT, and USAGE on the schema / sequences) to the linked service's user. The Data "
      "Estate -> Access page can request and apply this."], False),
    ("pg_ssl", [r"SSL (?:connection )?is required", r"requires? (?:an )?SSL", r"SSL negotiation", r"sslmode",
                r"certificate verify failed"], CREDS, "PostgreSQL SSL settings do not match",
     "The server requires an encrypted (SSL) connection, or the certificate check failed.",
     ["In the linked service set Encryption method / SSL mode to Require (Azure PostgreSQL always needs SSL)."], False),
    ("pg_capacity", [r"\b53300\b", r"too many connections", r"remaining connection slots", r"\b53100\b", r"disk full",
                     r"\b53200\b", r"out of memory"], TARGET, "PostgreSQL server is out of capacity",
     "The database server had no free connections, memory or disk space when the load ran.",
     ["Check the server's metrics in the Azure portal (connections, storage %, memory).",
      "Lower the Copy activity's parallel copies / max concurrent connections, or scale the server; then re-run."], True),
    ("pg_timeout", [r"\b57014\b", r"canceling statement due to statement timeout", r"\b40P01\b", r"deadlock detected",
                    r"\b55P03\b", r"lock timeout"], TARGET, "Query timed out or was blocked in PostgreSQL",
     "The load was cancelled by a statement timeout, or it waited on a lock / deadlock held by another session.",
     ["Look for long-running or locking sessions on the target at that time (pg_stat_activity).",
      "Re-run outside busy hours, or raise the timeout on the linked service."], True),
    ("ir_unavailable", [r"integration runtime .*(?:offline|unavailable|not (?:available|online))", r"NoAvailableIRNodes",
                        r"IntegrationRuntime.*(?:offline|Limited)", r"self-?hosted integration runtime .*(?:not|offline)"],
     ADF, "Integration runtime not available",
     "The integration runtime that runs this activity was offline or overloaded.",
     ["ADF Studio -> Manage -> Integration runtimes: check its status and node health; restart the service if self-hosted."], True),
    ("expression_error", [r"InvalidTemplate", r"cannot be evaluated", r"The expression '.*' (?:is not valid|cannot)",
                          r"property '.*' doesn't exist", r"BadRequest.*expression"], ADF,
     "Pipeline expression error",
     "A dynamic expression (parameter, variable or function) in the pipeline could not be evaluated for this run - "
     "typically a missing parameter value or a property that the previous activity did not return.",
     ["Check the parameters this run was started with (shown on this page) and the expression named in the error."], False),
    ("keyvault", [r"Key ?Vault", r"SecretNotFound", r"Forbidden.*vault"], CREDS, "Could not read a secret from Key Vault",
     "The linked service takes its password from Azure Key Vault and Data Factory could not read it.",
     ["Give the factory's managed identity 'Get' permission on secrets (or the 'Key Vault Secrets User' role)."], False),
    ("activity_timeout", [r"Activity .*timed out", r"exceeded the (?:activity )?timeout", r"ActivityTimeout"], ADF,
     "Activity ran longer than its timeout",
     "The activity's policy timeout was reached before it finished.",
     ["Compare with the usual duration on this page; if the data grew, raise the timeout or scale the work.",
      "If it is unusually slow, check the source/target performance at that time."], True),
    ("throttled", [r"TooManyRequests", r"\b429\b", r"ServerBusy", r"throttl"], AZURE, "Azure throttled the requests",
     "The service on the other side asked Data Factory to slow down.", ["Usually transient: re-run, or add retries on the activity policy."], True),
    ("network", [r"timed? ?out", r"Connection refused", r"No such host", r"Name or service not known",
                 r"A connection attempt failed", r"could not connect", r"Failed to connect", r"ConnectionFailed",
                 r"The remote name could not be resolved", r"actively refused", r"host .* unreachable",
                 r"An existing connection was forcibly closed"], NETWORK, "Could not reach the server",
     "Data Factory could not open or keep a network connection (DNS, firewall, private endpoint, or the server was down).",
     ["Test connection on the linked service in ADF Studio.", "Check the server is running and the network path "
      "(firewall, VNet, private DNS) allows the integration runtime."], True),
    ("dataflow", [r"DF-Executor-", r"DF-SYS-", r"DF-\w+-"], ADF, "Mapping data flow failed",
     "The data flow cluster raised an error (memory, a transformation, or a sink/source problem inside the flow).",
     ["Open the data flow run in ADF Monitor for the failing transformation; for out-of-memory use a larger compute size."], False),
]

SYSTEM_ERROR = ("transient_system", AZURE, "Azure-side error",
                "Data Factory reported a system error (not caused by your data or settings). These are usually temporary.",
                ["Re-run the pipeline. If it keeps failing, open a support case with the activity run ID."], True)
USER_ERROR = ("user_error", ADF, "Configuration or data problem",
              "Data Factory reported a user error: something in the pipeline settings, the connections or the data needs a change.",
              ["Read the error message below; it names the setting or value at fault."], False)

COMPILED = [(rid, [re.compile(p, re.I | re.S) for p in pats], system, title, expl, fixes, retry)
            for rid, pats, system, title, expl, fixes, retry in RULES]

SQLSTATE = re.compile(r"(?:SQLSTATE[: =]*|\[|\b)(0[0-9A-Z]{4}|2[0-9A-Z]{4}|4[0-9A-Z]{4}|5[0-9A-Z]{4})(?:\]|:|\b)")


def _first(patterns: list, text: str):
    for p in patterns:
        m = re.search(p, text, re.I)
        if m:
            return m.group(1)
    return None


def extract_details(text: str) -> dict:
    """Names the error message mentions (path, table, column, ...), used to make the explanation specific."""
    d = {
        "path": _first([r"Path:?\s*'([^']+)'", r"[Pp]ath '([^']+)'", r"file '([^']+)'", r"FolderPath:?\s*'([^']+)'",
                        r"[Bb]lob(?:Name)?:?\s*'([^']+)'"], text),
        "account": _first([r"Account:?\s*'([^']+)'", r"https?://([a-z0-9]+)\.(?:dfs|blob)\."], text),
        "container": _first([r"FileSystem:?\s*'([^']+)'", r"Container(?:Name)?:?\s*'([^']+)'"], text),
        "table": _first([r"relation \"?([\w.\"]+?)\"? does not exist", r"for (?:table|relation) \"?([\w.]+)\"?",
                         r"[Tt]able:?\s*'([^']+)'", r"object name '([^']+)'"], text),
        "column": _first([r"column \"?([\w]+)\"?", r"[Cc]olumn(?:Name)?:?\s*'([^']+)'", r"[Cc]olumn '([^']+)'"], text),
        "constraint": _first([r"constraint \"([^\"]+)\""], text),
        "row": _first([r"[Rr]ow(?: number)?:?\s*(\d+)", r"line (\d+)", r"\bRow (\d+)"], text),
        "inner_codes": sorted(set(re.findall(r"ErrorCode[=:]\s*'?(\w+)", text))),
        "sqlstate": None,
    }
    for m in SQLSTATE.finditer(text):
        if m.group(1)[:2] in ("22", "23", "28", "40", "42", "53", "55", "57", "08", "3D", "3F"):
            d["sqlstate"] = m.group(1)
            break
    return {k: v for k, v in d.items() if v}


def leaf_message(message: str) -> str:
    """'Operation on target X failed: Operation on target Y failed: <real error>' -> '<real error>'."""
    msg = message or ""
    while True:
        m = re.match(r"^\s*Operation on target .+? failed:\s*(.*)$", msg, re.S)
        if not m:
            return msg.strip()
        msg = m.group(1)


def _fmt(template: str, details: dict) -> str:
    hints = {
        "row_hint": f" (around row {details['row']})" if details.get("row") else "",
        "column_hint": f" (column {details['column']})" if details.get("column") else "",
        "constraint_hint": f" (constraint {details['constraint']})" if details.get("constraint") else "",
    }
    values = {"path": details.get("path") or "the configured path", "table": details.get("table") or "the target table",
              "column": details.get("column") or "?", "constraint": details.get("constraint") or "?",
              "account": details.get("account") or "(see the dataset)", "container": details.get("container") or "(see the dataset)",
              "row": details.get("row") or "?", "file": details.get("path") or "?", **hints}
    try:
        return template.format(**values)
    except (KeyError, IndexError):
        return template


def classify(code: str | None, message: str | None, failure_type: str | None = None, activity_type: str | None = None,
             status: str | None = None) -> dict:
    text = f"{code or ''} {message or ''}"
    details = extract_details(text)
    probe = " ".join([text, *details.get("inner_codes", []), details.get("sqlstate") or ""])
    if status == "Cancelled":
        probe = "__cancelled__"
    elif activity_type == "Fail":
        probe = "__fail_activity__"
    for rid, pats, system, title, expl, fixes, retry in COMPILED:
        if any(p.search(probe) for p in pats):
            break
    else:
        rid, system, title, expl, fixes, retry = SYSTEM_ERROR if (failure_type or "").lower() == "systemerror" else USER_ERROR
    return {"cause_id": rid, "system": system, "title": title, "explanation": _fmt(expl, details),
            "fixes": [_fmt(f, details) for f in fixes], "retry_helps": retry, "details": details,
            "error_code": code, "failure_type": failure_type, "message": leaf_message(message or "")}


def diagnose_task(task: dict) -> dict:
    d = classify(task.get("error_code"), task.get("error_message"), task.get("failure_type"), task.get("activity_type"),
                 task.get("status"))
    d.update(activity_name=task.get("activity_name"), activity_type=task.get("activity_type"),
             activity_run_id=task.get("activity_run_id"), run_id=task.get("run_id"), pipeline_name=task.get("pipeline_name"))
    return d


CONTAINER_TYPES = {"ForEach", "Until", "IfCondition", "Switch", "ExecutePipeline"}


def failure_chain(run: dict, tasks: list, child_lookup=None, depth: int = 0) -> list:
    """Root-cause activity failures of one pipeline run, each with its diagnosis and the path that led to it
    (e.g. ['Master pipeline', 'Run child (Execute Pipeline)', 'Child pipeline', 'Copy orders'])."""
    failed = [t for t in tasks if t.get("status") == "Failed"]
    roots = []
    for t in failed:
        if t.get("activity_type") == "ExecutePipeline" and child_lookup and depth < 5:
            child_id = ((t.get("output") or {}).get("pipelineRunId")) or ((t.get("output") or {}).get("pipelineRunID"))
            child = child_lookup(child_id) if child_id else None
            if child:
                child_run, child_tasks = child
                inner = failure_chain(child_run, child_tasks, child_lookup, depth + 1)
                if inner:
                    for r in inner:
                        r["path"] = [run.get("pipeline_name"), f"{t.get('activity_name')} (Execute Pipeline)"] + r["path"]
                    roots += inner
                    continue
        if t.get("activity_type") in CONTAINER_TYPES - {"ExecutePipeline"}:
            inner_failed = [x for x in failed if x is not t and x.get("activity_type") not in CONTAINER_TYPES
                            and (x.get("run_start") or "") >= (t.get("run_start") or "")
                            and (x.get("run_end") or "9") <= (t.get("run_end") or "9")]
            if inner_failed:
                continue                                     # the activity inside is the cause, reported on its own
        d = diagnose_task(t)
        d["path"] = [run.get("pipeline_name"), t.get("activity_name")]
        roots.append(d)
    if not roots and run.get("status") == "Failed":           # no activity details (no permission yet / not synced)
        d = classify(None, run.get("message"), None, None, None)
        d.update(activity_name=None, path=[run.get("pipeline_name")], run_id=run.get("run_id"),
                 pipeline_name=run.get("pipeline_name"))
        roots.append(d)
    if not roots and run.get("status") == "Cancelled":
        d = classify(None, run.get("message"), None, None, "Cancelled")
        d.update(activity_name=None, path=[run.get("pipeline_name")], run_id=run.get("run_id"), pipeline_name=run.get("pipeline_name"))
        roots.append(d)
    # earliest failure first: later ones are often consequences
    roots.sort(key=lambda r: next((t.get("run_start") or "" for t in tasks if t.get("activity_run_id") == r.get("activity_run_id")), ""))
    return roots
