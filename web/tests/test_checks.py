"""Data checks: SQL safety, both dialects, and the outcome of each check type (database replaced by a fake)."""
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

from checks import dialects, engine, store

PG, MS = dialects.Postgres(), dialects.SqlServer()


def chk(t, **kw):
    base = {"check_id": None, "check_type": t, "connection_id": 1, "database": None, "schema_name": "sales", "table_name": "orders",
            "config": {}, "state": {}}
    base.update(kw)
    return base


class FakeDb:
    """query() stand-in: answers by the first SQL fragment that matches."""
    def __init__(self, answers, dialect=PG):
        self.answers, self.dialect, self.sql = answers, dialect, []

    def query(self, connection_id, database, sql, params=(), max_rows=1000):
        self.sql.append(sql)
        for frag, rows in self.answers:
            if frag in sql:
                return rows
        raise AssertionError(f"unexpected SQL: {sql}")

    def __enter__(self):
        self.p = [patch.object(engine, "query", self.query), patch.object(engine, "_dialect", lambda cid: self.dialect)]
        for p in self.p:
            p.start()
        return self

    def __exit__(self, *a):
        for p in self.p:
            p.stop()


class SqlSafetyTests(unittest.TestCase):
    def test_only_one_read_only_select(self):
        self.assertEqual(dialects.clean_select(" select 1 ; "), "select 1")
        self.assertTrue(dialects.clean_select("WITH x AS (SELECT 1) SELECT * FROM x"))
        for bad in ("DELETE FROM t", "SELECT 1; DROP TABLE t", "SELECT * INTO t2 FROM t", "UPDATE t SET a=1",
                    "select 1 -- fine\n; truncate t", "EXEC sp_who", ""):
            with self.assertRaises(ValueError, msg=bad):
                dialects.clean_select(bad)

    def test_words_inside_strings_are_fine(self):
        self.assertTrue(dialects.clean_select("SELECT * FROM audit WHERE action = 'DELETE'"))

    def test_where_is_checked_too(self):
        self.assertEqual(PG.where("amount > 0"), " WHERE amount > 0")
        with self.assertRaises(ValueError):
            PG.where("1=1; DROP TABLE x")

    def test_identifiers_are_quoted_per_dialect(self):
        self.assertEqual(PG.table("sales", 'we"ird'), '"sales"."we""ird"')
        self.assertEqual(MS.table("dbo", "a]b"), "[dbo].[a]]b]")
        self.assertIn("LIMIT 5", PG.limit("SELECT 1", 5))
        self.assertIn("TOP 5", MS.limit("SELECT 1", 5))
        self.assertEqual(MS.limit("WITH x AS (SELECT 1) SELECT * FROM x", 5), "WITH x AS (SELECT 1) SELECT * FROM x")
        self.assertIn("?", MS.columns_sql())
        with self.assertRaises(ValueError):
            dialects.for_type("snowflake")


class CheckTypeTests(unittest.TestCase):
    def test_freshness(self):
        old = datetime.now() - timedelta(hours=30)
        with FakeDb([("MAX(", [{"latest": old}])]):
            r = engine.run(chk("freshness", config={"column": "loaded_at", "max_age_hours": 24}))
        self.assertEqual(r["status"], "FAIL")
        self.assertAlmostEqual(r["observed"], 30, delta=0.1)
        with FakeDb([("MAX(", [{"latest": datetime.now()}])]):
            self.assertEqual(engine.run(chk("freshness", config={"column": "loaded_at"}))["status"], "PASS")
        with FakeDb([("MAX(", [{"latest": None}])]):
            self.assertEqual(engine.run(chk("freshness", config={"column": "loaded_at"}))["status"], "FAIL")

    def test_volume_learns_the_usual(self):
        with FakeDb([("COUNT(*)", [{"n": 400}])]), patch.object(store, "usual_values", lambda cid, n=14: [1000, 1010, 990]):
            r = engine.run(chk("volume", check_id=7, config={"max_drop_pct": 50}))
        self.assertEqual(r["status"], "FAIL")
        self.assertIn("60% fewer than usual", r["message"])
        with FakeDb([("COUNT(*)", [{"n": 0}])]):
            self.assertEqual(engine.run(chk("volume"))["status"], "FAIL")
        with FakeDb([("COUNT(*)", [{"n": 5}])]):
            self.assertEqual(engine.run(chk("volume"))["status"], "PASS")

    def test_null_rate(self):
        with FakeDb([("COUNT(", [{"total": 200, "filled": 190}])]):
            r = engine.run(chk("null_rate", config={"column": "currency", "max_null_pct": 1}))
        self.assertEqual((r["status"], r["observed"]), ("FAIL", 5.0))

    def test_uniqueness_on_sql_server(self):
        db = FakeDb([("dup_keys", [{"dup_keys": 2, "dup_rows": 5}]), ("TOP 20", [{"order_id": "A", "copies": 3}])], MS)
        with db:
            r = engine.run(chk("uniqueness", config={"columns": ["order_id"]}))
        self.assertEqual((r["status"], r["observed"]), ("FAIL", 2))
        self.assertEqual(r["details"]["extra_rows"], 3)
        self.assertIn("[sales].[orders]", db.sql[0])

    def test_schema_baseline_then_changes(self):
        cols = [{"column_name": "id", "data_type": "integer", "is_nullable": "NO"},
                {"column_name": "amount", "data_type": "numeric", "is_nullable": "YES"}]
        with FakeDb([("information_schema.columns", cols)]):
            first = engine.run(chk("schema"))
        self.assertEqual(first["status"], "PASS")
        base = first["new_state"]
        changed = [{"column_name": "id", "data_type": "bigint", "is_nullable": "NO"},
                   {"column_name": "note", "data_type": "text", "is_nullable": "YES"}]
        with FakeDb([("information_schema.columns", changed)]):
            r = engine.run(chk("schema", state=base))
        self.assertEqual(r["status"], "FAIL")
        self.assertEqual(r["details"]["removed"], ["amount"])
        self.assertEqual(r["details"]["type_changed"][0]["now"], "bigint")
        with FakeDb([("information_schema.columns", cols + [{"column_name": "x", "data_type": "text", "is_nullable": "YES"}])]):
            self.assertEqual(engine.run(chk("schema", state=base))["status"], "WARN")

    def test_custom_sql_modes(self):
        with FakeDb([("dt_q", [{"order_id": 1}, {"order_id": 2}])]):
            r = engine.run(chk("custom_sql", config={"sql": "SELECT order_id FROM o WHERE amount < 0"}))
        self.assertEqual((r["status"], r["observed"]), ("FAIL", 2))
        with FakeDb([("dt_q", [{"n": 0}])]):
            self.assertEqual(engine.run(chk("custom_sql", config={"sql": "SELECT 0 AS n", "mode": "value", "op": "=", "threshold": 0}))["status"], "PASS")
        r = engine.run(chk("custom_sql", config={"sql": "DELETE FROM o"}))
        self.assertEqual(r["status"], "ERROR")
        self.assertIn("must start with SELECT", r["message"])
        r = engine.run(chk("custom_sql", config={"sql": "SELECT * INTO copy_of_o FROM o"}))
        self.assertIn("'INTO' is not allowed", r["message"])

    def test_reconciliation(self):
        left = [{"d": "2026-10-27", "n": 1364, "amt": 1759989.70}, {"d": "2026-10-28", "n": 10, "amt": 5.0}]
        right = [{"d": "2026-10-27", "n": 1361, "amt": 1756803.70}, {"d": "2026-10-28", "n": 10, "amt": 5.004},
                 {"d": "2026-10-22", "n": 1, "amt": 1.0}]
        answers = [("FROM settled", left), ("FROM loaded", right)]
        spec = {"left": {"connection_id": 1, "sql": "SELECT * FROM settled", "label": "gateway"},
                "right": {"connection_id": 1, "sql": "SELECT * FROM loaded", "label": "silver"}, "tolerance": 0.01}
        with FakeDb(answers):
            both = engine.run(chk("reconciliation", config=dict(spec, match="both")))
            left_only = engine.run(chk("reconciliation", config=dict(spec, match="left")))
        self.assertEqual((both["status"], both["observed"]), ("FAIL", 2))           # 27 Oct differs, 22 Oct missing in gateway
        self.assertEqual(left_only["observed"], 1)                                  # 22 Oct ignored; 28 Oct within tolerance
        diff = left_only["details"]["mismatches"][0]["differences"]
        self.assertEqual(diff["n"]["difference"], 3)
        self.assertAlmostEqual(diff["amt"]["difference"], 3186.0, places=2)

    def test_unreachable_source_is_an_error_not_a_crash(self):
        with patch.object(engine, "_dialect", side_effect=ValueError("connection 9 not found")):
            r = engine.run(chk("volume", connection_id=9))
        self.assertEqual(r["status"], "ERROR")
        self.assertIn("connection 9 not found", r["message"])


class ValidationTests(unittest.TestCase):
    def test_required_fields(self):
        with self.assertRaises(ValueError):
            store.validate({"check_type": "volume", "name": "x", "connection_id": 1})          # no table
        v = store.validate({"check_type": "uniqueness", "name": "k", "connection_id": "3", "schema_name": "s", "table_name": "t",
                            "config": {"columns": "a, b"}})
        self.assertEqual((v["connection_id"], v["config"]["columns"]), (3, ["a", "b"]))
        with self.assertRaises(ValueError):
            store.validate({"check_type": "reconciliation", "name": "r", "config": {"left": {"connection_id": 1, "sql": "x"}}})
        with self.assertRaises(ValueError):
            store.validate({"check_type": "volume", "name": "x", "connection_id": 1, "schema_name": "s", "table_name": "t",
                            "schedule_minutes": 1})


if __name__ == "__main__":
    unittest.main()
