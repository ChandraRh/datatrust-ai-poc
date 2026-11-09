import unittest
from unittest.mock import patch

import data_service as ds


class InvestigationTests(unittest.TestCase):
    def test_settlement_investigation_marks_missing_source_count_unavailable(self):
        incident = {
            "id": "RECON-2026-10-01",
            "type": "SETTLEMENT_MISMATCH",
            "business_date": "2026-10-01",
            "metrics": {
                "settled_payments": 12, "settled_amount": 120,
                "silver_payments": 10, "silver_amount": 100,
                "payment_difference": 2, "amount_difference": 20,
            },
            "rejected_explained": {"n": 2, "amount": 15, "rules": "AMOUNT_POSITIVE"},
        }
        with patch.object(ds, "incidents", return_value=[incident]), \
                patch.object(ds, "one", return_value={"n": 10}), \
                patch.object(ds, "_source_rows", return_value=None), \
                patch.object(ds, "rows", return_value=[]), \
                patch.object(ds, "impact", return_value={}):
            result = ds.investigate(incident["id"])

        self.assertIsNone(result["checks"][2]["ok"])
        self.assertIn("unavailable", result["checks"][2]["text"])
        self.assertIn("does not match", result["root_cause"])

    def test_rejected_rows_are_checked_against_current_count_and_raw_values(self):
        incident = {
            "id": "DQ-payments-AMOUNT_POSITIVE-2026-10-01",
            "type": "REJECTED_ROWS",
            "metrics": {"entity": "payments", "rule_name": "AMOUNT_POSITIVE",
                        "business_date": "2026-10-01", "n": 2},
        }
        observed = {"n": 2, "with_raw_record": 2}
        with patch.object(ds, "incidents", return_value=[incident]), \
                patch.object(ds, "rows", return_value=[{"raw_record": "{}"}]), \
                patch.object(ds, "one", return_value=observed), \
                patch.object(ds, "impact", return_value={}):
            result = ds.investigate(incident["id"])

        self.assertEqual([check["ok"] for check in result["checks"]], [True, True])
        self.assertIn("current query returns 2", result["checks"][0]["text"])
        self.assertNotIn("confidence", result)

    def test_data_loss_investigation_reports_unexplained_rows(self):
        incident = {
            "id": "LOSS-payments",
            "type": "DATA_LOSS",
            "metrics": {
                "source_file": "payments.csv", "source_rows": None,
                "bronze_table": "bronze.payments", "bronze_rows": 10,
                "silver_table": "silver.payments", "silver_loaded": 7,
                "rejected": 1, "duplicates": 1,
            },
        }
        with patch.object(ds, "incidents", return_value=[incident]), \
                patch.object(ds, "impact", return_value={}):
            result = ds.investigate(incident["id"])

        self.assertIsNone(result["checks"][0]["ok"])
        self.assertFalse(result["checks"][1]["ok"])
        self.assertIn("1 Bronze row(s)", result["root_cause"])


if __name__ == "__main__":
    unittest.main()
