"""A rule belongs to one source: the connection's own database (checks/store.own_database, used by validate)."""
import unittest
from unittest.mock import patch

from checks import store
from estate import store as estate_store

CONNS = {1: {"connection_id": 1, "database": "datatrust_poc"}, 2: {"connection_id": 2, "database": "salesdb"},
         3: {"connection_id": 3, "database": None}}


def conn(cid):
    return CONNS[cid]


class OwnDatabaseTests(unittest.TestCase):
    def setUp(self):
        p = patch.object(estate_store, "get_connection", side_effect=conn)
        p.start(); self.addCleanup(p.stop)

    def test_the_connections_own_database_is_used_when_none_is_given(self):
        self.assertEqual(store.own_database(1, ""), "datatrust_poc")
        self.assertEqual(store.own_database(1, "DATATRUST_POC"), "DATATRUST_POC")

    def test_another_database_on_the_same_server_is_refused(self):
        with self.assertRaises(ValueError) as e:
            store.own_database(1, "adf_ui_tmp")
        self.assertIn("datatrust_poc", str(e.exception))
        self.assertIn("own connection", str(e.exception))

    def test_a_connection_without_a_database_accepts_any(self):
        self.assertEqual(store.own_database(3, "master"), "master")
        self.assertIsNone(store.own_database(3, ""))

    def test_validate_refuses_a_table_rule_on_another_database(self):
        body = {"name": "fresh", "check_type": "freshness", "connection_id": 1, "database": "adf_ui_tmp",
                "schema_name": "audit", "table_name": "error_log", "config": {"column": "logged_at"}}
        with self.assertRaises(ValueError):
            store.validate(body)
        self.assertEqual(store.validate({**body, "database": ""})["database"], "datatrust_poc")

    def test_reconciliation_first_query_is_held_to_its_source_the_second_may_read_any_database(self):
        body = {"name": "totals", "check_type": "reconciliation", "config": {
            "left": {"connection_id": 1, "database": "", "sql": "SELECT 1, 1"},
            "right": {"connection_id": 2, "database": "other_db", "sql": "SELECT 1, 1"}}}
        out = store.validate(body)
        self.assertEqual(out["config"]["left"]["database"], "datatrust_poc")
        self.assertEqual(out["config"]["right"]["database"], "other_db")
        body["config"]["left"]["database"] = "adf_ui_tmp"
        with self.assertRaises(ValueError):
            store.validate(body)


if __name__ == "__main__":
    unittest.main()
