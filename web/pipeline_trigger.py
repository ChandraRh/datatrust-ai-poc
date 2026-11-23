"""Start the POC pipeline after an admin retries a failed batch.

In Azure the pipeline job starts when its queue has a message (Event Grid puts one there for every _READY upload). A retry
only marks the batch READY in the database, so DataTrust also adds a message to that queue, and the job starts within a
minute. The job scans for READY batches itself; the message only says "look now".

    PIPELINE_TRIGGER_QUEUE_URL   https://<account>.queue.core.windows.net/<queue>   (set by infra/datatrust/deploy.ps1)

DataTrust signs in with its managed identity (AZURE_CLIENT_ID), which may only add messages to that one queue
(Storage Queue Data Message Sender). No key is involved. Unset (a local install): nothing is sent; the local worker
(python adls_pipeline_worker.py) finds READY batches by itself every 30 seconds while it runs.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone

_LOCAL_NOTE = "The local pipeline worker picks it up within 30 seconds while it is running (python adls_pipeline_worker.py)."


def queue_url() -> str:
    return (os.getenv("PIPELINE_TRIGGER_QUEUE_URL") or "").strip()


def _queue_client(url: str):
    from azure.identity import DefaultAzureCredential   # managed identity in Azure (AZURE_CLIENT_ID)
    from azure.storage.queue import QueueClient

    return QueueClient.from_queue_url(url, credential=DefaultAzureCredential(exclude_interactive_browser_credential=True))


def start_pipeline(batch_name: str, requested_by: str) -> dict:
    """Ask the pipeline job to run now. Never raises: the retry itself is already recorded."""
    url = queue_url()
    if not url:
        return {"pipeline_started": False, "pipeline_note": _LOCAL_NOTE}
    message = {"reason": "retry", "batch": batch_name, "requested_by": requested_by,
               "at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    try:
        _queue_client(url).send_message(json.dumps(message))
        return {"pipeline_started": True, "pipeline_note": "The pipeline starts within about a minute."}
    except Exception as e:  # noqa: BLE001 - reported to the admin, the batch stays READY
        return {"pipeline_started": False,
                "pipeline_note": f"Could not start the pipeline ({type(e).__name__}); it runs at the next upload "
                                 f"or the daily run."}
