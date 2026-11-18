"""Retention (web/retention.py): what grows with every run is removed after RETENTION_DAYS; the audit trail stays."""
import os
import unittest
from datetime import datetime
from unittest.mock import MagicMock, patch

import retention

NOW = datetime(2026, 11, 18, 12, 0)


class Conn:
    """Stands in for a database connection: records each statement, answers a count of 2."""
    def __init__(self, log):
        self.log = log

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self.log.append((sql, params))
        cur = MagicMock()
        cur.rowcount = 2
        cur.fetchone.return_value = {"n": 2}
        return cur

    def commit(self):
        self.log.append(("COMMIT", None))


def run(**kw):
    log = []
    with patch.object(retention.appdb, "table_exists", side_effect=kw.pop("exists", lambda t: True)), \
            patch.object(retention.appdb, "connect", side_effect=lambda *a, **k: Conn(log)), \
            patch("estate.store.audit") as audit:
        out = retention.prune(now=NOW, **kw)
    return out, log, audit


class RetentionTests(unittest.TestCase):
    def test_removes_old_history_and_keeps_the_latest(self):
        out, log, audit = run(keep_days=90)
        deletes = [s for s, _ in log if s.startswith("DELETE")]
        self.assertEqual(len(deletes), len(retention.STEPS))
        self.assertEqual(log[0][1]["cut"], datetime(2026, 8, 20, 12, 0))                  # 90 days before now
        self.assertIn("max(result_id)", deletes[0])                                       # each rule keeps its latest result
        self.assertIn("DISTINCT ON (source_id, pipeline_name)", deletes[1])               # each pipeline keeps its latest run
        self.assertEqual(sum(1 for s, _ in log if s == "COMMIT"), len(retention.STEPS))
        audit.assert_called_once()
        self.assertEqual(audit.call_args[0][1], "RETENTION")

    def test_the_audit_trail_is_never_touched(self):
        tables = {t for t, _ in retention.STEPS}
        for kept in ("portal.audit_log", "portal.access_requests", "app.remediation_log", "app.incident_state", "app.alert_log",
                     "monitor.checks", "portal.users", "portal.connections"):
            self.assertNotIn(kept, tables)

    def test_dry_run_only_counts(self):
        out, log, audit = run(keep_days=90, dry_run=True)
        self.assertTrue(all(s.startswith("SELECT count(*)") for s, _ in log))
        audit.assert_not_called()
        self.assertEqual(set(out.values()), {2})

    def test_zero_keeps_everything_and_missing_tables_are_skipped(self):
        out, log, _ = run(keep_days=0)
        self.assertEqual((out, log), ({}, []))
        out, log, _ = run(keep_days=30, exists=lambda t: t == "monitor.results")
        self.assertEqual(list(out), ["monitor.results"])

    def test_sync_log_keeps_30_days_at_most(self):
        _, log, _ = run(keep_days=365)
        self.assertEqual(log[0][1]["sync_cut"], datetime(2026, 10, 19, 12, 0))

    def test_switches(self):
        with patch.dict(os.environ, {"RETENTION_DAYS": "0"}):
            self.assertFalse(retention.start_background())
        with patch.dict(os.environ, {"RETENTION_DAYS": "90", "RETENTION_IN_WEB": "false"}):
            self.assertFalse(retention.start_background())
        with patch.dict(os.environ, {"RETENTION_DAYS": "abc"}):
            self.assertEqual(retention.days(), 90)


if __name__ == "__main__":
    unittest.main()
