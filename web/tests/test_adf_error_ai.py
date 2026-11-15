"""ADF errors no rule recognises, explained by the AI model: masked, asked once per distinct error, rules first."""
import unittest
from unittest.mock import patch

import ai
import alerts
from adf import diagnose

UNKNOWN = ("ErrorCode=ParquetJavaInvocationException,'An error occurred when invoking java, message: "
           "java.lang.OutOfMemoryError:Java heap space. Activity ID: 0f4b8a2e-1c3d-4e5f-9a8b-7c6d5e4f3a2b'")
ANSWER = {"plain": "The integration runtime machine has no Java.", "where": "pipeline settings",
          "likely_cause": "Java is not installed on the self-hosted IR machine.", "fixes": ["Install a 64-bit JRE"], "retry_helps": False}


def cause(msg=UNKNOWN, code="ParquetJavaInvocationException"):
    return diagnose.classify(code, msg, "UserError", "Copy")


class AdfErrorAi(unittest.TestCase):
    def test_an_error_the_rules_know_is_not_sent_to_the_model(self):
        known = cause('column "PAYMENT_ID" of relation "payments" does not exist', None)
        self.assertEqual(known["cause_id"], "column_missing")
        with patch.object(ai.llm, "provider", return_value="gemini"), patch.object(ai.llm, "grounded") as g:
            self.assertIsNone(ai.explain_adf_error(known))
        g.assert_not_called()

    def test_an_unknown_error_is_explained_once_and_masked(self):
        c = cause()
        self.assertIn(c["cause_id"], ai.GENERIC_CAUSES)
        with patch.object(ai.llm, "provider", return_value="gemini"), \
                patch.object(ai.llm, "grounded", return_value={**ANSWER, "model": "gemini · x"}) as g:
            out = ai.explain_adf_error(c)
        self.assertEqual(out["plain"], ANSWER["plain"])
        ev = g.call_args[0][2]
        self.assertNotIn("0f4b8a2e", ev["message"])                                   # run-specific id removed: one answer per error
        self.assertIn("<id>", ev["message"])

    def test_masking_keeps_names_but_hides_values(self):
        m = ai.mask_error("Value 'jane@corp.example' in column 'email' at 2026-11-12T10:17:29Z row 1234567 is invalid "
                          "for table bronze.customers; data: 'Jane Doe'")
        self.assertNotIn("jane@", m)
        self.assertNotIn("Jane Doe", m)
        self.assertNotIn("1234567", m)
        self.assertNotIn("2026-11-12", m)
        self.assertIn("bronze.customers", m)
        self.assertIn("'email'", m)

    def test_cache_only_never_calls_the_model(self):
        with patch.object(ai.llm, "provider", return_value="gemini"), patch.object(ai.llm, "cached", return_value=None) as cached, \
                patch.object(ai.llm, "grounded") as g:
            self.assertIsNone(ai.explain_adf_error(cause(), call=False))
        g.assert_not_called()
        cached.assert_called_once()

    def test_no_model_or_model_down(self):
        with patch.object(ai.llm, "provider", return_value="none"):
            self.assertIsNone(ai.explain_adf_error(cause()))
        with patch.object(ai.llm, "provider", return_value="gemini"), \
                patch.object(ai.llm, "grounded", side_effect=ai.llm.Unavailable("quota")), patch.object(ai.llm, "cached", return_value=None):
            self.assertIsNone(ai.explain_adf_error(cause()))

    def test_the_chat_card_carries_it_for_high_and_critical_only(self):
        inc = {"id": "adf:3:PL:RUN_FAILED:r1", "severity": "HIGH", "title": "PL failed", "summary": "s", "adf": {"cause": cause()}}
        with patch.object(ai, "explain_adf_error", return_value=ANSWER), patch.object(alerts, "public_url", return_value="https://dt.example"):
            self.assertIn("In plain words (AI)", str(alerts.incident_card(inc, [])))
            self.assertNotIn("In plain words", str(alerts.incident_card({**inc, "severity": "MEDIUM"}, [])))


if __name__ == "__main__":
    unittest.main()
