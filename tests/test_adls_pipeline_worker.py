import unittest
from unittest.mock import patch

import adls_pipeline_worker as worker


class Blob:
    def __init__(self, name):
        self.name = name


class Container:
    def __init__(self, names=()):
        self.names = set(names)
        self.uploads = []

    def list_blobs(self, name_starts_with):
        return [Blob(name) for name in self.names if name.startswith(name_starts_with)]

    def upload_blob(self, name, data, overwrite):
        self.uploads.append((name, data, overwrite))
        self.names.add(name)


class AdlsPipelineWorkerTests(unittest.TestCase):
    def setUp(self):
        self.original_folder = worker.config.ADLS_FOLDER
        self.original_download_dir = worker.config.ADLS_DOWNLOAD_DIR

    def tearDown(self):
        worker.config.ADLS_FOLDER = self.original_folder
        worker.config.ADLS_DOWNLOAD_DIR = self.original_download_dir

    def make_batch_names(self, folder):
        return [f"{folder}/{source['file']}" for source in worker.config.SOURCES]

    def test_discover_batches_requires_all_source_files(self):
        folder = "batches/2026-11-09"
        container = Container([f"{folder}/{worker.READY_MARKER}", *self.make_batch_names(folder)[:-1]])

        [batch] = worker.discover_batches(container, "batches")

        self.assertEqual(batch.folder, folder)
        self.assertEqual(batch.name, "2026-11-09")
        self.assertIsNone(batch.legacy_state)
        self.assertEqual(len(batch.missing_files), 1)

    def test_discover_batches_maps_legacy_terminal_markers(self):
        processed = "batches/2026-11-08"
        failed = "batches/2026-11-09"
        names = [
            f"{processed}/{worker.READY_MARKER}",
            f"{processed}/{worker.PROCESSED_MARKER}",
            f"{failed}/{worker.READY_MARKER}",
            f"{failed}/{worker.FAILED_MARKER}",
        ]
        container = Container(names)

        batches = worker.discover_batches(container, "batches")

        self.assertEqual([batch.legacy_state for batch in batches], ["processed", "failed"])

    def test_successful_batch_records_success_in_postgres(self):
        folder = "batches/2026-11-09"
        container = Container(self.make_batch_names(folder))
        batch = worker.Batch(folder, "2026-11-09", (), None)
        claim = {"batch_id": 1, "attempt_count": 1, "started_at": "start"}

        with (
            patch.object(worker.batch_status, "claim_batch", return_value=claim),
            patch.object(worker.batch_status, "latest_pipeline_batch", return_value="pipeline-1"),
            patch.object(worker.batch_status, "finish_batch") as finish,
            patch.object(worker.run_pipeline, "main", return_value=0) as run,
        ):
            result = worker.run_batch(batch)

        self.assertEqual(result, 0)
        run.assert_called_once_with(["2026-11-09"])
        finish.assert_called_once_with(1, "SUCCEEDED", None, "pipeline-1")

    def test_failed_batch_records_failure_in_postgres_without_adls_marker(self):
        folder = "batches/full"
        container = Container(self.make_batch_names(folder))
        batch = worker.Batch(folder, "full", (), None)
        claim = {"batch_id": 1, "attempt_count": 1, "started_at": "start"}

        with (
            patch.object(worker.batch_status, "claim_batch", return_value=claim),
            patch.object(worker.batch_status, "latest_pipeline_batch", return_value="pipeline-1"),
            patch.object(worker.batch_status, "latest_pipeline_error", return_value="Spark failed"),
            patch.object(worker.batch_status, "finish_batch") as finish,
            patch.object(worker.run_pipeline, "main", return_value=1) as run,
        ):
            result = worker.run_batch(batch)

        self.assertEqual(result, 1)
        run.assert_called_once_with(["--full"])
        finish.assert_called_once_with(1, "FAILED", "Spark failed", "pipeline-1")
        self.assertEqual(container.uploads, [])

    def test_batch_folder_requires_canonical_iso_date(self):
        folder = "batches/20261109"
        container = Container()
        batch = worker.Batch(folder, "20261109", (), None)
        claim = {"batch_id": 1, "attempt_count": 1, "started_at": "start"}

        with (
            patch.object(worker.batch_status, "claim_batch", return_value=claim),
            patch.object(worker.batch_status, "finish_batch") as finish,
            patch.object(worker.run_pipeline, "main") as run,
        ):
            result = worker.run_batch(batch)

        self.assertEqual(result, 1)
        run.assert_not_called()
        finish.assert_called_once()
        self.assertEqual(finish.call_args.args[1], "FAILED")

    def test_batch_with_missing_csvs_is_failed_in_postgres(self):
        folder = "batches/2026-11-09"
        batch = worker.Batch(folder, "2026-11-09", ("payments.csv",), None)
        claim = {"batch_id": 1, "attempt_count": 1, "started_at": "start"}

        with (
            patch.object(worker.batch_status, "claim_batch", return_value=claim),
            patch.object(worker.batch_status, "finish_batch") as finish,
            patch.object(worker.run_pipeline, "main") as run,
        ):
            result = worker.run_batch(batch)

        self.assertEqual(result, 1)
        run.assert_not_called()
        finish.assert_called_once_with(1, "FAILED", "Missing required CSV files: payments.csv")

    def test_processing_keeps_root_for_later_poll_cycles(self):
        folders = ["batches/2026-11-08", "batches/2026-11-09"]
        names = []
        for folder in folders:
            names.extend(self.make_batch_names(folder))
            names.append(f"{folder}/{worker.READY_MARKER}")
        container = Container(names)
        statuses = {}

        def register(folder, name, legacy_state):
            statuses.setdefault(folder, "READY")
            return {"status": statuses[folder]}

        def claim(folder):
            if statuses[folder] != "READY":
                return None
            statuses[folder] = "RUNNING"
            return {
                "batch_id": folder,
                "attempt_count": 1,
                "started_at": "start",
            }

        def finish(batch_id, status, error, pipeline_batch_id):
            statuses[batch_id] = status

        def ready_batches():
            return [
                {"batch_folder": folder, "batch_name": folder.rsplit("/", 1)[-1]}
                for folder, status in statuses.items()
                if status == "READY"
            ]

        with (
            patch.object(worker.batch_status, "register_batch", side_effect=register),
            patch.object(worker.batch_status, "claim_batch", side_effect=claim),
            patch.object(worker.batch_status, "finish_batch", side_effect=finish),
            patch.object(worker.batch_status, "ready_batches", side_effect=ready_batches),
            patch.object(worker.batch_status, "latest_pipeline_batch", return_value=None),
            patch.object(worker.run_pipeline, "main", return_value=0) as run,
        ):
            self.assertEqual(worker.process_ready_batches(container, "batches"), 0)
            later_folder = "batches/2026-11-10"
            container.names.update(self.make_batch_names(later_folder))
            container.names.add(f"{later_folder}/{worker.READY_MARKER}")
            self.assertEqual(worker.process_ready_batches(container, "batches"), 0)

        self.assertEqual(
            [call.args[0] for call in run.call_args_list],
            [["2026-11-08"], ["2026-11-09"], ["2026-11-10"]],
        )


if __name__ == "__main__":
    unittest.main()
