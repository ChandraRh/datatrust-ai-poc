"""
Checks on the systems a Copy activity touched, so a run is judged on evidence and not only on ADF's own status.

Source (ADLS Gen2 / Blob storage)
    SOURCE_FILES     the files the activity read are there (count, size, last modified); changed after the copy?
    SOURCE_ROWS      data rows in the CSV file(s) vs rows ADF says it read (CSV only, up to ADF_PROBE_MAX_MB)
    SKIPPED_ROWS     rows ADF skipped (fault tolerance) and, when it logged them, the first reasons from its log file
Target (PostgreSQL)
    TARGET_TABLE     the table exists (and how many columns it has)
    TARGET_ROWS      rows in the table now vs rows this run wrote (fewer = deleted / truncated / rolled back later)
    SCHEMA_MATCH     the file's header vs the table's columns (columns that would be dropped, required columns missing)

Access, all read-only:
    storage   the factory's settings.storage_connection_env (a connection string), else the project's ADLS_* settings
              when it is the same account, else the same Azure identity as the factory (needs Storage Blob Data Reader)
    postgres  a Data Estate connection whose host matches the linked service (Connections page), else the factory's
              settings.target_connection_id, else this app's own database when the host and database are the same
"""
from __future__ import annotations

import csv
import fnmatch
import io
import os
from datetime import datetime, timezone

MAX_BYTES = int(float(os.getenv("ADF_PROBE_MAX_MB", "100")) * 1024 * 1024)


def _check(check_type, side, asset, status, expected=None, observed=None, detail=None):
    return {"check_type": check_type, "side": side, "asset": asset, "status": status, "expected": expected,
            "observed": observed, "detail": detail}


# ============================================================================ storage
def storage_client(account: str, cfg: dict, credential_factory):
    from azure.storage.blob import BlobServiceClient
    env = cfg.get("storage_connection_env")
    if env:
        from .client import _secret
        cs = _secret(env)
        if f"accountname={account}".lower() in cs.lower().replace(" ", "") or "UseDevelopmentStorage" in cs:
            return BlobServiceClient.from_connection_string(cs)
    import config
    if config.ADLS_ACCOUNT_NAME and config.ADLS_ACCOUNT_NAME.lower() == (account or "").lower():
        if config.ADLS_CONNECTION_STRING:
            return BlobServiceClient.from_connection_string(config.ADLS_CONNECTION_STRING)
        if config.ADLS_ACCOUNT_KEY:
            return BlobServiceClient(f"https://{account}.blob.core.windows.net", credential=config.ADLS_ACCOUNT_KEY)
    return BlobServiceClient(f"https://{account}.blob.core.windows.net", credential=credential_factory())


def _match_blobs(container, asset: dict) -> list:
    folder = (asset.get("wildcard_folder") or asset.get("folder") or "").strip("/")
    pattern = asset.get("wildcard_file") or asset.get("file") or ""
    has_wild = any(ch in folder + pattern for ch in "*?")
    if pattern and not has_wild:
        name = f"{folder}/{pattern}" if folder else pattern
        try:
            props = container.get_blob_client(name).get_blob_properties()
            return [props]
        except Exception as e:                                           # noqa: BLE001
            if type(e).__name__ in ("ResourceNotFoundError",):
                return []
            raise
    prefix = folder.split("*")[0].split("?")[0]
    prefix = prefix + "/" if prefix and not has_wild else prefix
    out = []
    for b in container.list_blobs(name_starts_with=prefix or None):
        if b.name.endswith("/") or (getattr(b, "size", 0) == 0 and "." not in b.name.rsplit("/", 1)[-1]):
            continue                                                     # folder markers
        folder_part, _, file_part = b.name.rpartition("/")
        if folder and not (fnmatch.fnmatchcase(folder_part, folder) if has_wild else
                           (folder_part == folder or (asset.get("recursive") is not False and folder_part.startswith(folder + "/")))):
            continue
        if pattern and not fnmatch.fnmatchcase(file_part, pattern):
            continue
        out.append(b)
        if len(out) > 5000:
            break
    return out


def _count_rows(container, blob_name: str, header: bool) -> tuple[int, list]:
    """Data rows of a CSV blob (streamed) and its header columns."""
    stream = container.download_blob(blob_name)
    lines, last, head = 0, b"", b""
    for chunk in stream.chunks():
        if len(head) < 65536:
            head += chunk[:65536]
        lines += chunk.count(b"\n")
        last = chunk[-1:] if chunk else last
    if last and last != b"\n":
        lines += 1
    columns = []
    if head:
        first = head.split(b"\n", 1)[0].decode("utf-8-sig", errors="replace").rstrip("\r")
        columns = next(csv.reader([first]), [])
    return (lines - (1 if header and lines else 0)), columns


def source_checks(asset: dict, task: dict, client) -> tuple[list, list]:
    """(checks, header columns of the first file) for a storage source."""
    checks = []
    label = asset.get("name")
    container = client.get_container_client(asset["container"])
    blobs = _match_blobs(container, asset)
    started = task.get("run_start")
    started = datetime.fromisoformat(str(started)) if started and not isinstance(started, datetime) else started
    if not blobs:
        failed = task.get("status") == "Failed"
        checks.append(_check("SOURCE_FILES", "source", label, "FAIL" if failed else "WARN", "file(s) present", "0 files",
                             "Not found in storage now." + ("" if failed else " The copy succeeded, so the files may have been "
                                                                               "moved or deleted after it (archiving).")))
        return checks, []
    total = sum(getattr(b, "size", 0) or 0 for b in blobs)
    newest = max((b.last_modified for b in blobs if getattr(b, "last_modified", None)), default=None)
    changed = bool(started and newest and newest > (started if started.tzinfo else started.replace(tzinfo=timezone.utc)))
    names = ", ".join(b.name for b in blobs[:5]) + (f" … (+{len(blobs) - 5})" if len(blobs) > 5 else "")
    files_read = task.get("files_read")
    status = "WARN" if changed or (files_read and files_read != len(blobs)) else "PASS"
    detail = f"{names}; {total:,} bytes; last modified {newest:%Y-%m-%d %H:%M UTC}" if newest else names
    if changed:
        detail += ". A file changed AFTER this run started: the target may not hold the latest version."
    if files_read and files_read != len(blobs):
        detail += f". ADF read {files_read} file(s); {len(blobs)} match now."
    checks.append(_check("SOURCE_FILES", "source", label, status, f"{files_read} file(s) read by ADF" if files_read else "file(s) present",
                         f"{len(blobs)} file(s) now", detail))
    columns = []
    fmt = asset.get("format") or {}
    if fmt.get("type") == "DelimitedText" and not fmt.get("compression") and total <= MAX_BYTES:
        rows = 0
        for i, b in enumerate(blobs):
            n, cols = _count_rows(container, b.name, fmt.get("header"))
            rows += n
            if i == 0:
                columns = cols if fmt.get("header") else []
        read = task.get("rows_read")
        if read is None:
            checks.append(_check("SOURCE_ROWS", "source", label, "INFO", None, rows, f"{rows:,} data rows in storage now."))
        else:
            st = "PASS" if rows == read else "WARN"
            checks.append(_check("SOURCE_ROWS", "source", label, st, read, rows,
                                 f"Storage has {rows:,} data rows; ADF read {read:,}." +
                                 ("" if st == "PASS" else " Different: the file changed after the run, or rows contain line breaks inside quotes.")))
    elif total > MAX_BYTES:
        checks.append(_check("SOURCE_ROWS", "source", label, "UNKNOWN", task.get("rows_read"), None,
                             f"Files are larger than ADF_PROBE_MAX_MB ({MAX_BYTES // 1048576} MB): rows not counted."))
    return checks, columns


def skipped_rows_check(task: dict, client_for_account) -> dict | None:
    skipped = task.get("rows_skipped") or 0
    if not skipped:
        return None
    out = task.get("output") or {}
    log = out.get("logFilePath") or (out.get("logPath")) or ""
    sample = []
    if log and client_for_account:
        try:
            parts = log.strip("/").split("/", 1)
            container_name, prefix = parts[0], (parts[1] if len(parts) > 1 else "")
            account = None
            for d in out.get("executionDetails") or []:
                account = account or (d.get("logStorageAccount"))
            client = client_for_account(account)
            if client:
                container = client.get_container_client(container_name)
                for b in container.list_blobs(name_starts_with=prefix):
                    text = container.download_blob(b.name, offset=0, length=65536).readall().decode("utf-8", errors="replace")
                    for row in list(csv.reader(io.StringIO(text)))[1:21]:
                        sample.append(" | ".join(row[-2:]) if len(row) >= 2 else ",".join(row))
                    if len(sample) >= 20:
                        break
        except Exception as e:                                           # noqa: BLE001
            sample.append(f"(could not read the skipped-rows log {log}: {type(e).__name__})")
    detail = f"ADF skipped {skipped:,} row(s) it could not load (fault tolerance)."
    if log:
        detail += f" Log: {log}."
    else:
        detail += " Turn on logging in the Copy activity's fault-tolerance settings to see which rows and why."
    if sample:
        detail += "\nFirst logged rows:\n" + "\n".join(sample[:20])
    return _check("SKIPPED_ROWS", "target", None, "WARN", 0, skipped, detail)


# ============================================================================ postgres
def find_pg_connection(asset: dict, cfg: dict):
    """A connection to the target database (estate connection, configured one, or this app's own database)."""
    from estate import store as estate_store
    from estate.postgres_connector import PostgresSource
    import config
    host = (asset.get("host") or "").lower()
    db = asset.get("database")
    try:
        conns = [c for c in estate_store.list_connections() if c["source_type"] == "postgresql"]
    except Exception:                                                    # noqa: BLE001
        conns = []
    match = [c for c in conns if host and c["host"].lower() == host]
    if not match and cfg.get("target_connection_id"):
        match = [c for c in conns if c["connection_id"] == cfg["target_connection_id"]]
    if match:
        c = match[0]
        return PostgresSource(c), db or c["database"], f"Data Estate connection '{c['name']}'"
    if host and host == config.PG_HOST.lower() and (not db or db == config.PG_DATABASE):
        own = {"host": config.PG_HOST, "port": config.PG_PORT, "database": config.PG_DATABASE, "username": config.PG_USER,
               "password_env": "PG_PASSWORD"}
        return PostgresSource(own), db or config.PG_DATABASE, "this app's database"
    return None, db, None


def target_checks(asset: dict, task: dict, cfg: dict, source_columns: list, mapping: list | None, compare_rows: bool) -> list:
    label = asset.get("name")
    src, db, via = find_pg_connection(asset, cfg)
    if not src:
        return [_check("TARGET_TABLE", "target", label, "UNKNOWN", None, None,
                       f"No connection to {asset.get('host') or 'the target server'} to check it. Add it on Data estate -> "
                       f"Connections (same host name as the ADF linked service), with a read-only login.")]
    if not asset.get("table"):
        return [_check("TARGET_TABLE", "target", label, "UNKNOWN", None, None, "Table name is a runtime expression; not checked.")]
    checks = []
    with src.connect(db) as conn:
        cols = conn.execute("""SELECT column_name, is_nullable, column_default, data_type FROM information_schema.columns
                               WHERE table_schema = %s AND table_name = %s ORDER BY ordinal_position""",
                            (asset["schema"], asset["table"])).fetchall()
        if not cols:
            checks.append(_check("TARGET_TABLE", "target", label, "FAIL", "table exists", "not found",
                                 f"{asset['schema']}.{asset['table']} does not exist in {db} (checked via {via})."))
            return checks
        checks.append(_check("TARGET_TABLE", "target", label, "PASS", "table exists", f"{len(cols)} columns",
                             f"{asset['schema']}.{asset['table']} found in {db} (via {via})."))
        est = conn.execute("""SELECT c.reltuples::bigint AS est FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
                              WHERE n.nspname = %s AND c.relname = %s""", (asset["schema"], asset["table"])).fetchone()
        from psycopg import sql
        if est and (est["est"] or 0) > 5_000_000:
            count, how = est["est"], "estimated"
        else:
            count = conn.execute(sql.SQL("SELECT count(*) AS n FROM {}.{}").format(
                sql.Identifier(asset["schema"]), sql.Identifier(asset["table"]))).fetchone()["n"]
            how = "counted"
        written = task.get("rows_written")
        if compare_rows and written is not None and task.get("status") == "Succeeded":
            st = "PASS" if count >= written else "FAIL"
            checks.append(_check("TARGET_ROWS", "target", label, st, f">= {written:,} (rows this run wrote)", f"{count:,} ({how})",
                                 "Table holds at least the rows this run loaded." if st == "PASS" else
                                 "The table has FEWER rows than this run loaded: rows were deleted or truncated afterwards, or a "
                                 "later step replaced the data."))
        else:
            checks.append(_check("TARGET_ROWS", "target", label, "INFO", None, f"{count:,} ({how})", f"{count:,} rows in the table now."))
        table_cols = {c["column_name"].lower(): c for c in cols}
        incoming = [m.lower() for m in (mapping or [])] or [c.lower() for c in source_columns]
        if incoming:
            extra = [c for c in incoming if c not in table_cols]
            required = [n for n, c in table_cols.items() if c["is_nullable"] == "NO" and not c["column_default"] and n not in incoming]
            if extra or required:
                parts = []
                if extra:
                    parts.append(f"not in the table (dropped or failing): {', '.join(extra[:10])}")
                if required:
                    parts.append(f"required in the table but not in the {'mapping' if mapping else 'file'}: {', '.join(required[:10])}")
                checks.append(_check("SCHEMA_MATCH", "target", label, "WARN", f"{len(incoming)} incoming columns",
                                     f"{len(table_cols)} table columns", "Columns differ - " + "; ".join(parts) + "."))
            else:
                checks.append(_check("SCHEMA_MATCH", "target", label, "PASS", f"{len(incoming)} incoming columns",
                                     f"{len(table_cols)} table columns", "Every incoming column exists in the table."))
    return checks
