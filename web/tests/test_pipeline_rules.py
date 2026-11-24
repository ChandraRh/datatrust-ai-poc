"""Pipeline rules (web/checks/pipeline_rules.py): each kind against a made-up run history, time zones, the 14-day replay,
validation and rules described in words."""
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from checks import pipeline_rules as P

UTC = timezone.utc
IST = timezone(timedelta(hours=5, minutes=30))
NOW = datetime(2026, 11, 24, 12, 0, tzinfo=UTC)          # a Tuesday, 17:30 in India


def run(start, minutes=2, status="Succeeded", rows=1000, run_id=None):
    end = start + timedelta(minutes=minutes) if status not in P.RUNNING else None
    return {"run_id": run_id or start.isoformat(), "status": status, "run_start": start, "run_end": end,
            "duration_ms": minutes * 60000 if end else None, "rows_written": rows, "rows_read": rows}


def evaluate(cfg, runs, now=NOW, preview=False):
    with patch.object(P, "_runs", return_value=sorted(runs, key=lambda r: r["run_start"], reverse=True)):
        return P.evaluate({"config": {"source_id": 3, "pipeline": "PL", **cfg}, **({} if preview else {"check_id": 1})}, now=now)


class KindTests(unittest.TestCase):
    def test_last_run(self):
        self.assertEqual(evaluate({"kind": "last_run"}, [run(NOW - timedelta(hours=2))])["status"], "PASS")
        r = evaluate({"kind": "last_run"}, [run(NOW - timedelta(hours=3)), run(NOW - timedelta(hours=1), status="Failed")])
        self.assertEqual((r["status"], r["details"]["run_id"] is not None), ("FAIL", True))
        self.assertEqual(evaluate({"kind": "last_run"}, [])["status"], "WARN")

    def test_success_rate(self):
        runs = [run(NOW - timedelta(days=d)) for d in range(1, 9)] + [run(NOW - timedelta(days=2, hours=1), status="Failed")]
        r = evaluate({"kind": "success_rate", "days": 7, "min_pct": 90}, runs)
        self.assertEqual(r["status"], "FAIL")                     # 7 of 8 in the last 7 days (the 7th day counts) = 87.5%
        self.assertAlmostEqual(r["observed"], 87.5)

    def test_finishes_by_uses_the_rules_time_zone(self):
        # 09:00 in India = 03:30 UTC. A run ending at 03:00 UTC (08:30 IST) is on time; one ending at 04:00 UTC is late.
        on_time = run(datetime(2026, 11, 24, 2, 58, tzinfo=UTC))
        late = run(datetime(2026, 11, 24, 3, 58, tzinfo=UTC))
        cfg = {"kind": "finishes_by", "time": "09:00", "days": list(range(7)), "utc_offset_min": 330}
        self.assertEqual(evaluate(cfg, [on_time])["status"], "PASS")
        r = evaluate(cfg, [late])
        self.assertEqual(r["status"], "FAIL")
        self.assertIn("succeeded later, at 09:30", r["message"])

    def test_database_times_in_another_zone_are_read_as_what_they_are(self):
        ended_ist = datetime(2026, 11, 24, 8, 30, tzinfo=IST) - timedelta(minutes=2)     # the database answers in +05:30
        r = evaluate({"kind": "finishes_by", "time": "09:00", "days": list(range(7)), "utc_offset_min": 330},
                     [{**run(ended_ist), "run_start": P._dt(ended_ist), "run_end": P._dt(ended_ist + timedelta(minutes=2))}])
        self.assertIn("succeeded at 08:30", r["message"])

    def test_finishes_by_skips_days_not_chosen(self):
        cfg = {"kind": "finishes_by", "time": "09:00", "days": [5, 6], "utc_offset_min": 0}       # Sat, Sun only
        saturday = datetime(2026, 11, 21, 8, 0, tzinfo=UTC)
        self.assertEqual(evaluate(cfg, [run(saturday), run(saturday + timedelta(days=1))])["status"], "PASS")
        self.assertIn("2026-11-22", evaluate(cfg, [run(saturday)])["message"])                  # Sunday had none

    def test_rows_against_the_average_and_a_range(self):
        runs = [run(NOW - timedelta(days=d), rows=1000) for d in range(2, 7)] + [run(NOW - timedelta(hours=1), rows=400)]
        r = evaluate({"kind": "rows", "mode": "average", "pct": 30, "days": 14}, runs)
        self.assertEqual((r["status"], r["details"]["difference_pct"]), ("FAIL", -60.0))
        self.assertEqual(evaluate({"kind": "rows", "mode": "range", "min": 100}, runs)["status"], "PASS")
        self.assertEqual(evaluate({"kind": "rows", "mode": "average", "pct": 30, "days": 14}, runs[-2:])["status"], "WARN")   # too few

    def test_duration_and_stuck(self):
        self.assertEqual(evaluate({"kind": "duration", "max_minutes": 5}, [run(NOW - timedelta(hours=1), minutes=9)])["status"], "FAIL")
        stuck = run(NOW - timedelta(minutes=90), status="InProgress")
        self.assertEqual(evaluate({"kind": "not_stuck", "max_minutes": 60}, [stuck])["status"], "FAIL")
        self.assertEqual(evaluate({"kind": "not_stuck", "max_minutes": 120}, [stuck])["status"], "PASS")


class ReplayTests(unittest.TestCase):
    def test_a_preview_replays_the_last_14_days(self):
        runs = [run(NOW - timedelta(days=d, hours=1), status="Failed" if d in (3, 4) else "Succeeded") for d in range(1, 16)]
        r = evaluate({"kind": "last_run"}, runs, preview=True)
        b = r["details"]["backtest"]
        self.assertEqual((b["days"], len(b["by_day"]), b["fail"]), (14, 14, 2))

    def test_a_saved_rule_does_not_replay(self):
        self.assertNotIn("backtest", evaluate({"kind": "last_run"}, [run(NOW - timedelta(hours=1))])["details"])

    def test_the_replay_ignores_runs_that_had_not_happened_yet(self):
        late_failure = run(NOW - timedelta(hours=1), status="Failed")
        r = evaluate({"kind": "last_run"}, [run(NOW - timedelta(days=2)), late_failure], preview=True)
        self.assertEqual(r["details"]["backtest"]["by_day"][-1]["status"], "PASS")    # yesterday it had not failed yet


class DefinitionTests(unittest.TestCase):
    def test_validation_messages(self):
        base = {"source_id": 3, "pipeline": "PL"}
        for cfg, words in [({**base, "kind": "x"}, "choose what to check"), ({"pipeline": "PL", "kind": "last_run"}, "data factory"),
                           ({**base, "kind": "finishes_by", "time": "25:00"}, "09:00"),
                           ({**base, "kind": "finishes_by", "time": "09:00", "days": []}, "at least one day"),
                           ({**base, "kind": "rows", "mode": "range"}, "minimum, a maximum"),
                           ({**base, "kind": "rows", "mode": "range", "min": 10, "max": 5}, "larger than the maximum"),
                           ({**base, "kind": "success_rate", "min_pct": 150}, "between")]:
            with self.assertRaises(ValueError) as e:
                P.validate_config(cfg)
            self.assertIn(words, str(e.exception))

    def test_describe(self):
        self.assertEqual(P.describe(P.validate_config({"source_id": 3, "pipeline": "PL", "kind": "finishes_by", "time": "9:00",
                                                       "days": [0, 1, 2, 3, 4], "utc_offset_min": 330})),
                         "PL succeeds by 09:00 (UTC+05:30) on Mon-Fri")

    def test_phrases(self):
        self.assertEqual(P.phrase("must succeed by 9 am on weekdays")["settings"], {"time": "09:00", "days": [0, 1, 2, 3, 4]})
        self.assertEqual(P.phrase("finish by 6pm")["settings"]["time"], "18:00")
        self.assertEqual(P.phrase("should take under 2 hours")["settings"], {"max_minutes": 120})
        self.assertEqual(P.phrase("at least 1,000 rows")["settings"]["min"], 1000)
        self.assertEqual(P.phrase("95% of runs succeed")["kind"], "success_rate")
        self.assertIsNone(P.phrase("make coffee"))

    def test_from_text_without_a_model_uses_the_phrases(self):
        with patch("llm.provider", return_value="none"), patch.object(P, "_runs", return_value=[]):
            r = P.from_text(3, "PL", None, "takes under 20 minutes", 330)
        self.assertTrue(r["ok"])
        self.assertEqual((r["config"]["kind"], r["config"]["max_minutes"], r["source"]), ("duration", 20, "phrase"))


if __name__ == "__main__":
    unittest.main()
