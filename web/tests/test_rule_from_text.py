"""A rule from plain English (ai.rule_from_text): only column names reach the model; the rule is checked and test-run."""
import json
import unittest
from unittest.mock import patch

import ai
from checks import dialects, engine

COLS = [{"name": "customer_id", "type": "integer", "nullable": False}, {"name": "email", "type": "text", "nullable": True},
        {"name": "amount", "type": "numeric", "nullable": True}, {"name": "status", "type": "text", "nullable": True}]
PG = dialects.DIALECTS["postgresql"]
PASS = {"status": "PASS", "observed": 0, "message": "No rows returned.", "sql": "x", "details": {}}


def run(text, model=None, provider="gemini"):
    calls = {}

    def grounded(kind, subject, evidence, system, prompt, refresh=False):
        calls["evidence"] = evidence
        if isinstance(model, Exception):
            raise model
        return {**model, "model": "gemini · test"}

    with patch.object(engine, "columns", return_value=COLS), patch.object(engine, "_dialect", return_value=PG), \
            patch.object(engine, "run", return_value=dict(PASS)) as r, patch.object(ai.llm, "provider", return_value=provider), \
            patch.object(ai.llm, "grounded", side_effect=grounded):
        out = ai.rule_from_text(1, None, "silver", "customers", text)
    return out, calls, r


class FromText(unittest.TestCase):
    def test_the_model_sees_column_names_and_types_only(self):
        model = {"name": "email is gmail", "check_type": "custom_sql", "severity": "MEDIUM",
                 "config": {"sql": "SELECT customer_id, email FROM \"silver\".\"customers\" WHERE email NOT LIKE '%@gmail.com'", "mode": "no_rows"},
                 "understood": "Every email ends with @gmail.com.", "assumptions": ["case-sensitive"], "why": "w"}
        out, calls, r = run("every email ends with @gmail.com", model)
        ev = calls["evidence"]
        self.assertEqual(set(ev), {"table", "fully_qualified_table", "database_type", "columns", "request"})
        self.assertEqual(ev["columns"][1], {"name": "email", "type": "text"})                # no values, no profile
        self.assertTrue(out["ok"])
        self.assertEqual(out["rule"]["check_type"], "custom_sql")
        self.assertEqual(out["preview"]["status"], "PASS")
        r.assert_called_once()                                                              # test-run once, nothing saved

    def test_a_refusal_is_passed_on(self):
        out, _, r = run("delete the bad emails", {"cannot": "That changes data; describe what good data looks like instead."})
        self.assertFalse(out["ok"])
        self.assertIn("changes data", out["reason"])
        r.assert_not_called()

    def test_an_unknown_column_is_refused_and_not_run(self):
        out, _, r = run("phone is unique", {"name": "n", "check_type": "uniqueness", "config": {"columns": ["phone"]}, "severity": "HIGH"})
        self.assertFalse(out["ok"])
        self.assertIn("phone", out["reason"])
        r.assert_not_called()

    def test_a_query_on_another_table_is_refused(self):
        out, _, r = run("revenue is never negative", {"name": "n", "check_type": "custom_sql", "severity": "LOW",
                              "config": {"sql": "SELECT * FROM gold.revenue WHERE 1=0", "mode": "no_rows"}})
        self.assertFalse(out["ok"])
        r.assert_not_called()

    def test_no_model_matches_common_phrasings(self):
        cases = {"customer_id is unique": ("uniqueness", None), "status is never empty": ("null_rate", None),
                 "every email ends with @gmail.com": ("custom_sql", "NOT LIKE '%@gmail.com'"),
                 "amount is positive": ("custom_sql", '"amount" <= 0'), "amount must be at least 10": ("custom_sql", '"amount" < 10')}
        for text, (kind, sql) in cases.items():
            out, _, _ = run(text, provider="none")
            self.assertTrue(out["ok"], text)
            self.assertEqual(out["rule"]["check_type"], kind, text)
            if sql:
                self.assertIn(sql, out["rule"]["config"]["sql"], text)

    def test_a_model_outage_falls_back_to_phrasings(self):
        out, _, _ = run("customer_id is unique", ai.llm.Unavailable("quota"))
        self.assertTrue(out["ok"])
        self.assertIn("did not answer", out["note"])

    def test_literals_are_escaped(self):
        r = ai.phrase_rule("email ends with o'brien_%x", COLS, "silver", "customers", PG)
        self.assertIn("NOT LIKE '%o''brien\\_\\%x' ESCAPE '\\'", r["config"]["sql"])

    def test_vague_text_without_a_model_says_how_to_write_it(self):
        out, _, _ = run("make the data good", provider="none")
        self.assertFalse(out["ok"])
        self.assertIn("ends with @gmail.com", out["reason"])

    def test_too_short_or_long(self):
        with self.assertRaises(ValueError):
            run("abc", provider="none")


if __name__ == "__main__":
    unittest.main()
