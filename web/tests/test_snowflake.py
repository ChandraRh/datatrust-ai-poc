"""Snowflake connector (estate/snowflake_connector.py) and Snowflake rules (checks/dialects.py, checks/engine.py),
run against a stand-in driver that records the SQL and answers like Snowflake does (SHOW keys in lower case,
unquoted aliases in UPPER case)."""
import os
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

from checks import dialects, engine
from estate import snowflake_connector as sf
from estate.snowflake_connector import SnowflakeSource


class FakeCursor:
    def __init__(self, con):
        self.con, self.description, self.rows = con, None, []

    def execute(self, sql, params=None):
        self.con.sent.append((" ".join(sql.split()), params))
        answer = self.con.answer(sql)
        if isinstance(answer, Exception):
            raise answer
        self.rows = answer
        self.description = [("x",)] if answer is not None else None
        return self

    def fetchall(self):
        return list(self.rows or [])

    def fetchmany(self, n):
        return list(self.rows or [])[:n]


class FakeConnection:
    def __init__(self, answer, kwargs):
        self.answer, self.kwargs, self.sent = answer, kwargs, []
        self.rolled_back = self.closed = False
        self.autocommit_set = None

    def cursor(self, kind=None):
        return FakeCursor(self)

    def autocommit(self, on):
        self.autocommit_set = on

    def rollback(self):
        self.rolled_back = True

    def close(self):
        self.closed = True


class FakeDriver:
    """Installs itself as snowflake.connector; answer(sql) -> rows, None (no result set) or an exception."""

    def __init__(self, answer):
        self.answer, self.connections = answer, []
        self.connector = types.ModuleType("snowflake.connector")
        self.connector.DictCursor = object
        self.connector.connect = self._connect
        self.package = types.ModuleType("snowflake")
        self.package.connector = self.connector

    def _connect(self, **kwargs):
        c = FakeConnection(self.answer, kwargs)
        self.connections.append(c)
        return c

    def __enter__(self):
        self._patch = patch.dict(sys.modules, {"snowflake": self.package, "snowflake.connector": self.connector})
        self._patch.start()
        return self

    def __exit__(self, *exc):
        self._patch.stop()

    def sent(self):
        return [s for c in self.connections for s, _ in c.sent]


CFG = {"connection_id": 7, "source_type": "snowflake", "host": "https://myorg-acct.snowflakecomputing.com/", "port": 443,
       "database": "SALES_DW", "username": "DATATRUST_SVC", "password_env": "SF_PW",
       "settings": {"warehouse": "COMPUTE_WH", "role": "DATATRUST_READ"}}


def src(cfg=None):
    return SnowflakeSource(dict(cfg or CFG))


class HelperTests(unittest.TestCase):
    def test_the_account_is_taken_from_whatever_people_paste(self):
        for pasted in ("myorg-acct", "myorg-acct.snowflakecomputing.com", "https://myorg-acct.snowflakecomputing.com/console"):
            self.assertEqual(sf.account_of(pasted), "myorg-acct")

    def test_upper_case_aliases_are_lowered_but_quoted_mixed_case_kept(self):
        self.assertEqual(sf.lower_keys({"N": 3, "DUP_KEYS": 1, "CustomerId": 5}), {"n": 3, "dup_keys": 1, "CustomerId": 5})

    def test_identifiers_are_quoted_exactly(self):
        self.assertEqual(sf.qn('ORDERS'), '"ORDERS"')
        self.assertEqual(sf.qn('we"ird'), '"we""ird"')
        self.assertEqual(sf.lit("it's 100%"), "'it''s 100%'")


class DialectTests(unittest.TestCase):
    def test_snowflake_rules_have_their_own_sql(self):
        d = dialects.for_type("snowflake")
        self.assertEqual(d.table("SALES", "ORDERS"), '"SALES"."ORDERS"')
        self.assertEqual(d.limit("WITH x AS (SELECT 1) SELECT * FROM x", 20), "SELECT * FROM (WITH x AS (SELECT 1) SELECT * FROM x) AS dt_q LIMIT 20")
        self.assertEqual(d.since("LOADED_AT", 24), '"LOADED_AT" >= DATEADD(hour, -24, CURRENT_TIMESTAMP())')

    def test_a_snowflake_timeout_counts_as_too_slow(self):
        self.assertTrue(engine._is_timeout(Exception("000630 (57014): Statement reached its statement or warehouse timeout of 60 second(s)")))


class LoginTests(unittest.TestCase):
    def test_password_login(self):
        with patch.object(sf.store, "resolve_password", return_value="s3cret"):
            a = src().login_args()
        self.assertEqual((a["account"], a["user"], a["password"], a["warehouse"], a["role"]),
                         ("myorg-acct", "DATATRUST_SVC", "s3cret", "COMPUTE_WH", "DATATRUST_READ"))
        self.assertNotIn("private_key_file", a)

    def test_key_pair_login_without_a_passphrase(self):
        with tempfile.NamedTemporaryFile("w", suffix=".p8", delete=False) as f:
            f.write("-----BEGIN PRIVATE KEY-----\n")
        try:
            cfg = {**CFG, "settings": {**CFG["settings"], "private_key_file": f.name}}
            with patch.object(sf.store, "resolve_password", side_effect=ValueError("SF_PW is not set")):
                a = src(cfg).login_args()
            self.assertEqual((a["authenticator"], a["private_key_file"]), ("SNOWFLAKE_JWT", f.name))
            self.assertNotIn("password", a)
            self.assertNotIn("private_key_password", a)
        finally:
            os.unlink(f.name)

    def test_a_missing_key_file_says_so(self):
        cfg = {**CFG, "settings": {**CFG["settings"], "private_key_file": "C:/nowhere/key.p8"}}
        with self.assertRaisesRegex(ValueError, "private key file not found"):
            src(cfg).login_args()

    def test_every_session_is_tagged_and_time_limited(self):
        with FakeDriver(lambda sql: [{"VERSION": "9.1.0", "LOGIN_USER": "DATATRUST_SVC", "DATABASE": "SALES_DW",
                                      "ROLE": "DATATRUST_READ", "WAREHOUSE": "COMPUTE_WH"}]) as drv, \
                patch.object(sf.store, "resolve_password", return_value="pw"):
            r = src().test()
        self.assertEqual(r["version"], "Snowflake 9.1.0")
        self.assertEqual(drv.connections[0].kwargs["session_parameters"], {"QUERY_TAG": "DataTrust", "STATEMENT_TIMEOUT_IN_SECONDS": 30})
        self.assertTrue(drv.connections[0].closed)


SHOW = {
    "SHOW DATABASES": [{"name": "SALES_DW", "owner": "SYSADMIN", "kind": "STANDARD"}, {"name": "SNOWFLAKE", "owner": "", "kind": "APPLICATION"}],
    'SHOW SCHEMAS IN DATABASE "SALES_DW"': [{"name": "INFORMATION_SCHEMA"}, {"name": "RAW", "owner": "SYSADMIN"}, {"name": "MART", "owner": "SYSADMIN"}],
    'SHOW TABLES IN DATABASE "SALES_DW"': [{"name": "ORDERS", "schema_name": "RAW"}, {"name": "CUSTOMERS", "schema_name": "RAW"}],
    'SHOW VIEWS IN DATABASE "SALES_DW"': [{"name": "DAILY_SALES", "schema_name": "MART"}],
    'SHOW USER FUNCTIONS IN DATABASE "SALES_DW"': [{"name": "FX", "schema_name": "MART", "is_builtin": "N"}],
    'SHOW PROCEDURES IN DATABASE "SALES_DW"': [{"name": "SYSTEM$X", "schema_name": "", "is_builtin": "Y"}],
    'SHOW TABLES IN SCHEMA "SALES_DW"."RAW"': [{"name": "ORDERS", "kind": "TABLE", "owner": "SYSADMIN", "rows": 1200, "bytes": 40960, "comment": ""},
                                              {"name": "EVENTS", "kind": "DYNAMIC", "owner": "SYSADMIN", "rows": 5, "bytes": 512, "comment": "x"}],
    'SHOW VIEWS IN SCHEMA "SALES_DW"."RAW"': [{"name": "V_ORDERS", "owner": "SYSADMIN", "is_materialized": "false", "text": "create view ..."}],
    'SHOW USER FUNCTIONS IN SCHEMA "SALES_DW"."RAW"': [],
    'SHOW PROCEDURES IN SCHEMA "SALES_DW"."RAW"': [{"name": "LOAD", "arguments": "LOAD(VARCHAR) RETURN VARCHAR", "is_builtin": "N", "owner": "SYSADMIN"}],
    "SHOW ROLES": [{"name": "ACCOUNTADMIN"}, {"name": "PUBLIC"}, {"name": "ANALYST"}],
    "SHOW USERS": [{"name": "PRIYA", "login_name": "PRIYA", "disabled": "false", "default_role": "ANALYST"}],
    'SHOW GRANTS TO USER "PRIYA"': [{"role": "ANALYST"}],
}


def show(sql):
    key = " ".join(sql.split())
    if key in SHOW:
        return SHOW[key]
    raise AssertionError(f"unexpected SQL: {key}")


class CatalogTests(unittest.TestCase):
    def run_with(self, answer, fn):
        with FakeDriver(answer) as drv, patch.object(sf.store, "resolve_password", return_value="pw"):
            return fn(), drv

    def test_databases_hide_the_snowflake_system_database(self):
        rows, _ = self.run_with(show, lambda: src().databases())
        self.assertEqual([r["name"] for r in rows], ["SALES_DW"])

    def test_schemas_are_counted_from_show_output_without_a_warehouse(self):
        rows, drv = self.run_with(show, lambda: src().schemas("SALES_DW"))
        self.assertEqual({r["name"]: (r["tables"], r["views"], r["routines"]) for r in rows}, {"RAW": (2, 0, 0), "MART": (0, 1, 1)})
        self.assertTrue(all(s.startswith("SHOW ") for s in drv.sent()))

    def test_objects_map_snowflake_kinds(self):
        rows, _ = self.run_with(show, lambda: src().objects("SALES_DW", "RAW"))
        got = {(r["name"], r["type"]) for r in rows}
        self.assertEqual(got, {("ORDERS", "TABLE"), ("EVENTS", "DYNAMIC TABLE"), ("V_ORDERS", "VIEW"), ("LOAD(VARCHAR)", "PROCEDURE")})
        orders = next(r for r in rows if r["name"] == "ORDERS")
        self.assertEqual((orders["est_rows"], orders["size_bytes"]), (1200, 40960))

    def test_roles_list_users_with_their_roles(self):
        rows, _ = self.run_with(show, lambda: src().roles())
        priya = next(r for r in rows if r["name"] == "PRIYA")
        self.assertEqual((priya["can_login"], priya["member_of"]), (True, ["ANALYST"]))
        self.assertTrue(next(r for r in rows if r["name"] == "ACCOUNTADMIN")["superuser"])

    def test_changes_fall_back_to_the_role_s_own_history(self):
        def answer(sql):
            if "ACCOUNT_USAGE" in sql:
                return Exception("Object 'SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY' does not exist or not authorized.")
            return [{"EVENT_TIME": "2026-11-15 10:00:00", "USERNAME": "PRIYA", "COMMAND_TAG": "ALTER_TABLE_ADD_COLUMN",
                     "SCHEMA_NAME": "RAW", "QUERY": "ALTER TABLE raw.orders ADD COLUMN x INT", "ROLE_NAME": "SYSADMIN"}]
        rows, drv = self.run_with(answer, lambda: src().changes("SALES_DW", object_name="RAW.ORDERS"))
        self.assertEqual((rows[0]["username"], rows[0]["command_tag"]), ("PRIYA", "ALTER_TABLE_ADD_COLUMN"))
        self.assertIn("INFORMATION_SCHEMA.QUERY_HISTORY", drv.sent()[-1])


class AccessTests(unittest.TestCase):
    def build(self, *args):
        with FakeDriver(show) as drv, patch.object(sf.store, "resolve_password", return_value="pw"):
            return src().build_statements(*args), drv

    def test_privileges_go_to_a_role(self):
        (stmts, preview), _ = self.build("GRANT", "SALES_DW", {"object_type": "SCHEMA", "schema": "RAW", "privileges": ["USAGE"]}, "ANALYST")
        self.assertEqual(stmts, ['GRANT USAGE ON SCHEMA "SALES_DW"."RAW" TO ROLE "ANALYST"'])

    def test_granting_to_a_user_explains_the_role_model(self):
        with self.assertRaisesRegex(ValueError, "privileges go to a role"):
            self.build("GRANT", "SALES_DW", {"object_type": "DATABASE", "privileges": ["USAGE"]}, "PRIYA")

    def test_a_user_gets_a_role(self):
        (stmts, _), _ = self.build("GRANT_ROLE", "SALES_DW", {"role": "ANALYST"}, "PRIYA")
        self.assertEqual(stmts, ['GRANT ROLE "ANALYST" TO USER "PRIYA"'])

    def test_users_are_not_created_from_the_portal(self):
        with self.assertRaisesRegex(ValueError, "MFA or a key pair"):
            self.build("CREATE_USER", "SALES_DW", {}, "NEW_USER")

    def test_built_in_roles_cannot_be_dropped(self):
        with self.assertRaisesRegex(ValueError, "built-in"):
            self.build("DROP_PRINCIPAL", "SALES_DW", {}, "ACCOUNTADMIN")


class RuleQueryTests(unittest.TestCase):
    def test_a_rule_query_runs_in_a_rolled_back_transaction_with_the_rule_time_limit(self):
        with FakeDriver(lambda sql: [{"DUP_KEYS": 2, "DUP_ROWS": 5}]) as drv, \
                patch.object(sf.store, "resolve_password", return_value="pw"), \
                patch.object(engine, "_source", return_value=src()):
            rows = engine.query(7, "SALES_DW", 'SELECT COUNT(*) AS dup_keys, 5 AS dup_rows FROM "RAW"."ORDERS"')
        c = drv.connections[0]
        self.assertEqual(rows, [{"dup_keys": 2, "dup_rows": 5}])
        self.assertEqual((c.autocommit_set, c.rolled_back, c.closed), (False, True, True))
        self.assertEqual(c.kwargs["database"], "SALES_DW")
        self.assertEqual(c.kwargs["session_parameters"]["STATEMENT_TIMEOUT_IN_SECONDS"], engine.QUERY_SECONDS)

    def test_a_snowflake_timeout_becomes_the_too_slow_message(self):
        boom = Exception("000630 (57014): Statement reached its statement or warehouse timeout of 60 second(s) and was canceled.")
        with FakeDriver(lambda sql: boom) as drv, patch.object(sf.store, "resolve_password", return_value="pw"), \
                patch.object(engine, "_source", return_value=src()):
            with self.assertRaises(engine.TooSlow):
                engine.query(7, "SALES_DW", "SELECT 1")
        self.assertTrue(drv.connections[0].rolled_back)


if __name__ == "__main__":
    unittest.main()
