"""One source at a time: scores and incidents of a database come only from its own checks; ids say which source they belong to."""
import unittest
from unittest.mock import patch

import scopes


def check(cid, status, severity="HIGH", ctype="uniqueness", dedupe=None, table="orders"):
    return {"check_id": cid, "name": f"check {cid}", "check_type": ctype, "connection_id": 5, "database": None, "schema_name": "sales",
            "table_name": table, "config": {"dedupe": dedupe} if dedupe else {}, "severity": severity, "enabled": True,
            "last_status": status, "last_run_at": "2026-11-13 03:00:00", "description": None, "observed": 3, "expected": "unique",
            "message": f"msg {cid}", "details": {}, "run_at": "2026-11-13 03:00:00"}


DB = {"key": "db:5", "kind": "db", "id": 5, "name": "Sales DB", "type": "PostgreSQL", "poc": False}
POC = {"key": "db:1", "kind": "db", "id": 1, "name": "POC", "type": "PostgreSQL", "poc": True}


class ScopeTests(unittest.TestCase):
    def test_database_score_comes_only_from_its_checks(self):
        cs = [check(1, "FAIL", "HIGH"), check(2, "PASS"), check(3, "ERROR"), check(4, "WARN", "LOW", "schema"), check(5, None)]
        with patch.object(scopes, "resolve", return_value=DB), patch.object(scopes, "_checks_of", return_value=cs), \
                patch.object(scopes, "_check_trend", return_value=[]):
            o = scopes.overview("db:5")
        t = o["trust"]
        self.assertEqual(t["score"], 100 - 25 - 5 - 2)                 # HIGH fail, error, warning; the pass and the never-run cost nothing
        self.assertEqual(t["label"], "At risk")                         # 68 is below 75
        self.assertEqual(len(t["reasons"]), 3)
        self.assertEqual({k["label"]: k["value"] for k in o["kpis"]}["Failing"], 1)

    def test_no_checks_means_no_score_not_a_perfect_one(self):
        with patch.object(scopes, "resolve", return_value=DB), patch.object(scopes, "_checks_of", return_value=[]), \
                patch.object(scopes, "_check_trend", return_value=[]):
            o = scopes.overview("db:5")
        self.assertIsNone(o["trust"]["score"])
        self.assertEqual(o["trust"]["label"], "No data yet")
        self.assertTrue(o["empty"])

    def test_check_incidents_are_tagged_with_their_source(self):
        cs = [check(7, "FAIL", "CRITICAL"), check(8, "WARN"), check(9, "PASS")]
        with patch.object(scopes, "_checks_of", return_value=cs):
            found = scopes._incidents(DB)
        self.assertEqual([i["id"] for i in found], ["db5:CHECK-7", "db5:CHECK-8"])
        self.assertEqual([i["severity"] for i in found], ["CRITICAL", "LOW"])          # a warning is never more than LOW
        self.assertEqual(scopes.scope_of_incident("db5:CHECK-7"), "db:5")
        self.assertEqual(scopes.scope_of_incident("adf:3:PL_X:RUN_FAILED:abc"), "adf:3")

    def test_poc_keeps_its_pipeline_findings_and_skips_duplicate_checks(self):
        pipeline = [{"id": "RECON-2026-10-27", "severity": "HIGH", "title": "gap", "summary": "", "type": "SETTLEMENT_MISMATCH"}]
        with patch.object(scopes, "_checks_of", side_effect=lambda cid, skip_dedupe: [check(1, "FAIL")] if skip_dedupe else []) as co, \
                patch.object(scopes.ds, "incidents", return_value=pipeline):
            found = scopes._incidents(POC)
        co.assert_called_with(1, skip_dedupe=True)
        self.assertEqual([i["id"] for i in found], ["RECON-2026-10-27", "db1:CHECK-1"])
        with patch.object(scopes.appdb, "rows", return_value=[{**check(2, "FAIL"), "config": {"dedupe": "poc"}}]), \
                patch.object(scopes.appdb, "table_exists", return_value=True):
            self.assertEqual(scopes._checks_of(1, skip_dedupe=True), [])                 # the gateway reconciliation is not counted twice

    def test_runtime_expressions_are_readable(self):
        self.assertEqual(scopes._pretty("landing/@pipeline().parameters.p_batch_id/x.csv"), "landing/{p_batch_id}/x.csv")
        self.assertEqual(scopes._pretty("db.@activity('LK').output.firstRow.tbl"), "db.{tbl from LK}")
        self.assertEqual(scopes._pretty("@{item().name}"), "{each name}")


if __name__ == "__main__":
    unittest.main()
