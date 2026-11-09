import sys
import tempfile
import unittest
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import patch

import upload_to_adls


class BlobClient:
    def __init__(self, container, name):
        self.container = container
        self.name = name

    def exists(self):
        return self.name in self.container.names

    def delete_blob(self):
        self.container.names.discard(self.name)


class Container:
    def __init__(self, names=()):
        self.names = set(names)
        self.uploads = []

    def get_container_properties(self):
        return {}

    def get_blob_client(self, name):
        return BlobClient(self, name)

    def upload_blob(self, name, data, overwrite):
        self.uploads.append((name, data, overwrite))
        self.names.add(name)


class UploadToAdlsTests(unittest.TestCase):
    def run_upload(self, container, temp_dir, batch="2026-11-09", current=None):
        with (
            patch.object(upload_to_adls.config, "SOURCES", [{"file": "source.csv"}]),
            patch.object(upload_to_adls.config, "LOCAL_SOURCE_DIR", Path(temp_dir)),
            patch.object(upload_to_adls.config, "ADLS_FOLDER", "batches"),
            patch.object(upload_to_adls.adls_utils, "get_container", return_value=container),
            patch.object(upload_to_adls.adls_utils, "upload_folder", return_value=1) as upload,
            patch.object(upload_to_adls.batch_status, "ensure_table"),
            patch.object(upload_to_adls.batch_status, "get_batch_by_folder", return_value=current),
            patch.object(upload_to_adls.batch_status, "retry_failed_upload") as retry,
            patch.object(upload_to_adls, "capture_errors", return_value=nullcontext()),
            patch.object(sys, "argv", ["upload_to_adls.py", batch]),
        ):
            result = upload_to_adls.main()
        return result, upload, retry

    def test_requires_canonical_iso_date(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            with self.assertRaises(SystemExit):
                self.run_upload(Container(), temp_dir, "20261109")

    def test_refuses_to_replace_processed_batch(self):
        folder = "batches/2026-11-09"
        container = Container()
        with tempfile.TemporaryDirectory() as temp_dir:
            Path(temp_dir, "source.csv").touch()
            with self.assertRaises(SystemExit):
                self.run_upload(container, temp_dir, current={"status": "SUCCEEDED"})

        self.assertEqual(container.uploads, [])

    def test_refuses_ready_batch_that_is_not_failed(self):
        folder = "batches/2026-11-09"
        container = Container([f"{folder}/_READY"])
        with tempfile.TemporaryDirectory() as temp_dir:
            Path(temp_dir, "source.csv").touch()
            with self.assertRaises(SystemExit):
                self.run_upload(container, temp_dir)

        self.assertIn(f"{folder}/_READY", container.names)
        self.assertEqual(container.uploads, [])

    def test_retry_replaces_failed_batch_then_writes_ready_last(self):
        folder = "batches/2026-11-09"
        container = Container([f"{folder}/_READY", f"{folder}/_FAILED"])
        with tempfile.TemporaryDirectory() as temp_dir:
            Path(temp_dir, "source.csv").touch()
            result, upload, retry = self.run_upload(
                container, temp_dir, current={"batch_id": 7, "status": "FAILED"}
            )

        self.assertEqual(result, 0)
        upload.assert_called_once_with(Path(temp_dir), folder)
        retry.assert_called_once_with(folder)
        self.assertNotIn(f"{folder}/_FAILED", container.names)
        self.assertIn(f"{folder}/_READY", container.names)
        self.assertEqual(container.uploads[-1], (f"{folder}/_READY", b"", True))


if __name__ == "__main__":
    unittest.main()
