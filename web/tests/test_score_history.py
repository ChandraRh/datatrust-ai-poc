"""Trust score over time (web/score_history.py): a row only when something changed, or once an hour."""
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import score_history as H

NOW = datetime(2026, 11, 24, 12, 0, tzinfo=timezone.utc)
OPEN = {"n": 3, "critical": 1, "high": 0}


class Conn:
    """A database connection that answers the open counts and the last history row, and records inserts."""
    def __init__(self, last, log):
        self.last, self.log = last, log

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        cur = MagicMock()
        if "FROM app.incident_state" in sql:
            cur.fetchone.return_value = OPEN
        elif "FROM app.trust_history" in sql:
            cur.fetchone.return_value = self.last
        else:
            self.log.append(params)
        return cur

    def commit(self):
        pass


def record(last, trust=None):
    log = []
    with patch.object(H.appdb, "connect", side_effect=lambda *a, **k: Conn(last, log)):
        wrote = H.record("db:1", trust or {"score": 74, "label": "At risk"}, now=NOW)
    return wrote, log


def last_row(minutes_ago, **kw):
    return {"recorded_at": NOW - timedelta(minutes=minutes_ago), "score": 74, "label": "At risk",
            "open_incidents": 3, "critical": 1, "high": 0, **kw}


class RecordTests(unittest.TestCase):
    def test_first_row_is_written(self):
        wrote, log = record(None)
        self.assertTrue(wrote)
        self.assertEqual((log[0]["score"], log[0]["open_incidents"], log[0]["critical"]), (74, 3, 1))

    def test_unchanged_within_the_hour_is_skipped(self):
        self.assertEqual(record(last_row(10)), (False, []))

    def test_unchanged_but_an_hour_old_is_written(self):
        self.assertTrue(record(last_row(61))[0])

    def test_a_changed_score_or_count_is_written(self):
        self.assertTrue(record(last_row(1), trust={"score": 100, "label": "Healthy"})[0])
        self.assertTrue(record(last_row(1, open_incidents=5))[0])


class HistoryTests(unittest.TestCase):
    def test_events_opened_and_resolved_in_the_window(self):
        since_in = NOW - timedelta(hours=2)
        # appdb.rows gives timestamps as text, like the real database
        points = [{"recorded_at": str(NOW - timedelta(days=9)), "score": 74, "label": "At risk", "open_incidents": 3, "critical": 1, "high": 0}]
        state = [{"incident_id": "RECON-2026-10-27", "title": "Revenue gap", "severity": "CRITICAL",
                  "first_seen": str(NOW - timedelta(days=9)), "resolved_at": str(since_in), "resolution": "resent_by_source",
                  "resolution_detail": "The source sent 3 corrected payments ..."}]
        with patch.object(H.appdb, "table_exists", return_value=True), \
                patch.object(H.appdb, "rows", side_effect=[points, state]):
            h = H.history("db:1", 168)
        self.assertEqual(len(h["points"]), 1)                               # the row before the window starts the line
        self.assertEqual(h["points"][0]["at"], h["since"])
        self.assertEqual([e["kind"] for e in h["events"]], ["resolved"])    # opened 9 days ago: outside the window
        self.assertEqual((h["events"][0]["reason"], h["events"][0]["verified"]), ("Fixed at the source", True))

    def test_hours_are_bounded(self):
        with patch.object(H.appdb, "table_exists", return_value=False):
            self.assertEqual(H.history("db:1", 0)["hours"], 1)
            self.assertEqual(H.history("db:1", 10 ** 6)["hours"], H.MAX_HOURS)


if __name__ == "__main__":
    unittest.main()
