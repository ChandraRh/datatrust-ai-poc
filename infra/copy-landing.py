"""Copy the POC landing files from the ADLS account in .env to the POC service's own ADLS account in Azure.

    python infra/copy-landing.py <destination account> [--container landing]
    destination key: environment variable LANDING_DEST_KEY (infra/poc/deploy.ps1 reads it from the POC Key Vault)

Files that already exist at the destination are skipped, so it can be run again. _READY markers are copied last: in the
destination, each one fires an upload event that starts the pipeline job, and the batch's CSV files must be there first.
Batches the pipeline already ran (audit.adls_batch_runs) are not run again.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from azure.storage.blob import BlobServiceClient  # noqa: E402

import adls_utils  # noqa: E402
import config  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("account", help="destination storage account name")
    parser.add_argument("--container", default=config.ADLS_CONTAINER)
    args = parser.parse_args()

    key = os.getenv("LANDING_DEST_KEY")
    if not key:
        print("Set LANDING_DEST_KEY to the destination account key.")
        return 2

    source = adls_utils.get_container()
    if source.account_name == args.account:
        print("Source and destination are the same account; nothing to copy.")
        return 0
    dest = BlobServiceClient(f"https://{args.account}.blob.core.windows.net", credential=key).get_container_client(args.container)
    if not dest.exists():
        dest.create_container()

    existing = {b.name for b in dest.list_blobs()}
    blobs = sorted(source.list_blobs(), key=lambda b: (b.name.endswith("/_READY"), b.name))
    copied = skipped = 0
    for blob in blobs:
        if blob.name in existing:
            skipped += 1
            continue
        data = source.download_blob(blob.name).readall()
        dest.upload_blob(blob.name, data, overwrite=False)
        copied += 1
        print(f"copied {blob.name} ({len(data):,} bytes)")
    print(f"Landing files: {copied} copied, {skipped} already there, from {source.account_name}/{source.container_name} "
          f"to {args.account}/{args.container}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
