"""Data quality pages for sources other than the POC: rows come from the source's own findings, rules from its own settings."""
import unittest
from unittest.mock import patch

import quality

DB = {"key": "db:5", "kind": "db", "id": 5, "name": "Sales DB", "type": "PostgreSQL", "poc": False}


def chk(cid, ctype, status, details=None, config=None, table="orders"):
    return {"check_id": cid, "name": f"check {cid}", "check_type": ctype, "connection_id": 5, "database": None, "schema_name": "sales",
            "table_name": table, "config": config or {}, "severity": "HIGH", "last_status": status, "last_run_at": "2026-11-13",
            "observed": 3, "expected": "x", "message": "m", "details": details or {}, "run_at": "2026-11-13"}


class QualityTests(unittest.TestCase):
    def test_database_rows_come_from_its_failing_checks(self):
        cs = [chk(1, "uniqueness", "FAIL", {"duplicates": [{"order_id": "A", "copies": 3}]}, {"columns": ["order_id"]}),
              chk(2, "custom_sql", "FAIL", {"rows": [{"refund_id": "R1", "amount": 9000}]}, {"sql": "SELECT 1", "mode": "no_rows"}),
              chk(3, "freshness", "FAIL", {}, {"column": "loaded_at"}),
              chk(4, "uniqueness", "PASS", {}, {"columns": ["id"]})]
        with patch.object(quality.scopes, "resolve", return_value=DB), patch.object(quality.scopes, "_checks_of", return_value=cs):
            r = quality.rows("db:5")
        self.assertEqual([g["check_id"] for g in r["groups"]], [1, 2])
        self.assertEqual(r["groups"][0]["rows"][0]["problem"], "appears 3 times")
        self.assertEqual([t["check_id"] for t in r["table_level"]], [3])          # about the whole table: no rows to list

    def test_database_rules_are_its_checks(self):
        cs = [chk(1, "null_rate", "FAIL", config={"column": "email", "max_null_pct": 1}), chk(2, "schema", "PASS")]
        with patch.object(quality.scopes, "resolve", return_value=DB), patch.object(quality.scopes, "_checks_of", return_value=cs),                 patch.object(quality.appdb, "rows", return_value=[{"check_id": 1, "status": "PASS"}, {"check_id": 1, "status": "FAIL"}]):
            r = quality.rules("db:5")
        self.assertEqual(r["rules"][0]["rule"], "email at most 1% empty")
        self.assertEqual(r["rules"][0]["history"], ["PASS", "FAIL"])                  # last runs, oldest first
        self.assertEqual(r["scorecard"], [{"applies_to": "sales.orders", "rules": 2, "passing": 1, "failing": 1, "warnings": 0, "errors": 0}])

    def test_poc_keeps_its_own_pages_and_lists_its_checks(self):
        poc = {**DB, "poc": True}
        cs = [chk(1, "volume", "PASS", config={"min_rows": 1, "dedupe": "poc"})]
        with patch.object(quality.scopes, "resolve", return_value=poc), patch.object(quality.scopes, "_checks_of", return_value=cs),                 patch.object(quality.appdb, "rows", return_value=[]):
            self.assertEqual(quality.rows("db:1")["kind"], "poc")
            r = quality.rules("db:1")
        self.assertEqual(r["kind"], "poc")
        self.assertTrue(r["rules"][0]["repeats_pipeline"])

    def test_factory_copy_settings_become_advice(self):
        pipe = {"properties": {"activities": [{"name": "CP", "type": "Copy", "typeProperties": {
            "translator": {"type": "Expression", "value": "@json(activity('LK').output.firstRow.column_mapping)"}}}]}}
        with patch.object(quality.appdb, "rows", side_effect=[[], [{"name": "PL", "body": pipe}]]):
            r = quality._adf_rules({"id": 3})
        st = r["steps"][0]
        self.assertFalse(st["skip_incompatible_rows"])
        self.assertEqual(st["mapping_at_runtime"], "@json({column_mapping from LK})")
        self.assertTrue(any("one bad row stops the whole copy" in a for a in st["advice"]))
        self.assertTrue(any("decided at run time" in a for a in st["advice"]))


if __name__ == "__main__":
    unittest.main()
