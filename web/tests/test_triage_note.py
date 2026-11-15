"""AI triage note on a new Critical incident's Google Chat card: Critical only, a few per round, never rows of the data."""
import unittest
from unittest.mock import patch

import ai
import alerts

EXPLAIN = {"available": True, "headline": "3 payments were rejected by STATUS_VALID, which is the whole gap.", "confidence": "high",
           "fix_steps": ["Fix the status typo in the source file", "Re-insert the rows", "Re-run gold"], "unverified": []}
INV = {"found": True, "incident": {"id": "RECON-1", "severity": "CRITICAL", "metrics": {}}, "checks": [], "chain": [],
       "root_cause": "r", "sample": [{"payment_id": "P1", "email": "jane@corp.example"}], "impact": {}}


def card(inc):
    with patch.object(alerts, "public_url", return_value="https://dt.example"):
        return str(alerts.incident_card(inc, []))


class TriageNote(unittest.TestCase):
    def setUp(self):
        alerts._triage_budget["left"] = alerts.TRIAGE_PER_ROUND

    def test_a_critical_card_carries_the_likely_cause_and_first_steps(self):
        with patch.object(ai, "explain", return_value=EXPLAIN) as ex:
            text = card({"id": "RECON-1", "severity": "CRITICAL", "title": "Revenue gap", "summary": "s", "scope": "db:1"})
        self.assertIn("Likely cause (AI, high confidence)", text)
        self.assertIn("1. Fix the status typo", text)
        self.assertNotIn("Re-run gold", text)                                     # first two steps only
        self.assertEqual(ex.call_args.kwargs.get("include_rows"), False)            # automatic: no rows to the model

    def test_not_for_lower_severities(self):
        with patch.object(ai, "explain", return_value=EXPLAIN) as ex:
            self.assertNotIn("Likely cause", card({"id": "x", "severity": "HIGH", "title": "t", "summary": "s"}))
        ex.assert_not_called()

    def test_a_few_per_round(self):
        with patch.object(ai, "explain", return_value=EXPLAIN) as ex:
            for n in range(5):
                card({"id": f"c{n}", "severity": "CRITICAL", "title": "t", "summary": "s"})
        self.assertEqual(ex.call_count, alerts.TRIAGE_PER_ROUND)

    def test_no_model_answer_means_no_note_but_still_a_card(self):
        with patch.object(ai, "explain", return_value={"available": False, "reason": "quota"}):
            text = card({"id": "c", "severity": "CRITICAL", "title": "Revenue gap", "summary": "s"})
        self.assertIn("Revenue gap", text)
        self.assertNotIn("Likely cause", text)

    def test_the_model_never_sees_rows_and_investigate_finds_the_saved_note(self):
        sent = {}

        def grounded(kind, subject, evidence, system, prompt, refresh=False):
            sent["ev"] = evidence
            return {"headline": "h", "model": "m"}

        with patch.object(ai.scopes, "investigate", return_value=INV), patch.object(ai.llm, "provider", return_value="gemini"), \
                patch.object(ai.llm, "grounded", side_effect=grounded):
            ai.explain("RECON-1", "db:1", include_rows=False)
        self.assertEqual(sent["ev"]["sample_rows"], [])
        self.assertNotIn("jane@", str(sent["ev"]))
        saved = {}
        with patch.object(ai.scopes, "investigate", return_value=INV), patch.object(ai.llm, "provider", return_value="gemini"), \
                patch.object(ai.llm, "cached", side_effect=lambda k, s, ev: saved.setdefault("hit", {"headline": "h"}) if ev.get("sample_rows") == [] else None):
            out = ai.explain("RECON-1", "db:1", cached_only=True)              # Investigate: rows allowed, but finds the note
        self.assertTrue(out["available"])


if __name__ == "__main__":
    unittest.main()
