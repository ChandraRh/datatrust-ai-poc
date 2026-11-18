"""Investigate a rule incident: who changed the rule's table (scopes.who_changed, from the Schema changes log)."""
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

import scopes

NOW = datetime(2026, 11, 18, 12, 0, tzinfo=timezone.utc)
CHK = {"schema_name": "silver", "table_name": "payments", "database": "datatrust_poc", "check_type": "schema"}


def row(at, who, action, obj, sql):
    return {"at": at, "who": who, "action": action, "object": obj, "app": "psql", "sql": sql, "times": 1, "tone": "warn",
            "verb": action.split()[0].lower(), "object_type": "table", "client": "", "datatrust": False}


LOG = [row("2026-11-17 10:02:00+00:00", "ravi", "Changed table", "silver.payments", "ALTER TABLE silver.payments DROP COLUMN currency"),
       row("2026-11-17 09:00:00+00:00", "asha", "Created view", "gold.daily_revenue", "CREATE VIEW gold.daily_revenue AS SELECT * FROM silver.payments"),
       row("2026-11-16 08:00:00+00:00", "ravi", "Created index", "silver.ix_pay_date", "CREATE INDEX ix_pay_date ON silver.payments (business_date)"),
       row("2026-11-01 08:00:00+00:00", "old", "Changed table", "silver.payments", "ALTER TABLE silver.payments ADD COLUMN x int")]


def run(log=LOG, since=None, **out):
    d = {"tracking": True, "unreachable": None, "rows": [dict(r) for r in log], **out}
    with patch("estate.schema_changes.schema_changes", return_value=d) as sc:
        return scopes.who_changed(1, CHK, since, now=NOW), sc


class WhoChangedTests(unittest.TestCase):
    def test_changes_to_the_table_since_it_last_passed(self):
        w, sc = run(since=datetime(2026, 11, 15, 0, 0, tzinfo=timezone.utc))
        self.assertEqual([c["who"] for c in w["changes"]], ["ravi", "ravi"])    # the ALTER and the index on it, not the view
        self.assertIn("ravi changed silver.payments at 2026-11-17 10:02", w["check"]["text"])
        self.assertIn("1 more change", w["check"]["text"])
        self.assertEqual(sc.call_args[0][4], "silver.payments")                 # the log is searched for this table
        self.assertEqual(sc.call_args[0][2], 4)                                  # days back to just before the last pass

    def test_a_view_that_only_reads_the_table_is_not_a_change(self):
        w, _ = run(log=[LOG[1]])
        self.assertEqual(w["changes"], [])
        self.assertIn("Nobody changed the structure of silver.payments in the last 30 days", w["check"]["text"])

    def test_older_than_the_last_pass_is_left_out(self):
        w, _ = run(log=[LOG[3]], since=datetime(2026, 11, 10, tzinfo=timezone.utc))
        self.assertEqual(w["changes"], [])

    def test_tracking_off_and_unreachable_are_said_plainly(self):
        w, _ = run(tracking=False)
        self.assertIsNone(w["check"]["ok"])
        self.assertIn("change tracking is off", w["check"]["text"])
        w, _ = run(unreachable="[08001] cannot connect")
        self.assertIn("not reachable", w["check"]["text"])

    def test_naive_last_pass_time_is_read_as_server_time(self):
        w, _ = run(since=datetime(2026, 11, 17, 9, 0))                          # no zone: this server's clock
        self.assertTrue(w["changes"] or w["check"]["ok"])


class InvestigateRootCauseTests(unittest.TestCase):
    def test_schema_rule_root_cause_names_the_person(self):
        inc = {"id": "db1:CHECK-6", "summary": "Breaking change", "metrics": {"check_id": 6}}
        chk = {**CHK, "check_id": 6, "name": "silver.payments columns unchanged", "connection_id": 1, "description": None,
               "schedule_minutes": 360}
        hist = [{"status": "FAIL", "message": "Breaking change: removed currency.", "observed": 1, "expected": "same columns",
                 "details": {"removed": ["currency"]}, "run_at": "2026-11-17 11:00:00"}]
        s = {"id": 1, "name": "DataTrust POC", "type": "PostgreSQL"}
        with patch("checks.store.get_check", return_value=chk), patch("checks.store.history", return_value=hist), \
                patch.object(scopes, "_downstream", return_value=[]), \
                patch("estate.schema_changes.schema_changes", return_value={"tracking": True, "unreachable": None, "rows": [dict(LOG[0])]}):
            out = scopes._investigate_check(s, inc)
        self.assertIn("Changed by ravi at 2026-11-17 10:02", out["root_cause"])
        self.assertEqual(out["changed_by"]["changes"][0]["who"], "ravi")
        self.assertTrue(any("ravi changed silver.payments" in c["text"] for c in out["checks"]))


if __name__ == "__main__":
    unittest.main()
