"""A POC batch with Bronze but no Gold: running while it is within its usual time, stuck after that (data_service.run_state)."""
import unittest
from datetime import datetime
from unittest.mock import patch

import builtin
import data_service as ds
import resolution

CFG = {"enabled": True, "severity": "HIGH", "factor": 3, "min_minutes": 20}
T0 = datetime(2026, 11, 15, 16, 0)


def layer(name, status="SUCCESS", start_min=0):
    return {"layer": name, "status": status, "started_at": T0.replace(minute=start_min).isoformat(sep=" ")}


def at(minutes):
    return datetime(2026, 11, 15, 16 + minutes // 60, minutes % 60)


class RunStateTests(unittest.TestCase):
    def test_complete(self):
        r = ds.run_state([layer("BRONZE"), layer("SILVER", start_min=1), layer("GOLD", start_min=7)], [7, 6, 8], CFG, at(9))
        self.assertEqual((r["state"], r["missing"]), ("complete", []))

    def test_running_within_its_usual_time(self):
        r = ds.run_state([layer("BRONZE")], [7, 6, 8], CFG, at(5))
        self.assertEqual((r["state"], r["missing"], r["usual_minutes"], r["limit_minutes"]), ("running", ["SILVER", "GOLD"], 7.0, 21.0))

    def test_stuck_after_three_times_the_usual(self):
        r = ds.run_state([layer("BRONZE"), layer("SILVER", start_min=1)], [7, 6, 8], CFG, at(25))
        self.assertEqual((r["state"], r["missing"], r["minutes"]), ("stuck", ["GOLD"], 25.0))

    def test_never_stuck_before_the_minimum(self):
        r = ds.run_state([layer("BRONZE")], [1, 1, 1], CFG, at(10))      # 3 x 1 min = 3, but the minimum is 20
        self.assertEqual((r["state"], r["limit_minutes"]), ("running", 20.0))

    def test_a_failed_layer_ends_the_run(self):
        r = ds.run_state([layer("BRONZE"), layer("SILVER", "FAILED", 1)], [7], CFG, at(90))
        self.assertEqual(r["state"], "ended")                            # its own FAILED incident covers it

    def test_no_history_waits_an_hour(self):
        self.assertEqual(ds.run_state([layer("BRONZE")], [], CFG, at(50))["state"], "running")
        self.assertEqual(ds.run_state([layer("BRONZE")], [], CFG, at(61))["state"], "stuck")

    def test_switched_off_is_never_stuck(self):
        self.assertEqual(ds.run_state([layer("BRONZE")], [7], {**CFG, "enabled": False}, at(300))["state"], "running")

    def test_incident_and_its_closing_reason(self):
        run = ds.run_state([layer("BRONZE")], [7, 6, 8], CFG, at(40))
        i = ds._stuck_incident({"batch_id": "B1", "business_date": "2026-11-15"}, run, CFG)
        self.assertEqual((i["id"], i["severity"], i["type"]), ("RUN-B1-INCOMPLETE", "HIGH", "PIPELINE_INCOMPLETE"))
        self.assertIn("SILVER and GOLD never finished", i["title"])
        finished = [{"layer": "BRONZE", "status": "SUCCESS"}, {"layer": "SILVER", "status": "SUCCESS"}, {"layer": "GOLD", "status": "SUCCESS"}]
        with patch.object(resolution.ds, "rows", return_value=finished):
            self.assertEqual(resolution._incomplete("RUN-B1-INCOMPLETE", [])[0], "run_finished")
        with patch.object(resolution.ds, "rows", return_value=finished[:2] + [{"layer": "GOLD", "status": "FAILED"}]):
            reason, _, newer = resolution._incomplete("RUN-B1-INCOMPLETE", ["RUN-B1-GOLD"])
            self.assertEqual((reason, newer), ("superseded", "RUN-B1-GOLD"))


if __name__ == "__main__":
    unittest.main()
