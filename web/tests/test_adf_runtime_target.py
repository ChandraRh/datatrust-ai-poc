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

    def test_other_runtime_functions_stay_unknown(self):
        with self.assertRaises(resolve.Unresolved):
            resolve.evaluate("item().name", {})
        with self.assertRaises(resolve.Unresolved):
            resolve.evaluate("activity('LK').output.firstRow.x", {"activity_outputs": {}})


if __name__ == "__main__":
    unittest.main()
