"""Incident fixes need a request and an admin's approval (web/fix_requests.py): four-eyes, stale proposals, reasons."""
import unittest
from unittest.mock import patch

import fix_requests as F

PROPOSAL = {"available": True, "entity": "payments", "business_date": "2026-10-30", "title": "9 payments without a currency",
            "rows": [{"record_key": f"PAY{i}", "column": "currency", "current": None, "proposed": "INR"} for i in range(9)],
            "resolves": ["RECON-2026-10-30"]}
PENDING = {"request_id": 7, "incident_id": "DQ-payments-CURRENCY_REQUIRED-2026-10-30", "status": "PENDING", "rows": 9,
           "entity": "payments", "business_date": "2026-10-30", "requested_by": "asha", "fingerprint": F.fingerprint(PROPOSAL)}


class CanDecideTests(unittest.TestCase):
    def test_only_admins_decide(self):
        self.assertEqual(F.can_decide(PENDING, "ravi", "viewer")[0], False)

    def test_four_eyes_blocks_the_requester(self):
        with patch.object(F, "four_eyes", return_value=True):
            ok, why = F.can_decide(PENDING, "ASHA", "admin")
        self.assertFalse(ok)
        self.assertIn("another admin must approve", why)

    def test_another_admin_may_approve(self):
        with patch.object(F, "four_eyes", return_value=True):
            self.assertEqual(F.can_decide(PENDING, "ravi", "admin"), (True, ""))

    def test_self_approval_when_the_setting_is_off(self):
        with patch.object(F, "four_eyes", return_value=False):
            self.assertTrue(F.can_decide(PENDING, "asha", "admin")[0])

    def test_a_decided_request_cannot_be_decided_again(self):
        self.assertFalse(F.can_decide({**PENDING, "status": "APPLIED"}, "ravi", "admin")[0])


class DecideTests(unittest.TestCase):
    def run_decide(self, approve, by="ravi", note="", proposal=PROPOSAL, apply_result=None, apply_error=None):
        finished = {}

        def finish(rid, status, who, n, result=None):
            finished.update(status=status, by=who, note=n, result=result)
            return {**PENDING, "status": status, "decided_by": who}
        applied = []

        def apply(iid, who):
            applied.append((iid, who))
            if apply_error:
                raise apply_error
            return apply_result or {"fixed": 9, "resolves": proposal["resolves"], "action": "fixed"}
        with patch.object(F, "get", return_value=dict(PENDING)), patch.object(F, "_finish", side_effect=finish), \
                patch.object(F, "_audit"), patch.object(F, "four_eyes", return_value=True), \
                patch("autofix.proposal", return_value=proposal), patch("autofix.apply", side_effect=apply):
            try:
                out = F.decide(7, by, "admin", approve, note)
            except Exception as e:                                   # noqa: BLE001
                out = e
        return out, finished, applied

    def test_approve_applies_the_fix(self):
        out, fin, applied = self.run_decide(True)
        self.assertEqual((fin["status"], fin["by"], applied), ("APPLIED", "ravi", [(PENDING["incident_id"], "ravi")]))

    def test_the_requester_cannot_approve(self):
        out, fin, applied = self.run_decide(True, by="asha")
        self.assertIsInstance(out, ValueError)
        self.assertEqual((fin, applied), ({}, []))                   # nothing decided, nothing changed

    def test_changed_rows_make_the_request_stale(self):
        changed = {**PROPOSAL, "rows": PROPOSAL["rows"][:5]}         # a pipeline run fixed some rows meanwhile
        out, fin, applied = self.run_decide(True, proposal=changed)
        self.assertIsInstance(out, F.StaleRequest)
        self.assertEqual((fin["status"], applied), ("STALE", []))

    def test_a_failed_apply_is_recorded(self):
        out, fin, _ = self.run_decide(True, apply_error=RuntimeError("deadlock"))
        self.assertIsInstance(out, ValueError)
        self.assertEqual(fin["status"], "FAILED")
        self.assertIn("deadlock", fin["result"]["error"])

    def test_reject_needs_a_reason(self):
        out, fin, applied = self.run_decide(False, note="")
        self.assertIsInstance(out, ValueError)
        self.assertEqual(fin, {})
        out, fin, applied = self.run_decide(False, note="currency must come from the source system")
        self.assertEqual((fin["status"], applied), ("REJECTED", []))


class FingerprintTests(unittest.TestCase):
    def test_order_does_not_matter_but_values_do(self):
        rev = {**PROPOSAL, "rows": list(reversed(PROPOSAL["rows"]))}
        self.assertEqual(F.fingerprint(rev), F.fingerprint(PROPOSAL))
        other = {**PROPOSAL, "rows": [{**PROPOSAL["rows"][0], "proposed": "USD"}] + PROPOSAL["rows"][1:]}
        self.assertNotEqual(F.fingerprint(other), F.fingerprint(PROPOSAL))


class HistoryTests(unittest.TestCase):
    def test_requested_fixes_count_for_self_approval_and_are_listed_once(self):
        import history
        fixes = [{**PENDING, "request_id": 1, "status": "APPLIED", "requested_by": "asha", "decided_by": "asha",
                  "requested_at": "2026-11-26 10:00:00", "decided_at": "2026-11-26 10:05:00", "title": "t", "note": None,
                  "decision_note": None, "result": {"fixed": 9}},
                 {**PENDING, "request_id": 2, "status": "PENDING", "requested_by": "ravi", "decided_by": None,
                  "requested_at": "2026-11-26 11:00:00", "decided_at": None, "title": "t", "note": "month end",
                  "decision_note": None, "result": None}]
        log = [{"remediation_id": 5, "incident_id": PENDING["incident_id"], "incident_title": "t", "actions": "fixed",
                "approved_by": "asha", "note": "9 correction(s)", "status": "APPLIED", "approved_at": None,
                "tracked": None, "resolved_at": None, "resolution": None}]
        with patch.object(history, "_access", return_value=[]), patch.object(history, "_reinsert", return_value=[]), \
                patch("appdb.table_exists", return_value=True), patch("appdb.rows", side_effect=[log, []]), \
                patch("fix_requests.history_rows", return_value=fixes):
            d = history.approvals()
        fixes_listed = [r for r in d["rows"] if r["kind"] == "remediation"]
        self.assertEqual(len(fixes_listed), 2)                       # the remediation_log line of the applied fix is not repeated
        self.assertEqual(d["totals"]["self_approved"], 1)
        pending = next(r for r in d["waiting"] if r["kind"] == "remediation")
        self.assertEqual(pending["link"], f"impact={PENDING['incident_id']}")


if __name__ == "__main__":
    unittest.main()
