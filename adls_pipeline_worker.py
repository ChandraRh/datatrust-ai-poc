"""Watch ADLS for complete CSV batches and track their runs in PostgreSQL."""
from __future__ import annotations

import argparse
import os
import time
import traceback
from dataclasses import dataclass
from datetime import date

import adls_utils
import batch_status
import config
import run_pipeline

READY_MARKER = "_READY"
# Read old terminal markers once when importing batches created by an earlier worker version.
PROCESSED_MARKER = "_PROCESSED"
FAILED_MARKER = "_FAILED"


@dataclass(frozen=True)
class Batch:
    folder: str
    name: str
    missing_files: tuple[str, ...]
    legacy_state: str | None


def discover_batches(container, root: str) -> list[Batch]:
    """Find ADLS batch folders whose upload-complete marker is present."""
    root = root.strip("/")
    prefix = f"{root}/" if root else ""
    names = {blob.name for blob in container.list_blobs(name_starts_with=prefix)}
    ready = sorted(name for name in names if name.endswith(f"/{READY_MARKER}"))
    batches = []
    for marker in ready:
        folder = marker.rsplit("/", 1)[0]
        name = folder.rsplit("/", 1)[-1]
        if f"{folder}/{PROCESSED_MARKER}" in names:
            legacy_state = "processed"
        elif f"{folder}/{FAILED_MARKER}" in names:
            legacy_state = "failed"
        else:
            legacy_state = None
        missing = tuple(
            source["file"]
            for source in config.SOURCES
            if f"{folder}/{source['file']}" not in names
        )
        batches.append(Batch(folder, name, missing, legacy_state))
    return sorted(batches, key=lambda batch: (batch.name != "full", batch.name))


def run_batch(batch: Batch) -> int:
    """Run an eligible batch and persist its result in PostgreSQL."""
    claimed = batch_status.claim_batch(batch.folder)
    if claimed is None:
        return 0

    if batch.name == "full":
        args = ["--full"]
    else:
        try:
            if date.fromisoformat(batch.name).isoformat() != batch.name:
                raise ValueError
        except ValueError:
            message = f"Folder must be 'full' or an ISO date (YYYY-MM-DD): {batch.name}"
            batch_status.finish_batch(claimed["batch_id"], "FAILED", message)
            print(f"Failed ADLS batch {batch.folder}: {message}")
            return 1
        args = [batch.name]

    if batch.missing_files:
        message = f"Missing required CSV files: {', '.join(batch.missing_files)}"
        batch_status.finish_batch(claimed["batch_id"], "FAILED", message)
        print(f"Failed ADLS batch {batch.folder}: {message}")
        return 1

    config.ADLS_FOLDER = batch.folder
    config.ADLS_DOWNLOAD_DIR = config.PROJECT_DIR / "data" / "adls_download" / batch.name
    print(f"Starting ADLS batch {batch.name}: {batch.folder} (attempt {claimed['attempt_count']})")
    error = None
    pipeline_batch_id = None
    try:
        result = run_pipeline.main(args)
    except Exception as exc:
        traceback.print_exc()
        result = 1
        error = f"{type(exc).__name__}: {exc}"[:2000]

    try:
        pipeline_batch_id = batch_status.latest_pipeline_batch(claimed["started_at"])
        if result != 0 and error is None:
            error = batch_status.latest_pipeline_error(claimed["started_at"], pipeline_batch_id)
    except Exception:
        traceback.print_exc()
        if result != 0 and error is None:
            error = "Pipeline failed. See the Pipeline log for step-level details."
    if result != 0 and error is None:
        error = "Pipeline returned a failure status. See the Pipeline log for step-level details."

    final_status = "SUCCEEDED" if result == 0 else "FAILED"
    batch_status.finish_batch(claimed["batch_id"], final_status, error, pipeline_batch_id)

    if result == 0:
        print(f"Completed ADLS batch {batch.name}.")
    else:
        print(f"ADLS batch {batch.name} failed: {error or 'see Pipeline log for step-level details.'}")
    return result


def process_ready_batches(container, root: str) -> int:
    failed = False
    discovered = discover_batches(container, root)
    by_folder = {batch.folder: batch for batch in discovered}
    for batch in discovered:
        batch_status.register_batch(batch.folder, batch.name, batch.legacy_state)
    for pending in batch_status.ready_batches():
        batch = by_folder.get(pending["batch_folder"])
        if batch is None:
            batch = Batch(pending["batch_folder"], pending["batch_name"], (), None)
            claimed = batch_status.claim_batch(batch.folder)
            if claimed:
                batch_status.finish_batch(
                    claimed["batch_id"], "FAILED", "The ADLS _READY marker is missing; the batch was not run."
                )
                failed = True
            continue
        if run_batch(batch) != 0:
            failed = True
    return 1 if failed else 0


def take_ready_messages():
    """In Azure, Event Grid puts a message in a queue when a _READY marker is uploaded, and that message starts this job.

    Take the messages that are there now (hidden for an hour, not yet deleted). The run scans ADLS anyway, so the
    messages only say "look now"; they are deleted after the run (delete_messages). A message that arrives during the
    run stays in the queue and starts the next run. Returns (queue, messages), or (None, []) when no queue is set.
    """
    conn = os.getenv("ADLS_QUEUE_CONNECTION")
    name = os.getenv("ADLS_QUEUE_NAME")
    if not (conn and name):
        return None, []
    try:
        from azure.storage.queue import QueueClient

        queue = QueueClient.from_connection_string(conn, name)
        messages = list(queue.receive_messages(max_messages=32, visibility_timeout=3600))
        print(f"Started by {len(messages)} upload message(s) from queue '{name}'.")
        return queue, messages
    except Exception:
        traceback.print_exc()
        return None, []


def delete_messages(queue, messages) -> None:
    for message in messages:
        try:
            queue.delete_message(message)
        except Exception:
            traceback.print_exc()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true", help="check ADLS once, then exit")
    parser.add_argument(
        "--poll-seconds",
        type=int,
        default=int(os.getenv("ADLS_WORKER_POLL_SECONDS", "30")),
        help="seconds between ADLS checks (default: ADLS_WORKER_POLL_SECONDS or 30)",
    )
    args = parser.parse_args()
    if args.poll_seconds < 1:
        parser.error("--poll-seconds must be at least 1")
    if config.SOURCE_MODE.lower() != "adls":
        parser.error("set SOURCE_MODE=adls for the ADLS pipeline worker")

    batch_status.ensure_table()
    interrupted = batch_status.fail_interrupted_batches()
    if interrupted:
        print(f"Marked {interrupted} interrupted batch(es) failed after worker restart.")

    container = adls_utils.get_container()
    root = config.ADLS_FOLDER
    print(
        f"Watching ADLS container '{config.ADLS_CONTAINER}' under '{root}' "
        f"every {args.poll_seconds} seconds."
    )
    while True:
        queue, messages = take_ready_messages() if args.once else (None, [])
        try:
            result = process_ready_batches(container, root)
        finally:
            # A failed batch is recorded as FAILED and is not retried, so its message is done either way.
            delete_messages(queue, messages)
        if args.once:
            return result
        time.sleep(args.poll_seconds)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("ADLS pipeline worker stopped.")
