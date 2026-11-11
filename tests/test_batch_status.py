import unittest
from unittest.mock import patch

import batch_status


class Result:
    def __init__(self, row):
        self.row = row

    def fetchone(self):
        return self.row


class Connection:
    def __init__(self, results):
        self.results = iter(results)
        self.queries = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def execute(self, query, params=()):
        self.queries.append((query, params))
        return Result(next(self.results))


class BatchStatusTests(unittest.TestCase):
    def test_connection_uses_configured_postgres_sslmode(self):
        with patch.object(batch_status.config, "PG_SSLMODE", "require"), \
             patch.object(batch_status.psycopg, "connect") as connect:
            batch_status.connect()

        self.assertEqual(connect.call_args.kwargs["sslmode"], "require")

    def test_retry_transitions_only_failed_batches_to_ready(self):
        row = {"batch_id": 7, "batch_folder": "batches/2026-11-09",
               "batch_name": "2026-11-09", "status": "READY"}
        connection = Connection([row])

        with patch.object(batch_status, "connect", return_value=connection):
            result = batch_status.request_retry(7, "admin")

        self.assertEqual(result, row)
        query, params = connection.queries[0]
        self.assertIn("status = 'FAILED'", query)
        self.assertEqual(params, ("admin", 7))

    def test_retry_rejects_running_batch(self):
        connection = Connection([None])
        running = {"batch_id": 7, "status": "RUNNING"}
        lookup = Connection([running])

        with patch.object(batch_status, "connect", side_effect=[connection, lookup]):
            with self.assertRaisesRegex(ValueError, "Only failed batches can be retried"):
                batch_status.request_retry(7, "admin")

if __name__ == "__main__":
    unittest.main()
