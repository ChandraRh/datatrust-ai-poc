"""Why an incident is gone: only a fix, a successful rerun with the same rule or a passing re-check counts as verified."""
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

import resolution as R

FIRST = datetime(2026, 11, 13, 8, 0, tzinfo=timezone.utc)
MARKS = {"batch": "B1@2026-11-13 07:00:00", "correction": 10, "rule_change": 5}
DQ = {"incident_id": "DQ-payments-CURRENCY_REQUIRED-2026-10-03", "first_seen": FIRST, "marks": MARKS,
      "meta": {"type": "REJECTED_ROWS", "entity": "payments", "rule_name": "CURRENCY_REQUIRED", "business_date": "2026-10-03"}}
NEW_OK = {"batch_id": "B2", "sig": "B2@2026-11-13 09:00:00", "layers": {"BRONZE": "SUCCESS", "SILVER": "SUCCESS", "GOLD": "SUCCESS"}}


def poc(rule_change=None, corrections=None, batch=None, active=True):
    """Patch the POC's logs as they stand when the incident has disappeared."""
    return [patch.object(R, "_rule_change", return_value=rule_change or {}),
            patch.object(R, "_corrections", return_value=corrections or {}),
            patch.object(R, "_latest_batch", return_value=batch or {"batch_id": "B1", "sig": MARKS["batch"], "layers": {"SILVER": "SUCCESS"}}),
            patch.object(R.ds, "table_exists", return_value=True),
            patch.object(R.ds, "one", return_value={"active": active} if active is not None else {})]


def run(row, patches, open_now=()):
    for p in patches:
        p.start()
    try:
        return R.classify(row, [{"id": i} for i in open_now])
    finally:
        for p in patches:
            p.stop()


class ResolutionTests(unittest.TestCase):
    def test_rows_corrected_by_datatrust_are_verified(self):
        reason, detail, _ = run(DQ, poc(corrections={"n": 2, "by": "admin"}))
        self.assertEqual(reason, "fixed_by_datatrust")
        self.assertIn("approved by admin", detail)
        self.assertTrue(R.label(reason)[1])

    def test_newer_successful_run_with_the_same_rule_is_verified(self):
        reason, detail, _ = run(DQ, poc(batch=NEW_OK))
        self.assertEqual(reason, "pipeline_rerun")
        self.assertIn("B2", detail)

    def test_rule_changed_after_opening_is_not_verified(self):
        reason, _, _ = run(DQ, poc(rule_change={"rule_name": "CURRENCY_REQUIRED", "change": "switched off", "changed_by": "x"},
                                   corrections={"n": 2}, batch=NEW_OK))
        self.assertEqual(reason, "rule_changed")                  # a rule change wins over a rerun: the rows were not fixed
        self.assertFalse(R.label(reason)[1])

    def test_rule_switched_off_or_deleted_is_not_verified(self):
        self.assertEqual(run(DQ, poc(active=False, batch=NEW_OK))[0], "rule_changed")
        self.assertEqual(run(DQ, poc(active=None, batch=NEW_OK))[0], "rule_changed")

    def test_nothing_recorded_is_not_verified(self):
        self.assertEqual(run(DQ, poc())[0], "unexplained")       # same batch, no correction, no rule change

    def test_old_incident_without_meta_reads_its_id(self):
        row = {**DQ, "meta": None}
        reason, detail, _ = run(row, poc(batch=NEW_OK))
        self.assertEqual(reason, "pipeline_rerun")
        self.assertIn("CURRENCY_REQUIRED", detail)

    def test_failed_layer_in_a_newer_run_is_superseded_not_resolved(self):
        row = {"incident_id": "RUN-B1-SILVER", "first_seen": FIRST, "marks": MARKS, "meta": {"type": "PIPELINE_FAILURE"}}
        with patch.object(R, "_latest_batch", return_value={}):
            reason, _, newer = R.classify(row, [{"id": "RUN-B2-SILVER"}])
        self.assertEqual((reason, newer), ("superseded", "RUN-B2-SILVER"))

    def test_failed_layer_that_succeeds_later_is_a_rerun(self):
        row = {"incident_id": "RUN-B1-SILVER", "first_seen": FIRST, "marks": MARKS, "meta": {"type": "PIPELINE_FAILURE"}}
        with patch.object(R, "_latest_batch", return_value=NEW_OK):
            self.assertEqual(R.classify(row, [])[0], "pipeline_rerun")

    def test_data_check(self):
        row = {"incident_id": "db5:CHECK-7", "first_seen": FIRST, "marks": {}, "meta": {"type": "CHECK_FAILED"}}
        base = {"check_type": "uniqueness", "enabled": True, "edited_after": False, "accepted_after": None,
                "status": "PASS", "ran_after": True, "run_at": "2026-11-13 09:00", "observed": 0, "expected": "unique"}
        cases = [({**base}, "check_passes"), ({**base, "edited_after": True}, "rule_changed"), ({**base, "enabled": False}, "rule_changed"),
                 ({}, "rule_changed"), ({**base, "ran_after": False}, "unexplained"),
                 ({**base, "check_type": "schema", "accepted_after": True}, "accepted")]
        for c, want in cases:
            with patch.object(R.appdb, "one", return_value=c):
                self.assertEqual(R.classify(row, [])[0], want, c)

    def test_adf_issue_seen_again_in_a_newer_run_is_superseded(self):
        row = {"incident_id": "adf:3:PL_SALES:ACTIVITY_FAILED:run-1", "first_seen": FIRST, "marks": {}, "meta": {}}
        reason, _, newer = R.classify(row, [{"id": "adf:3:PL_SALES:ACTIVITY_FAILED:run-2"}])
        self.assertEqual((reason, newer), ("superseded", "adf:3:PL_SALES:ACTIVITY_FAILED:run-2"))
        with patch.object(R.appdb, "one", return_value={"run_id": "run-3", "run_start": "2026-11-13 10:00"}):
            self.assertEqual(R.classify(row, [])[0], "pipeline_rerun")


if __name__ == "__main__":
    unittest.main()
