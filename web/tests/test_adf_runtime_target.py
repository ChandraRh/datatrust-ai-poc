"""A Copy step whose target table comes from a Lookup: resolved from that run's Lookup output, never looked up as an expression."""
import unittest
from unittest.mock import patch

from adf import probes, resolve

DS = {"name": "DS_Target", "properties": {
    "type": "AzurePostgreSqlTable", "linkedServiceName": {"referenceName": "LS_PG"},
    "parameters": {"p_target_schema": {"type": "string"}, "p_target_table": {"type": "string"}},
    "typeProperties": {"schema": {"value": "@dataset().p_target_schema", "type": "Expression"},
                       "table": {"value": "@dataset().p_target_table", "type": "Expression"}}}}
LS = {"name": "LS_PG", "properties": {"type": "AzurePostgreSql",
                                      "typeProperties": {"server": "pg-sales.postgres.database.azure.com", "database": "salesdb"}}}
PARAMS = {"p_target_schema": {"value": "@activity('LK_Get_File_Config').output.firstRow.target_schema", "type": "Expression"},
          "p_target_table": {"value": "@activity('LK_Get_File_Config').output.firstRow.target_table", "type": "Expression"}}


class RuntimeTargetTests(unittest.TestCase):
    def asset(self, ctx):
        return resolve.dataset_asset(DS["properties"] and DS, PARAMS, ctx, {"LS_PG": LS})

    def test_resolved_from_the_lookup_output_of_the_run(self):
        ctx = {"activity_outputs": {"lk_get_file_config": {"firstRow": {"target_schema": "bronze", "target_table": "payments"}}}}
        a = self.asset(ctx)
        self.assertEqual((a["schema"], a["table"], a["unresolved"]), ("bronze", "payments", []))

    def test_unknown_without_the_output(self):
        a = self.asset({})
        self.assertIn("parameter p_target_table", a["unresolved"])
        self.assertTrue(str(a["table"]).startswith("@activity("))          # kept only to show the expression

    def test_an_expression_is_never_checked_as_a_table_name(self):
        a = self.asset({})
        with patch.object(probes, "find_pg_connection", return_value=(object(), "salesdb", "test")):
            checks = probes.target_checks(a, {}, {}, [], None, False)
        self.assertEqual([(c["check_type"], c["status"]) for c in checks], [("TARGET_TABLE", "UNKNOWN")])
        self.assertIn("not checked", checks[0]["detail"])

    def test_columns_the_copy_step_adds_count_as_incoming(self):
        class Conn:
            def __init__(self): self.n = 0
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def execute(self, sql, params=None):
                self.n += 1
                rows = {1: [{"column_name": "payment_id", "is_nullable": "NO", "column_default": None, "data_type": "text"},
                            {"column_name": "batch_id", "is_nullable": "NO", "column_default": None, "data_type": "text"},
                            {"column_name": "load_ts", "is_nullable": "NO", "column_default": "now()", "data_type": "timestamp"}],
                        2: [{"est": 10}], 3: [{"n": 10}]}[min(self.n, 3)]
                return type("R", (), {"fetchall": lambda s: rows, "fetchone": lambda s: rows[0]})()

        class Src:
            def connect(self, db): return Conn()

        asset = {"name": "salesdb.bronze.payments", "schema": "bronze", "table": "payments", "unresolved": []}
        with patch.object(probes, "find_pg_connection", return_value=(Src(), "salesdb", "test")):
            without = probes.target_checks(asset, {}, {}, ["payment_id"], None, False)
            withadd = probes.target_checks(asset, {}, {}, ["payment_id"], None, False, ["batch_id", "source_file"])
        sm = lambda cs: next(c for c in cs if c["check_type"] == "SCHEMA_MATCH")
        self.assertEqual(sm(without)["status"], "WARN")                    # batch_id is required and not in the file
        self.assertIn("batch_id", sm(without)["detail"])
        self.assertEqual(sm(withadd)["status"], "WARN")                    # source_file is added but the table lacks it
        self.assertNotIn("required", sm(withadd)["detail"])
        self.assertIn("source_file", sm(withadd)["detail"])

    def test_other_runtime_functions_stay_unknown(self):
        with self.assertRaises(resolve.Unresolved):
            resolve.evaluate("item().name", {})
        with self.assertRaises(resolve.Unresolved):
            resolve.evaluate("activity('LK').output.firstRow.x", {"activity_outputs": {}})


if __name__ == "__main__":
    unittest.main()
