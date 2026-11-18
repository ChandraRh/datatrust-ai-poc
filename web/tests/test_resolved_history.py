"""Verify → Resolved incidents (scopes.resolved_history) and the "Incident now" column of Audit & approvals (history._result)."""
import unittest
from datetime import datetime
from unittest.mock import patch

from fastapi.testclient import TestClient

import history
import scopes

SCOPES = [{"key": "db:1", "name": "DataTrust POC", "kind": "db", "poc": True},
          {"key": "adf:3", "name": "Sales ADF1 (Dev)", "kind": "adf", "poc": False}]
ROWS = [
    {"incident_id": "RECON-2026-10-08", "scope": "db:1", "title": "Revenue gap on 2026-10-08", "severity": "HIGH",
     "first_seen": datetime(2026, 10, 20, 9, 0), "resolved_at": datetime(2026, 10, 29, 22, 43), "resolution": "fixed_by_datatrust",
     "resolution_detail": "12 rows corrected", "acknowledged_by": None, "approved_by": "admin"},
    {"incident_id": "adf:3:PL_01:failed", "scope": "adf:3", "title": "PL_01 failed", "severity": "CRITICAL",
     "first_seen": datetime(2026, 11, 1, 8, 0), "resolved_at": datetime(2026, 11, 2, 8, 0), "resolution": "unexplained",
     "resolution_detail": None, "acknowledged_by": "ravi", "approved_by": None},
    {"incident_id": "db9:CHECK-4", "scope": "db:9", "title": "Old check", "severity": "LOW",
     "first_seen": datetime(2026, 9, 1), "resolved_at": datetime(2026, 9, 2), "resolution": "check_passes",
     "resolution_detail": "passes", "acknowledged_by": None, "approved_by": None},
]


class ResolvedHistoryTests(unittest.TestCase):
    def run_it(self, **kw):
        with patch.object(scopes, "list_scopes", return_value=SCOPES), \
                patch.object(scopes.appdb, "table_exists", return_value=True), \
                patch.object(scopes.appdb, "rows", return_value=[dict(r) for r in ROWS]) as rows:
            return scopes.resolved_history(**kw), rows

    def test_all_sources_names_each_source_and_counts_verified(self):
        out, rows = self.run_it(all_sources=True)
        sql, args = rows.call_args[0]
        self.assertNotIn("s.scope = %s", sql)                         # all sources: no scope filter
        self.assertIn("'superseded'", sql)                            # continued in a newer run: still open, never listed
        self.assertEqual([r["source"] for r in out["rows"]], ["DataTrust POC", "Sales ADF1 (Dev)", "db:9 (removed)"])
        self.assertEqual(out["counts"], {"verified": 2, "not_verified": 1})
        self.assertEqual(out["rows"][0]["resolution_label"], "Fixed by DataTrust")
        self.assertEqual(out["rows"][1]["resolution_detail"], "Resolved before DataTrust recorded reasons.")

    def test_one_source_and_dates_go_into_the_query(self):
        out, rows = self.run_it(scope="adf:3", date_from="2026-11-01", date_to="2026-11-30")
        sql, args = rows.call_args[0]
        self.assertIn("s.scope = %s", sql)
        self.assertEqual(args, ("adf:3", "2026-11-01", "2026-11-30"))
        self.assertIn("%s::date + 1", sql)                            # the To date is included
        self.assertEqual(out["scope"], "adf:3")

    def test_verified_and_search_filters(self):
        self.assertEqual([r["incident_id"] for r in self.run_it(all_sources=True, verified="no")[0]["rows"]], ["adf:3:PL_01:failed"])
        self.assertEqual([r["incident_id"] for r in self.run_it(all_sources=True, q="revenue")[0]["rows"]], ["RECON-2026-10-08"])
        self.assertEqual([r["incident_id"] for r in self.run_it(all_sources=True, q="sales adf")[0]["rows"]], ["adf:3:PL_01:failed"])
        with self.assertRaises(ValueError):
            self.run_it(all_sources=True, verified="maybe")

    def test_no_state_table_yet_means_an_empty_list(self):
        with patch.object(scopes, "list_scopes", return_value=SCOPES), patch.object(scopes.appdb, "table_exists", return_value=False):
            out = scopes.resolved_history(all_sources=True)
        self.assertEqual(out["rows"], [])


class RoutesTests(unittest.TestCase):
    def test_json_and_csv(self):
        # importing the server must not start its background loops in a test (no Google Chat posts, no syncs, no checks)
        with patch.dict("os.environ", {"ALERTS_ENABLED": "false", "ADF_COLLECTOR_IN_WEB": "false", "CHECKS_IN_WEB": "false"}):
            import server
        c = TestClient(server.app)
        data = {"rows": [{"incident_id": "RECON-2026-10-08", "title": "Revenue gap", "severity": "HIGH", "source": "DataTrust POC",
                          "scope": "db:1", "first_seen": "2026-10-20 09:00:00", "resolved_at": "2026-10-29 22:43:00",
                          "resolution_label": "Fixed by DataTrust", "verified": True, "resolution_detail": "12 rows corrected",
                          "approved_by": "admin", "acknowledged_by": None}],
                "all_sources": True, "scope": None, "truncated": False, "counts": {"verified": 1, "not_verified": 0}}
        with patch.object(scopes, "resolved_history", return_value=data) as fn, \
                patch.object(server.estate_store, "auth_enabled", return_value=True), \
                patch.object(server.estate_store, "read_session", return_value={"username": "viewer1", "role": "viewer"}):
            r = c.get("/api/incidents/resolved?all=true&verified=yes")
            csv = c.get("/api/incidents/resolved.csv?all=true")
        self.assertEqual(r.status_code, 200)                           # any signed-in user, not only admins
        self.assertEqual(fn.call_args_list[0][0], (None, True, None, None, "yes", None))
        lines = csv.text.strip().splitlines()
        self.assertTrue(lines[0].startswith("Resolved at,Source,Incident,Title"))
        self.assertIn(",yes,", lines[1])


class IncidentNowTests(unittest.TestCase):
    def test_result_of_an_approved_fix(self):
        self.assertEqual(history._result(None, None, None)["label"], "Not tracked")
        self.assertEqual(history._result("X", None, None), {"state": "open", "label": "Still open"})
        self.assertEqual(history._result("X", datetime(2026, 11, 1), "superseded")["label"], "Still failing in a newer run")
        done = history._result("X", datetime(2026, 11, 1, 12, 0), "fixed_by_datatrust")
        self.assertEqual((done["state"], done["label"], done["at"]), ("verified", "Fixed by DataTrust", "2026-11-01 12:00:00"))
        self.assertEqual(history._result("X", datetime(2026, 11, 1), "unexplained")["state"], "unverified")


if __name__ == "__main__":
    unittest.main()
