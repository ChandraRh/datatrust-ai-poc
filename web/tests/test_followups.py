"""Investigate's follow-up checks (web/followups.py): the fixed plan's answers, the AI query guard, masking."""
import unittest
from datetime import date, timedelta
from unittest.mock import patch

import followups as F
from checks import dialects

PG = dialects.for_type("postgresql")
T = {"connection_id": 1, "database": None}


class MaskerTests(unittest.TestCase):
    def test_text_becomes_tokens_numbers_and_dates_stay(self):
        m = F.Masker()
        rows = m.rows([{"region": "NORTH", "day": "2026-11-25", "n": 33, "email": "a@x.com"},
                       {"region": "NORTH", "day": "2026-11-24 09:00:00", "n": 0, "email": None}])
        self.assertEqual(rows[0], {"region": "[v1]", "day": "2026-11-25", "n": 33, "email": "[v2]"})
        self.assertEqual(rows[1]["region"], "[v1]")                    # the same value keeps its token
        self.assertEqual(rows[1]["day"], "2026-11-24 09:00:00")

    def test_unmask_puts_the_values_back(self):
        m = F.Masker()
        m.rows([{"region": "NORTH"}])
        self.assertEqual(m.unmask({"conclusion": "[v1] sent nothing", "next": ["call [v1]", "[v9] unknown"]}),
                         {"conclusion": "NORTH sent nothing", "next": ["call NORTH", "[v9] unknown"]})


class ValidateTests(unittest.TestCase):
    ALLOWED = [("shop", "orders"), ("shop", "orders_raw")]

    def ok(self, sql):
        return F.validate(sql, self.ALLOWED, "postgres")

    def refused(self, sql):
        with self.assertRaises(ValueError) as e:
            self.ok(sql)
        return str(e.exception)

    def test_an_aggregate_on_the_incident_table_passes(self):
        self.ok("SELECT region, date_trunc('day', loaded_at) AS d, COUNT(*) FROM shop.orders GROUP BY 1, 2")
        self.ok("WITH x AS (SELECT region FROM orders_raw) SELECT region, count(*) FROM x GROUP BY region")

    def test_another_table_is_refused(self):
        self.assertIn("portal.users", self.refused("SELECT username FROM portal.users"))
        self.assertIn("customers", self.refused("SELECT o.region FROM shop.orders o JOIN shop.customers c ON c.id = o.id"))

    def test_writes_and_several_statements_are_refused(self):
        self.refused("WITH d AS (DELETE FROM shop.orders RETURNING *) SELECT count(*) FROM d")
        self.refused("SELECT 1 FROM shop.orders; DROP TABLE shop.orders")
        self.refused("UPDATE shop.orders SET region = 'x'")

    def test_user_functions_are_refused(self):
        self.assertIn("pg_sleep", self.refused("SELECT pg_sleep(30), count(*) FROM shop.orders"))
        self.assertIn("shop.fn", self.refused("SELECT shop.fn(region) FROM shop.orders"))


class PlanAnswerTests(unittest.TestCase):
    D, B = date(2026, 11, 25), date(2026, 11, 24)

    def groups(self, rows, grp="region"):
        with patch.object(F, "_q", return_value=rows):
            return F._volume_groups(T, PG, '"shop"."orders"', "loaded_at", grp, self.D, self.B)

    def test_a_sender_that_sent_nothing_is_named(self):
        r = self.groups([{"grp": "NORTH", "day": self.B, "n": 30}, {"grp": "SOUTH", "day": self.B, "n": 30},
                         {"grp": "SOUTH", "day": self.D, "n": 29}])
        self.assertIn("nothing at all arrived on 2026-11-25 from region = NORTH", r["text"])
        self.assertIn("about as usual", r["text"])
        self.assertEqual(r["_score"], 3)

    def test_a_drop_everywhere_is_called_spread(self):
        r = self.groups([{"grp": g, "day": d, "n": n} for g in ("A", "B", "C") for d, n in ((self.B, 30), (self.D, 10))])
        self.assertEqual(r["_score"], 0)
        best = F._best([r, {**r, "_column": "channel"}], "By {names}: spread")
        self.assertEqual(best["text"], "By region, channel: spread")

    def test_best_prefers_the_column_that_points_at_one_group(self):
        spread = {"text": "spread", "_score": 0, "_column": "channel", "ok": True}
        one = {"text": "NORTH", "_score": 3, "_column": "region", "ok": True}
        self.assertEqual(F._best([spread, one], "x")["text"], "NORTH")

    def test_empties_that_started_on_a_day(self):
        days = [date(2026, 11, 10) + timedelta(days=i) for i in range(14)]
        per = {d: {"day": d, "n": 100, "filled": 100 if d < date(2026, 11, 21) else 70} for d in days}
        with patch.object(F, "_per_day", return_value=("sql", per)):
            r = F._empties_per_day(T, PG, "t", "email", "loaded_at")
        self.assertIn("Empty email started on 2026-11-21", r["text"])

    def test_empties_on_every_day_are_long_standing(self):
        days = [date(2026, 11, 10) + timedelta(days=i) for i in range(14)]
        with patch.object(F, "_per_day", return_value=("sql", {d: {"day": d, "n": 100, "filled": 95} for d in days})):
            self.assertIn("long-standing", F._empties_per_day(T, PG, "t", "email", "loaded_at")["text"])

    def test_duplicates_inside_one_load(self):
        rows = [{"order_id": f"O{i}", "copies": 2, "days": 1, "first_seen": "2026-11-24 01:00", "last_seen": "2026-11-24 01:00"}
                for i in range(5)]
        with patch.object(F, "_q", return_value=rows):
            r = F._duplicates_when(T, PG, "t", ["order_id"], "loaded_at")
        self.assertIn("copied inside one day's load", r["text"])

    def test_duplicates_again_on_a_later_day(self):
        rows = [{"order_id": f"O{i}", "copies": 2, "days": 2, "first_seen": "2026-11-20", "last_seen": "2026-11-23"} for i in range(5)]
        with patch.object(F, "_q", return_value=rows):
            self.assertIn("no upsert", F._duplicates_when(T, PG, "t", ["order_id"], "loaded_at")["text"])

    def test_group_columns_by_name(self):
        cols = [{"name": n, "type": t} for n, t in (("order_id", "text"), ("region", "text"), ("channel", "character varying"),
                                                      ("amount", "numeric"), ("source_system", "text"), ("email", "text"))]
        self.assertEqual(F._group_columns(cols, {"email"}), ["source_system", "channel", "region"])


class LineageStepTests(unittest.TestCase):
    def steps(self, nb):
        with patch.object(F, "neighbours", return_value=nb):
            return F._lineage_steps("postgresql", "h", "db", "silver", "payments")

    def test_an_upstream_problem_is_flagged(self):
        nb = {"known": True, "writers": [], "readers": [], "inputs": [
            {"id": "a", "label": "bronze.payments", "kind": "table", "incidents": [{"title": "rows missing"}]}]}
        s = self.steps(nb)[0]
        self.assertIs(s["ok"], False)
        self.assertIn("bronze.payments: rows missing", s["text"])

    def test_clean_upstream_points_at_this_table(self):
        nb = {"known": True, "writers": [], "readers": [], "inputs": [{"id": "a", "label": "bronze.payments", "kind": "table"}]}
        s = self.steps(nb)[0]
        self.assertIs(s["ok"], True)
        self.assertIn("it has no failing rule", s["text"])
        self.assertIn("Not watched by any rule: bronze.payments", s["text"])

    def test_no_map_and_unknown_table_are_not_answers(self):
        self.assertIsNone(self.steps(None)[0]["ok"])
        self.assertIn("does not read", self.steps({"known": False})[0]["text"])

    def test_a_failed_factory_run_is_the_place_to_start(self):
        runs = [{"run_start": "2026-11-25 02:00", "status": "Failed", "cause": "Source file missing", "message": "path not found",
                 "rows_written": None}]
        with patch("adf.insights.runs", return_value=runs):
            s = F._writer_step({"id": "job:adf:3:pl_load", "label": "PL_Load", "kind": "pipeline"}, "bronze.payments")
        self.assertIs(s["ok"], False)
        self.assertIn("last run (2026-11-25 02:00) failed: Source file missing", s["text"])


if __name__ == "__main__":
    unittest.main()
