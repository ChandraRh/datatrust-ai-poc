"""AI features: numbers must come from the evidence, errors are explained, suggestions follow the profile, tools respect sources."""
import os
import unittest
from unittest.mock import MagicMock, patch

import ai
import ask
import llm


class GroundingTests(unittest.TestCase):
    def test_numbers_not_in_the_evidence_are_flagged(self):
        evidence = {"settled": 1394, "silver": 1385, "gap": 6401.15, "date": "2026-10-30", "views": ["gold.daily_revenue"]}
        text = "Gateway settled 1,394 payments, silver has 1,385; gap ₹6,401.15 on 2026-10-30. About 4,200 customers are affected."
        self.assertEqual(llm.unverified_numbers(text, evidence), ["4,200"])
        self.assertEqual(llm.unverified_numbers("3 steps, last 24 hours", {"x": 1}), ["24"])
        self.assertEqual(llm.unverified_numbers("last 24 hours", {"e": {"x": 1}, "s": "within the last 24 hours"}), [])

    def test_no_model_means_unavailable_not_a_crash(self):
        with patch.dict(os.environ, {"LLM_PROVIDER": "none"}):
            with self.assertRaises(llm.Unavailable):
                llm.complete_json("s", "p")

    def test_quota_errors_are_explained(self):
        resp = MagicMock(status_code=429, text='{"message": "Quota exceeded ... limit: 20 ... requests per day"}')
        msg = llm.describe_error(type("E", (Exception,), {"response": resp})())
        self.assertIn("daily request limit", msg)
        self.assertIn("20 requests a day", msg)
        resp.status_code, resp.text = 403, "denied"
        self.assertIn("API key", llm.describe_error(type("E", (Exception,), {"response": resp})()))

    def test_grounded_answers_are_cached_by_evidence(self):
        with patch.dict(os.environ, {"LLM_PROVIDER": "gemini"}), patch.object(llm, "cached", return_value={"headline": "saved"}) as c, \
                patch.object(llm, "complete_json") as call:
            out = llm.grounded("explain", "X", {"a": 1}, "sys", "prompt")
        self.assertEqual(out["headline"], "saved")
        call.assert_not_called()
        c.assert_called_once()


class SuggestTests(unittest.TestCase):
    PROFILE = {"table": "sales.orders", "rows": 500, "columns": [
        {"column": "order_id", "type": "text", "nullable": False, "empty_pct": 0, "distinct": 500, "unique": True, "min": None, "max": None},
        {"column": "amount", "type": "numeric", "nullable": True, "empty_pct": 0, "distinct": 300, "unique": False, "min": 1, "max": 900},
        {"column": "business_date", "type": "date", "nullable": True, "empty_pct": 0, "distinct": 30, "unique": False, "min": "2026-10-01", "max": "2026-10-30"},
        {"column": "loaded_at", "type": "timestamp without time zone", "nullable": True, "empty_pct": 0, "distinct": 5, "unique": False,
         "min": "2026-11-01 10:00", "max": "2026-11-12 10:00"}]}

    def test_profile_suggestions_without_a_model(self):
        s = ai._heuristic(self.PROFILE)
        kinds = {x["check_type"]: x for x in s}
        self.assertEqual(kinds["freshness"]["config"]["column"], "loaded_at")      # the load timestamp, not the business date
        self.assertEqual(kinds["uniqueness"]["config"]["columns"], ["order_id"])
        self.assertIn("volume", kinds)
        self.assertIn("schema", kinds)
        self.assertEqual({x["config"]["column"] for x in s if x["check_type"] == "null_rate"}, {"amount", "business_date"})

    def test_model_proposals_that_cannot_run_are_dropped(self):
        bad = {"checks": [{"name": "ok", "check_type": "volume", "config": {"min_rows": 1}, "severity": "MEDIUM", "why": "w"},
                          {"name": "nonsense", "check_type": "telepathy", "config": {}, "severity": "LOW"},
                          {"name": "no column", "check_type": "freshness", "config": {}, "severity": "LOW"}]}
        with patch.object(ai, "_profile", return_value=dict(self.PROFILE)), patch("checks.engine._dialect") as d, \
                patch.object(llm, "provider", return_value="gemini"), patch.object(llm, "grounded", return_value={**bad, "model": "m"}):
            d.return_value.name = "postgresql"
            out = ai.suggest_checks(1, None, "sales", "orders")
        names = [x["name"] for x in out["suggestions"]]
        self.assertIn("ok", names)
        self.assertNotIn("nonsense", names)
        self.assertNotIn("no column", names)
        self.assertTrue(any(x["check_type"] == "freshness" for x in out["suggestions"]))  # the profile's own basics are kept


class AskToolTests(unittest.TestCase):
    def test_tools_refuse_the_wrong_kind_of_source(self):
        adf = {"key": "adf:3", "kind": "adf", "id": 3, "name": "Sales ADF", "type": "Azure Data Factory", "poc": False}
        with patch.object(ask.scopes, "resolve", return_value=adf):
            out = ask.runner_for("adf:3")("query_source", {"sql": "SELECT 1"})
        self.assertIn("needs a database source", out["error"])

    def test_query_tool_only_reads(self):
        db = {"key": "db:5", "kind": "db", "id": 5, "name": "Sales DB", "type": "PostgreSQL", "poc": False}
        src = MagicMock()
        src.cfg = {"database": "salesdb", "source_type": "postgresql"}
        with patch.object(ask.scopes, "resolve", return_value=db), patch("estate.service.source", return_value=src):
            out = ask.runner_for("db:5")("query_source", {"sql": "DELETE FROM sales.orders"})
        self.assertIn("SELECT", out["error"])


if __name__ == "__main__":
    unittest.main()
