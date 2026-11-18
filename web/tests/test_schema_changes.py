"""Schema changes page (estate/schema_changes.py): one source's own database, a period, DataTrust's housekeeping hidden,
repeats grouped, real counts."""
import unittest
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from estate import schema_changes as sc

NOW = datetime(2026, 11, 18, 12, 0, tzinfo=timezone.utc)
LOG = [   # newest first, like the connectors return it
    {"event_time": "2026-11-17 10:00:05+00:00", "username": "ravi", "command_tag": "DROP TABLE", "object_type": "table",
     "schema_name": "silver", "object_identity": "silver.stg_customers", "client_addr": "10.0.0.5", "application": "psql", "query": "DROP TABLE silver.stg_customers"},
    {"event_time": "2026-11-17 09:00:30+00:00", "username": "postgres", "command_tag": "ALTER TABLE", "object_type": "table",
     "schema_name": "portal", "object_identity": "portal.users", "client_addr": "::1/128", "application": "", "query": "ALTER TABLE portal.users ADD COLUMN IF NOT EXISTS x"},
    {"event_time": "2026-11-16 08:00:20+00:00", "username": "asha", "command_tag": "ALTER TABLE", "object_type": "table",
     "schema_name": "gold", "object_identity": "gold.daily_revenue", "client_addr": "", "application": "", "query": "ALTER TABLE gold.daily_revenue ..."},
    {"event_time": "2026-11-16 08:00:10+00:00", "username": "asha", "command_tag": "ALTER TABLE", "object_type": "table",
     "schema_name": "gold", "object_identity": "gold.daily_revenue", "client_addr": "", "application": "", "query": "ALTER TABLE gold.daily_revenue ..."},
    {"event_time": "2026-11-12 07:00:00+00:00", "username": "svc", "command_tag": "CREATE TABLE", "object_type": "table",
     "schema_name": "telemetry", "object_identity": "telemetry.adf_runs", "client_addr": "", "application": "datatrust-adf-connector", "query": "CREATE TABLE ..."},
    {"event_time": "2026-09-01 07:00:00+00:00", "username": "old", "command_tag": "CREATE VIEW", "object_type": "view",
     "schema_name": "gold", "object_identity": "gold.old_view", "client_addr": "", "application": "", "query": "CREATE VIEW ..."},
]


def run(**kw):
    src = MagicMock()
    src.tracking_enabled.return_value = kw.pop("tracking", True)
    src.changes.return_value = [dict(r) for r in LOG]
    conn = {"connection_id": 1, "name": "DataTrust POC", "database": "datatrust_poc", "source_type": kw.pop("source_type", "postgresql")}
    src.failures.return_value = {"jobs": [{"job": "nightly", "step": "load", "error_message": "failed"}]}
    with patch.object(sc.store, "get_connection", return_value=conn), patch.object(sc.service, "source", return_value=src):
        return sc.schema_changes(1, kw.pop("database", "datatrust_poc"), now=NOW, **kw), src


class SchemaChangesTests(unittest.TestCase):
    def test_period_hides_housekeeping_groups_repeats_and_counts(self):
        out, _ = run(days=30)
        self.assertEqual([r["object"] for r in out["rows"]], ["silver.stg_customers", "gold.daily_revenue"])
        self.assertEqual(out["rows"][1]["times"], 2)                          # same minute, same person, same statement
        self.assertEqual(out["hidden_internal"], 2)                           # portal.* and the datatrust-* application
        self.assertEqual(out["counts"], {"changes": 2, "objects": 2, "dropped": 1, "people": 2})
        self.assertEqual((out["rows"][0]["action"], out["rows"][0]["tone"]), ("Dropped table", "bad"))

    def test_datatrust_housekeeping_on_request(self):
        out, _ = run(days=30, show_internal=True)
        self.assertEqual(len(out["rows"]), 4)
        self.assertTrue(any(r["datatrust"] for r in out["rows"]))
        self.assertEqual(out["hidden_internal"], 0)

    def test_a_longer_period_reaches_older_changes(self):
        self.assertIn("gold.old_view", [r["object"] for r in run(days=90)[0]["rows"]])
        with self.assertRaises(ValueError):
            run(days=0)

    def test_only_the_sources_own_database(self):
        with self.assertRaises(ValueError):
            run(database="adf_ui_tmp")

    def test_tracking_off_reads_nothing(self):
        out, src = run(tracking=False)
        self.assertFalse(out["tracking"])
        src.changes.assert_not_called()

    def test_a_source_that_cannot_be_reached_says_so(self):
        src = MagicMock()
        src.tracking_enabled.side_effect = RuntimeError("[08001] Named Pipes Provider: Could not open a connection\nmore")
        conn = {"connection_id": 2, "name": "PC SQL Server", "database": "DataTrustPOC", "source_type": "sqlserver"}
        with patch.object(sc.store, "get_connection", return_value=conn), patch.object(sc.service, "source", return_value=src):
            out = sc.schema_changes(2, "DataTrustPOC", now=NOW)
        self.assertIn("Could not open a connection", out["unreachable"])
        self.assertIsNone(out["tracking"])
        src.changes.assert_not_called()

    def test_failed_agent_jobs_only_for_sql_server(self):
        self.assertEqual(run(source_type="postgresql")[0]["jobs"], [])
        self.assertEqual(len(run(source_type="sqlserver")[0]["jobs"]), 1)

    def test_action_words(self):
        self.assertEqual(sc.action("CREATE VIEW")["label"], "Created view")
        self.assertEqual(sc.action("ALTER TABLE")["tone"], "warn")
        self.assertEqual(sc.action("GRANT")["label"], "Granted")


if __name__ == "__main__":
    unittest.main()
