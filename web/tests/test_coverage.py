"""Start monitoring: starter rules follow what the table shows today, never fail on day one for a non-problem, keep existing rules."""
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

from checks import coverage, dialects, engine

PG = dialects.Postgres()
COLS = [{"name": "order_id", "type": "integer"}, {"name": "created_at", "type": "timestamp without time zone"},
        {"name": "business_date", "type": "date"}]


def ago(hours):
    return datetime.now() - timedelta(hours=hours)


def plan(row, cols=COLS):
    with patch.object(engine, "_dialect", return_value=PG), patch.object(engine, "query", return_value=[row]) as q:
        p = coverage.plan_table(5, None, "sales", "orders", cols)
    return p, q.call_args[0][2]


def kinds(p):
    return {c["check_type"]: c for c in p["checks"]}


class PlanTests(unittest.TestCase):
    def test_daily_table_gets_fresh_per_day_volume_and_schema(self):
        p, sql = plan({"n_rows": 1_240_000, "mx0": ago(2), "w0": 8000, "mx1": ago(30)})
        k = kinds(p)
        self.assertEqual(k["freshness"]["config"], {"column": "created_at", "max_age_hours": 26})
        self.assertEqual(k["volume"]["config"]["window_column"], "created_at")      # each load's size, not the whole table
        self.assertIn("schema", k)
        self.assertIn("""COUNT(CASE WHEN "created_at" >= now() - interval '24 hours' THEN 1 END)""", sql)
        self.assertEqual(p["facts"]["rows_24h"], 8000)

    def test_static_table_gets_no_freshness_rule_and_says_why(self):
        p, _ = plan({"n_rows": 300, "mx0": ago(24 * 40), "w0": 0, "mx1": ago(24 * 40)})
        self.assertNotIn("freshness", kinds(p))
        self.assertIn("static", p["skipped"][0]["why"])
        self.assertNotIn("window_column", kinds(p)["volume"]["config"])           # whole-table count

    def test_weekly_table_gets_a_one_week_limit(self):
        p, _ = plan({"n_rows": 300, "mx0": ago(24 * 3), "w0": 0, "mx1": None})
        self.assertEqual(kinds(p)["freshness"]["config"]["max_age_hours"], 192)

    def test_empty_table_only_watches_its_columns(self):
        p, _ = plan({"n_rows": 0, "mx0": None, "w0": 0, "mx1": None})
        self.assertEqual(list(kinds(p)), ["schema"])
        self.assertEqual({s["check_type"] for s in p["skipped"]}, {"freshness", "volume"})

    def test_table_without_a_date_column(self):
        p, _ = plan({"n_rows": 50}, cols=[{"name": "code", "type": "text"}])
        self.assertEqual(set(kinds(p)), {"volume", "schema"})
        self.assertIn("No date or time column", p["skipped"][0]["why"])

    def test_date_only_column_gets_a_day_and_a_half(self):
        p, _ = plan({"n_rows": 900, "mx0": datetime.now().date() - timedelta(days=1)}, cols=[{"name": "business_date", "type": "date"}])
        self.assertEqual(kinds(p)["freshness"]["config"]["max_age_hours"], 36)
        self.assertNotIn("window_column", kinds(p)["volume"]["config"])

    def test_start_keeps_the_rules_a_table_already_has(self):
        planned = {"checks": [{"check_type": t, "name": f"x {t}", "config": {}} for t in ("freshness", "volume", "schema")],
                   "skipped": [], "facts": {}}
        with patch.object(coverage, "_conn", return_value={"name": "Sales DB"}), patch.object(engine, "_dialect", return_value=PG), \
                patch.object(coverage, "list_tables", return_value=[{"schema": "sales", "table": "orders", "columns": COLS}]), \
                patch.object(coverage, "_watched", return_value={("sales", "orders"): {"types": {"freshness": [3]}, "statuses": ["PASS"]}}), \
                patch.object(coverage, "plan_table", return_value=planned), \
                patch.object(coverage.store, "add_check", side_effect=[11, 12]) as add:
            r = coverage.start(5, [{"schema": "sales", "table": "orders"}, {"schema": "sales", "table": "gone"}], "admin")
        self.assertEqual([a["check_type"] for a in r["results"][0]["added"]], ["volume", "schema"])
        self.assertEqual(r["results"][0]["kept"], ["freshness"])
        self.assertEqual(r["results"][1]["error"], "table not found")
        self.assertEqual(add.call_args.kwargs["origin"], "auto")

    def test_volume_window_counts_only_recent_rows(self):
        chk = {"check_id": None, "connection_id": 5, "database": None,
               "config": {"min_rows": 1, "max_drop_pct": 50, "window_column": "created_at", "window_hours": 24}}
        with patch.object(engine, "query", return_value=[{"n": 7900}]) as q:
            r = engine._volume(chk, PG, '"sales"."orders"')
        self.assertIn("""WHERE ("created_at" >= now() - interval '24 hours')""", q.call_args[0][2])
        self.assertIn("in the last 24 h", r["message"])
        self.assertEqual(dialects.SqlServer().since("loaded_at", 24), "[loaded_at] >= DATEADD(hour, -24, SYSDATETIME())")


if __name__ == "__main__":
    unittest.main()
