
"""
Upload the source CSV files (data/source_csv) to your ADLS Gen2 container.
Upload one complete batch and write its _READY marker last so the worker can process it.
Batch status is stored in PostgreSQL. A failed batch can be uploaded again with the same batch name.

    python upload_to_adls.py full
    python upload_to_adls.py 2026-11-09
"""
import argparse
from datetime import date
import sys

import adls_utils
import batch_status
import config
from db_utils import capture_errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("batch", help="'full' for the initial load or a business date in YYYY-MM-DD format")
    args = parser.parse_args()
    if args.batch != "full":
        try:
            if date.fromisoformat(args.batch).isoformat() != args.batch:
                raise ValueError
        except ValueError:
            parser.error("batch must be 'full' or a date in YYYY-MM-DD format")

    required = {source["file"] for source in config.SOURCES}
    available = {path.name for path in config.LOCAL_SOURCE_DIR.glob("*.csv")}
    missing = sorted(required - available)
    if missing:
        sys.exit(f"missing required source CSV files in {config.LOCAL_SOURCE_DIR}: {', '.join(missing)}")

    batch_folder = f"{config.ADLS_FOLDER.strip('/')}/{args.batch}".strip("/")
    try:
        with capture_errors("ADLS", "PL_ADLS_CONNECT"):
            container = adls_utils.get_container()
            container.get_container_properties()
            print(f"ADLS connection OK ({config.ADLS_CONTAINER})")
        batch_status.ensure_table()
        current = batch_status.get_batch_by_folder(batch_folder)
        ready = container.get_blob_client(f"{batch_folder}/_READY")
        processed = container.get_blob_client(f"{batch_folder}/_PROCESSED")
        failed = container.get_blob_client(f"{batch_folder}/_FAILED")
        if processed.exists() or (current and current["status"] == "SUCCEEDED"):
            sys.exit(f"batch {args.batch} is already processed; use a new batch folder")
        if current and current["status"] in {"READY", "RUNNING"}:
            sys.exit(f"batch {args.batch} is {current['status'].lower()}; do not replace its files")
        if ready.exists() and not (current and current["status"] == "FAILED"):
            sys.exit(f"batch {args.batch} is already ready or being processed")
        if ready.exists():
            ready.delete_blob()
        if failed.exists():
            failed.delete_blob()
        with capture_errors("ADLS", "PL_ADLS_UPLOAD"):
            count = adls_utils.upload_folder(config.LOCAL_SOURCE_DIR, batch_folder)
            container.upload_blob(f"{batch_folder}/_READY", b"", overwrite=True)
            if current and current["status"] == "FAILED":
                if batch_status.retry_failed_upload(batch_folder) is None:
                    raise RuntimeError("batch status changed during upload; inspect the Pipeline log before retrying")
        print(f"\n{count} CSV files uploaded to {config.ADLS_CONTAINER}/{batch_folder}.")
        print("Uploaded _READY marker last; the pipeline worker can now process this batch.")
        return 0
    except Exception as e:
        sys.exit(f"\nupload failed: {type(e).__name__}: {e}")


if __name__ == "__main__":
    sys.exit(main())
