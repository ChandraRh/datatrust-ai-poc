"""Incident handling for every source: distinct ADF incidents, signed-in approvals, rule time limit, Re-check now."""
import unittest
from unittest.mock import patch

import resolution as R
import scopes
from adf import insights
from checks import engine

SRC = {"source_id": 3, "name": "f", "config": {}}


class AdfIncidentIds(unittest.TestCase):
    def test_each_failed_check_on_each_table_is_its_own_incident(self):
        a = insights._issue("HIGH", "CHECK_FAILED", SRC, "PL_01", "t", "", "run-1", key="TARGET_TABLE.salesdb.bronze.payments")
        b = insights._issue("HIGH", "CHECK_FAILED", SRC, "PL_01", "t", "", "run-1", key="TARGET_TABLE.salesdb.bronze.refunds")
        c = insights._issue("MEDIUM", "CHECK_FAILED", SRC, "PL_01", "t", "", "run-1", key="SCHEMA_MATCH.salesdb.bronze.payments")
        self.assertEqual(len({a["id"], b["id"], c["id"]}), 3)
        self.assertEqual(a["type"], "CHECK_FAILED")
        self.assertEqual(scopes.scope_of_incident(a["id"]), "adf:3")
        self.assertEqual(len(a["id"].split(":")), 5)                     # adf : source : pipeline : kind : run

    def test_ids_never_hold_a_slash_or_a_colon(self):
        i = insights._issue("HIGH", "CHECK_FAILED", SRC, "PL_01", "t", "", "run-1", key="SOURCE_FILES.landing/batch_1/pay:ments.csv")
        self.assertNotIn("/", i["id"])
        self.assertEqual(len(i["id"].split(":")), 5)

    def test_a_newer_run_with_the_same_check_continues_the_incident(self):
        old = "adf:3:PL_01:CHECK_FAILED.SCHEMA_MATCH.salesdb.bronze.payments:run-1"
        new = "adf:3:PL_01:CHECK_FAILED.SCHEMA_MATCH.salesdb.bronze.payments:run-2"
        other = "adf:3:PL_01:CHECK_FAILED.SCHEMA_MATCH.salesdb.bronze.refunds:run-2"
        self.assertEqual(R._adf(old, [other])[0] != "superseded", True)  # another table is not the same problem
        self.assertEqual(R._adf(old, [new])[:3:2], ("superseded", new))

    def test_a_check_that_passes_again_closes_as_re_checked(self):
        iid = "adf:3:PL_01:CHECK_FAILED.SCHEMA_MATCH.salesdb.bronze.payments:run-1"
        with patch.object(R.appdb, "one", return_value={"status": "PASS", "detail": "Every incoming column exists in the table."}):
            reason, detail, _ = R._adf(iid, [])
        self.assertEqual(reason, "check_passes")
        self.assertIn("passes now", detail)


class Approvals(unittest.TestCase):
    def test_recorded_under_the_signed_in_user_not_a_typed_name(self):
        # never import server.py in a test: it starts the ADF collector, the rule scheduler and the Google Chat alerts
        with patch.object(scopes.ds, "approve") as ap, patch("estate.store.audit") as audit:
            r = scopes.approve("X", "t", ["fix"], " ticket 42 ", "chandra")
        self.assertEqual(r["approved_by"], "chandra")
        self.assertEqual(ap.call_args[0][3:], ("chandra", "ticket 42"))
        self.assertEqual(audit.call_args[0][:2], ("chandra", "REMEDIATION_APPROVED"))
        with self.assertRaises(ValueError):
            scopes.approve("X", "t", [], "", "chandra")


class RuleTimeLimit(unittest.TestCase):
    def test_a_timeout_says_what_to_do(self):
        self.assertTrue(engine._is_timeout(Exception("QueryCanceled: canceling statement due to statement timeout")))
        self.assertTrue(engine._is_timeout(Exception("('HYT00', '[HYT00] Query timeout expired (0)')")))
        self.assertFalse(engine._is_timeout(Exception("relation does not exist")))
        msg = str(engine._too_slow())
        self.assertIn(f"{engine.QUERY_SECONDS} s", msg)
        self.assertIn("last 24 h", msg)

    def test_a_slow_rule_ends_as_could_not_run(self):
        chk = {"connection_id": 1, "check_type": "volume", "schema_name": "s", "table_name": "t", "config": {}}
        with patch.object(engine, "_dialect", side_effect=engine._too_slow()):
            r = engine.run(chk)
        self.assertEqual(r["status"], "ERROR")
        self.assertIn("time limit for rules", r["message"])


class RecheckNow(unittest.TestCase):
    def test_a_rule_incident_runs_its_rule_and_reports_why_it_closed(self):
        with patch("checks.engine.run_and_save", return_value={"status": "PASS", "message": "Every payment_id appears once."}) as run, \
                patch.object(scopes, "incidents", return_value={"open": [], "resolved": []}), \
                patch.object(scopes.appdb, "one", return_value={"resolution": "check_passes", "resolution_detail": "ran again, passes"}):
            r = scopes.recheck("db2:CHECK-7", "chandra")
        run.assert_called_once_with(7, "recheck:chandra")
        self.assertEqual((r["still_open"], r["resolution"], r["verified"]), (False, "Re-checked: passes", True))

    def test_still_open_says_why(self):
        open_ = {"id": "db2:CHECK-7", "title": "dup keys", "summary": "3 payment_id values appear more than once"}
        with patch("checks.engine.run_and_save", return_value={"status": "FAIL", "message": "3 duplicates"}), \
                patch.object(scopes, "incidents", return_value={"open": [open_], "resolved": []}):
            r = scopes.recheck("db2:CHECK-7", "chandra")
        self.assertTrue(r["still_open"])
        self.assertIn("more than once", r["why"])

    def test_an_adf_incident_reads_the_factory_again(self):
        with patch("adf.collector.sync", return_value={"checks": 5}) as sync, \
                patch.object(scopes, "incidents", return_value={"open": [], "resolved": []}), \
                patch.object(scopes.appdb, "one", return_value={}):
            r = scopes.recheck("adf:3:PL_01:CHECK_FAILED.X.y:run-1", "chandra")
        sync.assert_called_once_with(3, "recheck:chandra")
        self.assertIn("Read the factory again", r["did"])


if __name__ == "__main__":
    unittest.main()
