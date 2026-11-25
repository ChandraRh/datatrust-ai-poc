"""Freshness that learns how often a table loads (checks/engine.py rhythm / learn / _freshness, checks/coverage.py plan)."""
import unittest
from datetime import date, datetime, timedelta, timezone
from unittest.mock import patch

from checks import coverage, engine

MON = date(2026, 11, 2)


def days(start, n, step=1, skip_weekends=False):
    out, d = [], start
    while len(out) < n:
        if not (skip_weekends and d.weekday() >= 5):
            out.append(d)
        d += timedelta(days=step)
    return out


class RhythmTests(unittest.TestCase):
    def test_daily(self):
        r = engine.rhythm(days(MON, 30))
        self.assertEqual((r["pattern"], r["longest_gap_days"], r["limit_hours"]), ("every day", 1, 28))      # 24 h + 15%

    def test_date_only_column_gets_half_a_day_more(self):
        self.assertEqual(engine.rhythm(days(MON, 30), date_only=True)["limit_hours"], 40)

    def test_weekdays(self):
        r = engine.rhythm(days(MON, 20, skip_weekends=True))
        self.assertTrue(r["pattern"].startswith("on weekdays"))
        self.assertEqual(r["longest_gap_days"], 3)                    # Friday -> Monday
        self.assertEqual(r["limit_hours"], 83)

    def test_weekly(self):
        r = engine.rhythm(days(MON, 8, step=7))
        self.assertEqual((r["pattern"], r["limit_hours"]), ("about every 7 days", 194))      # 168 h + 15%

    def test_one_odd_gap_does_not_decide(self):
        d = days(MON, 25) + [MON + timedelta(days=40)]                # daily, then one 16-day hole
        self.assertEqual(engine.rhythm(d)["longest_gap_days"], 1)

    def test_too_little_history(self):
        self.assertIsNone(engine.rhythm([MON, MON + timedelta(days=1)]))


class Dialect:
    def ident(self, n): return n
    def where(self, w): return f" WHERE {w}" if w else ""
    def since(self, c, h): return f"{c} >= now() - interval '{h} hours'"


def run_fresh(config, state, newest, load_days):
    calls = []

    def fake_query(cid, db, sql, params=(), max_rows=1000):
        calls.append(sql)
        if "GROUP BY" in sql:
            return [{"day": x} for x in load_days]
        return [{"latest": newest}]
    chk = {"check_type": "freshness", "connection_id": 1, "database": None, "config": config, "state": state}
    with patch.object(engine, "query", side_effect=fake_query):
        return engine._freshness(chk, Dialect(), "s.t"), calls


NOW = datetime.now(timezone.utc)


class AdaptiveRuleTests(unittest.TestCase):
    def test_learns_and_uses_the_learned_limit(self):
        weekly = days(date.today() - timedelta(days=56), 9, step=7)
        r, calls = run_fresh({"column": "loaded_at", "max_age_hours": 24, "adaptive": True}, {}, NOW - timedelta(days=3), weekly)
        self.assertEqual(r["status"], "PASS")                         # 3 days old is fine for a weekly table (24 h would fail)
        self.assertEqual(r["details"]["limit_hours"], 194)
        self.assertIn("learned: it loads about every 7 days", r["expected"])
        self.assertEqual(r["new_state"]["rhythm"]["limit_hours"], 194)
        self.assertEqual(len(calls), 2)

    def test_relearns_only_once_a_day(self):
        learned = {"limit_hours": 28, "pattern": "every day", "learned_at": (NOW - timedelta(hours=2)).isoformat()}
        r, calls = run_fresh({"column": "c", "adaptive": True}, {"rhythm": learned}, NOW - timedelta(hours=30), [])
        self.assertEqual((r["status"], len(calls)), ("FAIL", 1))      # 30 h > 28 h; no learning query this time
        self.assertNotIn("new_state", r)

    def test_too_little_history_keeps_the_starting_limit(self):
        r, _ = run_fresh({"column": "c", "max_age_hours": 26, "adaptive": True}, {}, NOW - timedelta(hours=30), [MON])
        self.assertEqual((r["status"], r["details"]["limit_hours"]), ("FAIL", 26.0))
        self.assertTrue(r["new_state"]["rhythm"]["too_few"])

    def test_a_fixed_rule_never_learns(self):
        r, calls = run_fresh({"column": "c", "max_age_hours": 24}, {}, NOW - timedelta(hours=30), [])
        self.assertEqual((r["status"], len(calls)), ("FAIL", 1))


class StartMonitoringTests(unittest.TestCase):
    def plan(self, newest, learned):
        cols = [{"name": "loaded_at", "type": "timestamp"}]
        with patch.object(engine, "_dialect", return_value=type("D", (Dialect,), {"table": lambda self, s, t: f"{s}.{t}"})()), \
                patch.object(engine, "query", return_value=[{"n_rows": 100, "mx0": newest, "w0": 0}]), \
                patch.object(engine, "learn", return_value=learned):
            return coverage.plan_table(1, None, "s", "t", cols)

    def test_uses_the_learned_rhythm(self):
        p = self.plan(NOW - timedelta(hours=2), {"load_days": 9, "window_days": 60, "pattern": "about every 7 days",
                                                 "longest_gap_days": 7, "limit_hours": 194})
        f = next(c for c in p["checks"] if c["check_type"] == "freshness")
        self.assertEqual((f["config"]["max_age_hours"], f["config"]["adaptive"]), (194, True))   # not 26 h from the snapshot
        self.assertIn("about every 7 days", f["description"])

    def test_without_history_falls_back_to_the_newest_row(self):
        p = self.plan(NOW - timedelta(hours=2), {"load_days": 1, "too_few": True})
        f = next(c for c in p["checks"] if c["check_type"] == "freshness")
        self.assertEqual(f["config"]["max_age_hours"], 26)
        self.assertIn("too little history", f["description"])

    def test_a_table_far_past_its_rhythm_is_static(self):
        p = self.plan(NOW - timedelta(days=40), {"load_days": 9, "window_days": 60, "pattern": "about every 7 days",
                                                 "longest_gap_days": 7, "limit_hours": 194})
        self.assertFalse(any(c["check_type"] == "freshness" for c in p["checks"]))


if __name__ == "__main__":
    unittest.main()


class PerLoadVolumeTests(unittest.TestCase):
    """Volume per load: the latest load day compared with earlier load days, whatever the rhythm."""

    def run_volume(self, per_day, arriving=0, config=None):
        def fake_query(cid, db, sql, params=(), max_rows=1000):
            if "GROUP BY" in sql:
                return [{"day": d, "n": n} for d, n in per_day.items()]
            return [{"n": arriving}]
        chk = {"check_type": "volume", "check_id": 1, "connection_id": 1, "database": None,
               "config": {"min_rows": 1, "max_drop_pct": 50, "window_column": "loaded_at", "per_load": True, **(config or {})}}
        with patch.object(engine, "query", side_effect=fake_query):
            return engine._volume(chk, Dialect(), "s.t")

    def weekly(self, sizes):
        start = date.today() - timedelta(days=7 * (len(sizes) - 1) + 2)
        return {start + timedelta(days=7 * i): n for i, n in enumerate(sizes)}

    def test_a_weekly_table_is_not_flagged_between_loads(self):
        r = self.run_volume(self.weekly([10000, 10200, 9900, 10100]))        # last load 2 days ago, nothing since
        self.assertEqual(r["status"], "PASS")
        self.assertEqual(r["details"]["usual"], 10000)
        self.assertIn("about every 7 days", r["expected"])

    def test_a_short_load_fails(self):
        r = self.run_volume(self.weekly([10000, 10200, 9900, 4000]))
        self.assertEqual(r["status"], "FAIL")
        self.assertIn("60% fewer than the usual load", r["message"])

    def test_todays_load_still_arriving_checks_the_previous_one(self):
        today = date.today()
        per_day = {today - timedelta(days=i): 1500 for i in range(1, 6)} | {today: 200}
        r = self.run_volume(per_day, arriving=12)
        self.assertEqual((r["status"], r["details"]["load_day"]), ("PASS", (today - timedelta(days=1)).isoformat()))
        self.assertIn("still arriving", r["message"])
        r = self.run_volume(per_day, arriving=0)                             # finished: today's small load is checked
        self.assertEqual(r["status"], "FAIL")

    def test_nothing_in_60_days_fails(self):
        self.assertEqual(self.run_volume({})["status"], "FAIL")


class StartMonitoringVolumeTests(StartMonitoringTests):
    def test_a_learned_rhythm_gives_volume_per_load(self):
        p = self.plan(NOW - timedelta(hours=2), {"load_days": 9, "window_days": 60, "pattern": "about every 7 days",
                                                 "longest_gap_days": 7, "limit_hours": 194})
        v = next(c for c in p["checks"] if c["check_type"] == "volume")
        self.assertEqual((v["name"], v["config"]["per_load"], v["config"]["window_column"]), ("s.t rows per load", True, "loaded_at"))
        self.assertIn("not compared", v["description"])

    def test_no_rhythm_gives_a_whole_table_count(self):
        p = self.plan(NOW - timedelta(hours=2), {"load_days": 1, "too_few": True})
        v = next(c for c in p["checks"] if c["check_type"] == "volume")
        self.assertNotIn("per_load", v["config"])
