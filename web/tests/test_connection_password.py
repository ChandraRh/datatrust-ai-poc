import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from estate import routes as estate_routes
from estate import store


def fake_db(rows):
    conn = MagicMock()
    conn.__enter__.return_value = conn
    conn.execute.return_value.fetchall.return_value = rows
    return lambda: conn


class KeepPasswordTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.root = Path(self.dir.name)
        (self.root / ".env").write_text("PG_PASSWORD=app-db-secret\n", encoding="utf-8")

    def tearDown(self):
        self.dir.cleanup()

    def keep(self, rows, *args):
        with patch.object(store, "ROOT", self.root), patch.object(store, "db", fake_db(rows)):
            env = store.keep_password(*args)
            return env, store.resolve_password(env)

    def test_new_source_gets_its_own_variable_and_the_password_reads_back(self):
        tricky = "p@ss w'rd \"#$x=1"
        env, back = self.keep([], "Finance PostgreSQL", tricky, "", None)
        self.assertEqual(env, "CONN_FINANCE_POSTGRESQL_PASSWORD")
        self.assertEqual(back, tricky)

    def test_name_taken_by_another_source_gets_a_number(self):
        env, _ = self.keep([{"connection_id": 1, "password_env": "CONN_SALES_PASSWORD"}], "Sales", "x", "", None)
        self.assertEqual(env, "CONN_SALES_2_PASSWORD")

    def test_edit_reuses_its_own_variable(self):
        rows = [{"connection_id": 4, "password_env": "CONN_OLD_NAME_PASSWORD"}]
        env, back = self.keep(rows, "New name", "new-pw", "CONN_OLD_NAME_PASSWORD", 4)
        self.assertEqual((env, back), ("CONN_OLD_NAME_PASSWORD", "new-pw"))

    def test_edit_never_overwrites_the_apps_own_or_a_shared_variable(self):
        rows = [{"connection_id": 1, "password_env": "PG_PASSWORD"}, {"connection_id": 2, "password_env": "PG_PASSWORD"}]
        env, back = self.keep(rows, "POC", "other-pw", "PG_PASSWORD", 1)
        self.assertEqual((env, back), ("CONN_POC_PASSWORD", "other-pw"))
        self.assertIn("PG_PASSWORD=app-db-secret", (self.root / ".env").read_text(encoding="utf-8"))

    def test_a_typed_password_is_used_before_the_saved_one(self):
        self.assertEqual(store.password_of({"password": "typed", "password_env": "NOT_SET_ANYWHERE"}), "typed")


class ConnectionRouteTests(unittest.TestCase):
    def client(self):
        from server import app
        app.dependency_overrides[estate_routes.admin_user] = lambda: {"username": "admin", "role": "admin"}
        self.addCleanup(app.dependency_overrides.clear)
        return TestClient(app)

    BODY = {"name": "Finance", "source_type": "postgresql", "host": "h", "port": 5432, "database": "d", "username": "u"}

    def test_add_stores_only_the_variable_name(self):
        with patch.object(store, "keep_password", return_value="CONN_FINANCE_PASSWORD") as keep, \
                patch.object(store, "add_connection", return_value=7) as add, \
                patch.object(estate_routes.service, "overview", return_value={"ok": True}):
            r = self.client().post("/api/estate/connections", json={**self.BODY, "password": "s3cret"})
        self.assertEqual(r.status_code, 200)
        keep.assert_called_once_with("Finance", "s3cret", "", None)
        self.assertNotIn("s3cret", repr(add.call_args))
        self.assertIn("CONN_FINANCE_PASSWORD", add.call_args.args)

    def test_edit_with_an_empty_password_keeps_the_saved_one(self):
        with patch.object(store, "get_connection", return_value={"password_env": "CONN_FINANCE_PASSWORD"}), \
                patch.object(store, "keep_password") as keep, \
                patch.object(store, "update_connection") as upd, \
                patch.object(estate_routes.service, "overview", return_value={"ok": True}):
            r = self.client().put("/api/estate/connections/7", json={**self.BODY, "password": ""})
        self.assertEqual(r.status_code, 200)
        keep.assert_not_called()
        self.assertIn("CONN_FINANCE_PASSWORD", upd.call_args.args)

    def test_test_button_uses_the_typed_password(self):
        with patch("estate.service.test_connection", return_value={"version": "16"}) as tc:
            r = self.client().post("/api/estate/connections/test", json={**self.BODY, "password": "typed"})
        self.assertTrue(r.json()["ok"])
        self.assertEqual(tc.call_args.args[0]["password"], "typed")


if __name__ == "__main__":
    unittest.main()
