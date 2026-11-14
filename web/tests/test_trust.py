"""The standard trust score (web/trust.py): one formula for every source, explained by its subjects."""
import unittest

import trust
from trust import signal, status_signal


class TrustTests(unittest.TestCase):
    def test_marks_are_shares_and_the_score_is_their_average(self):
        sig = [signal("freshness", 1, "MEDIUM", "a")] * 9 + [signal("freshness", 0, "MEDIUM", "late")] \
            + [signal("volume", 1)] * 10 + [signal("schema", 1)] * 10
        t = trust.compute(sig)
        self.assertEqual([(x["key"], x["mark"]) for x in t["subjects"]], [("freshness", 90.0), ("volume", 100.0), ("schema", 100.0)])
        self.assertEqual(t["score"], 97)                                  # (90 + 100 + 100) / 3, no cap for a MEDIUM failure
        self.assertEqual(t["label"], "Healthy")
        self.assertEqual(t["not_measured"], ["Validity", "Uniqueness", "Reconciliation", "Pipeline runs"])   # left out, not 0

    def test_a_high_failure_caps_at_89_and_a_critical_one_at_74(self):
        sig = [signal("freshness", 1, "HIGH", "ok")] * 9 + [signal("freshness", 0, "HIGH", "bronze.refunds is late")] \
            + [signal("volume", 1)] * 10 + [signal("schema", 1)] * 10
        t = trust.compute(sig)
        self.assertEqual((t["score"], t["label"], t["average"]), (89, "Needs attention", 96.7))
        self.assertEqual(t["cap"]["text"], "bronze.refunds is late")
        self.assertIn("capped at 89", t["reasons"][0]["reason"])          # the cap is the first reason
        recon = [signal("reconciliation", 1, "CRITICAL")] * 9 + [signal("reconciliation", 0, "CRITICAL", "settlement does not match on 11 Nov")]
        t = trust.compute(sig + recon)                                    # average 94: one critical mismatch still means At risk
        self.assertEqual((t["score"], t["label"], t["cap"]["severity"]), (74, "At risk", "CRITICAL"))
        low = trust.compute([signal("reconciliation", 0, "CRITICAL", "x")])
        self.assertEqual((low["score"], low["cap"]), (0, None))           # already below the cap: nothing to cap

    def test_more_passing_rules_never_lower_the_score(self):
        one_fail = [signal("validity", 0, "MEDIUM", "x")] + [signal("validity", 1, "MEDIUM")] * 9
        more = one_fail + [signal("validity", 1, "MEDIUM")] * 30
        self.assertGreater(trust.compute(more)["score"], trust.compute(one_fail)["score"])

    def test_severity_weights_a_subject(self):
        t = trust.compute([signal("validity", 0, "CRITICAL", "big", failed=False), signal("validity", 1, "LOW")])
        self.assertEqual(t["subjects"][0]["mark"], 20.0)                  # 1 of weight 5 passes

    def test_a_share_lowers_the_mark_and_only_an_open_failure_caps(self):
        t = trust.compute([signal("validity", 0.98, "HIGH", "20 rows rejected", failed=False)])
        self.assertEqual((t["score"], t["cap"]), (98, None))
        t = trust.compute([signal("volume", 0.995, "HIGH", "5 rows lost", failed=True)])
        self.assertEqual(t["score"], 89)

    def test_rule_results(self):
        self.assertIsNone(status_signal("schema", None, "HIGH", "never ran"))
        s = status_signal("schema", "WARN", "HIGH", "w")
        self.assertEqual((s["ok"], s["failed"]), (0.5, False))            # a warning counts half and never caps
        self.assertTrue(status_signal("schema", "FAIL", "HIGH", "f")["failed"])

    def test_each_subject_lists_its_checks_problems_first(self):
        sig = [signal("volume", 1, "HIGH", "3 rows lost", ok_text="no rows lost"),
               signal("volume", 0.98, "MEDIUM", "2% short", failed=False),
               status_signal("volume", "FAIL", "HIGH", "'row count' failed", origin="rule", rule_id=7)]
        checks = trust.compute(sig)["subjects"][0]["checks"]
        self.assertEqual([(c["text"], c["state"]) for c in checks],
                         [("'row count' failed", "fail"), ("2% short", "partial"), ("no rows lost", "pass")])
        self.assertEqual((checks[0]["origin"], checks[0]["rule_id"]), ("rule", 7))
        self.assertEqual(checks[2]["origin"], "built-in")
        lost = trust.compute([signal("volume", 0.99, "HIGH", "1 row lost", failed=True)])["subjects"][0]["checks"][0]
        self.assertEqual(lost["state"], "fail")                           # an open problem that caps the score shows as failing

    def test_nothing_measured_is_no_score_not_a_perfect_one(self):
        t = trust.compute([])
        self.assertEqual((t["score"], t["label"]), (None, "No data yet"))

    def test_confidence_is_reported_beside_the_score(self):
        c = trust.confidence(10, 44, "tables")
        self.assertEqual((c["level"], c["text"]), ("low", "Low confidence: 10 of 44 tables measured"))
        self.assertEqual(trust.confidence(40, 44, "tables")["level"], "high")
        self.assertIsNone(trust.confidence(3, None, "tables"))
        sig = [signal("schema", 1)]
        self.assertEqual(trust.compute(sig, c)["score"], trust.compute(sig)["score"])   # never mixed into the number

    def test_reason_points_add_up_to_what_the_average_lost(self):
        sig = [signal("freshness", 0, "MEDIUM", "late"), signal("freshness", 1, "MEDIUM"), signal("volume", 0.5, "LOW", "half", failed=False)]
        t = trust.compute(sig)
        self.assertAlmostEqual(sum(r["points"] for r in t["reasons"]), 100 - t["average"], delta=0.2)


if __name__ == "__main__":
    unittest.main()
