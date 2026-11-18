"""Access page, "Who has access" (estate/access_review.py): read-only review with risks, and the Ask tools built on it."""
import unittest
from unittest.mock import MagicMock, patch

import ask
import scopes
from estate import access_review as ar

ROLES = [
    {"name": "postgres", "can_login": True, "superuser": True, "member_of": [], "valid_until": None},
    {"name": "etl_user", "can_login": True, "superuser": False, "member_of": ["loaders"], "valid_until": None},
    {"name": "report_bot", "can_login": True, "superuser": False, "member_of": [], "valid_until": "2027-01-01"},
    {"name": "loaders", "can_login": False, "superuser": False, "member_of": [], "valid_until": None},
]
ACCESS = {
    "postgres": {"objects": [], "schemas": [], "owns": [{"schema": "gold", "object": "daily_revenue"}], "database": {"connect": True}},
    "etl_user": {"objects": [{"schema": "silver", "object": "payments", "privileges": "INSERT, SELECT", "via_public": False},
                             {"schema": "gold", "object": "daily_revenue", "privileges": "UPDATE", "via_public": False},
                             {"schema": "audit", "object": "error_log", "privileges": "SELECT", "via_public": True}],
                 "schemas": [], "owns": [], "database": {"connect": True}},
    "report_bot": {"objects": [{"schema": "gold", "object": "daily_revenue", "privileges": "SELECT", "via_public": False}],
                   "schemas": [], "owns": [], "database": {"connect": True}},
    "loaders": {"objects": [], "schemas": [], "owns": [], "database": {"connect": False}},
}


def review(source_type="postgresql", roles=ROLES):
    src = MagicMock()
    src.roles.return_value = [dict(r) for r in roles]
    src.role_access.side_effect = lambda db, role: ACCESS.get(role, {"objects": [], "schemas": [], "owns": [], "database": {}})
    conn = {"connection_id": 1, "name": "DataTrust POC", "database": "datatrust_poc", "source_type": source_type}
    with patch.object(ar.store, "get_connection", return_value=conn), patch.object(ar.service, "source", return_value=src):
        return ar.review(1, "datatrust_poc"), src


class AccessReviewTests(unittest.TestCase):
    def test_reads_writes_and_risks(self):
        out, src = review()
        people = {p["name"]: p for p in out["people"]}
        self.assertEqual(people["postgres"]["risks"], ["superuser"])                 # a superuser is flagged once
        self.assertEqual(people["etl_user"]["writes"], ["gold", "silver"])
        self.assertIn("writes_gold", people["etl_user"]["risks"])
        self.assertIn("no_expiry", people["etl_user"]["risks"])
        self.assertEqual(people["report_bot"]["reads"], ["gold"])
        self.assertEqual(people["report_bot"]["risks"], [])                         # has an expiry date, reads only
        self.assertEqual(people["loaders"]["kind"], "group role")
        self.assertEqual(out["public"], [{"schema": "audit", "privileges": ["SELECT"]}])
        self.assertEqual(out["counts"], {"logins": 3, "superusers": 1, "write_gold": 1, "public_schemas": 1, "no_expiry": 1})
        self.assertEqual(out["people"][0]["name"], "postgres")                       # risks first
        src.apply.assert_not_called()                                              # read-only: nothing is changed

    def test_sql_server_fixed_roles_count_as_rights_on_every_schema(self):
        roles = [{"name": "app", "can_login": True, "superuser": False, "member_of": ["db_datawriter"]},
                 {"name": "boss", "can_login": True, "superuser": False, "member_of": ["db_owner"]}]
        people = {p["name"]: p for p in review("sqlserver", roles)[0]["people"]}
        self.assertEqual((people["app"]["writes"], people["app"]["risks"]), (["(all)"], ["writes_gold"]))
        self.assertTrue(people["boss"]["superuser"])
        self.assertNotIn("no_expiry", people["app"]["risks"])                     # SQL Server has no expiry to read

    def test_postgres_predefined_data_roles(self):
        roles = [{"name": "azure_pg_admin", "can_login": False, "superuser": False, "member_of": ["pg_read_all_data", "pg_write_all_data"]}]
        p = review("postgresql", roles)[0]["people"][0]
        self.assertEqual((p["reads"], p["writes"], p["risks"]), (["(all)"], ["(all)"], ["writes_gold"]))

    def test_only_the_sources_own_database(self):
        conn = {"connection_id": 1, "name": "POC", "database": "datatrust_poc", "source_type": "postgresql"}
        with patch.object(ar.store, "get_connection", return_value=conn), patch.object(ar.service, "source"):
            with self.assertRaises(ValueError):
                ar.review(1, "adf_ui_tmp")


class AskToolsTests(unittest.TestCase):
    SC = {"key": "db:1", "kind": "db", "id": 1, "name": "DataTrust POC", "type": "PostgreSQL", "poc": True}

    def test_the_two_estate_questions_are_ask_tools(self):
        names = {t[0] for t in ask.TOOLS}
        self.assertTrue({"get_schema_changes", "get_access"} <= names)

    def test_get_access_and_get_schema_changes(self):
        src = MagicMock(); src.cfg = {"database": "datatrust_poc"}
        with patch.object(scopes, "resolve", return_value=self.SC), patch("estate.service.source", return_value=src), \
                patch("estate.access_review.review", return_value={"counts": {"superusers": 1}, "public": [], "people": [{"name": "postgres", "risks": ["superuser"]}]}), \
                patch("estate.schema_changes.schema_changes", return_value={"tracking": True, "days": 7, "counts": {"changes": 1},
                      "rows": [{"at": "2026-11-17 10:00", "who": "ravi", "action": "Dropped table", "object": "silver.x", "times": 1, "app": ""}]}):
            acc = ask._run("get_access", {}, "db:1")
            chg = ask._run("get_schema_changes", {"days": 7, "table": "silver.x"}, "db:1")
        self.assertEqual(acc["logins_and_roles"][0]["risks"], ["superuser"])
        self.assertEqual(chg["changes"][0]["action"], "Dropped table")

    def test_schema_changes_with_tracking_off_says_so(self):
        src = MagicMock(); src.cfg = {"database": "salesdb"}
        with patch.object(scopes, "resolve", return_value=self.SC), patch("estate.service.source", return_value=src), \
                patch("estate.schema_changes.schema_changes", return_value={"tracking": False, "unreachable": None}):
            out = ask._run("get_schema_changes", {}, "db:3")
        self.assertIn("Change tracking is off", out["note"])


if __name__ == "__main__":
    unittest.main()
