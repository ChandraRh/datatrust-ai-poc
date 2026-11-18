import unittest
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

import dq
import history
import history_routes
from estate import routes as estate_routes

ACCESS = [{"kind": "access", "ref": "#7", "what": "Grant: analyst_priya (POC · sales)", "detail": "monthly report",
           "status": "REJECTED", "requested_by": "priya", "requested_at": "2026-11-10 09:00:00",
           "decided_by": "admin", "decided_at": "2026-11-10 10:00:00", "link": "estate-access"}]
REINSERT = [{"kind": "reinsert", "ref": "#3", "what": "Re-insert 9 payments record(s) with currency → INR", "detail": "",
             "status": "APPLIED", "requested_by": "ravi", "requested_at": "2026-11-12 08:00:00",
             "decided_by": "admin", "decided_at": "2026-11-12 08:30:00", "link": "estate-dq-rejected"},
            {"kind": "reinsert", "ref": "#4", "what": "Re-insert 1 orders record(s)", "detail": "",
             "status": "PENDING", "requested_by": "ravi", "requested_at": "2026-11-13 08:00:00",
             "decided_by": None, "decided_at": None, "link": "estate-dq-rejected"}]
REMEDIATION = [{"kind": "remediation", "ref": "RECON-2026-10-08", "what": "Revenue gap on 2026-10-08", "detail": "auto-fix",
                "status": "APPROVED", "requested_by": "admin", "requested_at": "2026-11-01 12:00:00",
                "decided_by": "admin", "decided_at": "2026-11-01 12:00:00", "link": "verify"}]


@contextmanager
def sources(access=ACCESS, reinsert=REINSERT, remediation=REMEDIATION):
    copy = lambda rows: (lambda: [dict(r) for r in rows])   # noqa: E731  each call gets fresh dicts, like a real read
    with patch.object(history, "_access", side_effect=access if callable(access) else copy(access)), \
            patch.object(history, "_reinsert", side_effect=copy(reinsert)), \
            patch.object(history, "_remediation", side_effect=copy(remediation)):
        yield


class ApprovalsTests(unittest.TestCase):
    def test_all_decisions_in_one_list_newest_first(self):
        with sources():
            out = history.approvals()
        self.assertEqual([r["ref"] for r in out["rows"]], ["#4", "#3", "#7", "RECON-2026-10-08"])
        self.assertEqual(out["counts"], {"pending": 1, "approved": 2, "rejected": 1, "failed": 0})
        self.assertEqual(out["rows"][2]["decision"], "rejected")
        self.assertEqual(out["rows"][2]["kind_label"], "Access request")

    def test_filters_by_decision_person_dates_and_text(self):
        with sources():
            self.assertEqual([r["ref"] for r in history.approvals(decision="rejected")["rows"]], ["#7"])
            self.assertEqual([r["ref"] for r in history.approvals(person="PRIYA")["rows"]], ["#7"])
            self.assertEqual([r["ref"] for r in history.approvals(date_from="2026-11-11", date_to="2026-11-12")["rows"]], ["#3"])
            self.assertEqual([r["ref"] for r in history.approvals(q="currency")["rows"]], ["#3"])

    def test_kind_is_a_tab_the_tiles_and_tab_counts_stay_whole(self):
        with sources():
            out = history.approvals(kind="remediation")
        self.assertEqual([r["kind"] for r in out["rows"]], ["remediation"])
        self.assertEqual(out["by_kind"], {"access": 1, "reinsert": 2, "remediation": 1})   # every tab's count
        self.assertEqual(out["totals"]["approved"], 2)                                     # tiles: all kinds
        with sources():
            self.assertEqual(sorted({r["kind"] for r in history.approvals(kind="access,reinsert")["rows"]}), ["access", "reinsert"])
        with self.assertRaises(ValueError):
            history.approvals(kind="nonsense")

    def test_tab_counts_follow_the_other_filters(self):
        with sources():
            out = history.approvals(decision="approved")
        self.assertEqual(out["by_kind"], {"access": 0, "reinsert": 1, "remediation": 1})

    def test_waiting_lists_every_pending_request_whatever_the_filters(self):
        with sources():
            out = history.approvals(kind="remediation", decision="rejected")
        self.assertEqual([r["ref"] for r in out["waiting"]], ["#4"])
        self.assertEqual(out["rows"], [])

    def test_one_broken_source_does_not_hide_the_others(self):
        def broken():
            raise RuntimeError("relation portal.access_requests does not exist")
        with sources(access=broken):
            out = history.approvals()
        self.assertEqual(len(out["rows"]), 3)
        self.assertIn("Access request", out["errors"][0])

    def test_csv_has_readable_headers_and_one_line_per_row(self):
        with sources():
            rows = history.approvals(kind="access")["rows"]
        text = history.to_csv(rows, history_routes.APPROVAL_COLUMNS)
        lines = text.strip().splitlines()
        self.assertEqual(len(lines), 2)
        self.assertTrue(lines[0].startswith("Type,Reference,What,Decision,Outcome"))
        self.assertIn("Access request,#7", lines[1])
        self.assertIn("Rejected", lines[1])

    def test_decision_label_and_outcome_follow_the_kind(self):
        with sources():
            rows = {r["ref"]: r for r in history.approvals()["rows"]}
        self.assertEqual(rows["#3"]["decision_label"], "Approved")
        self.assertEqual(rows["#3"]["outcome"], "rows loaded")
        self.assertEqual(rows["#4"]["decision_label"], "Waiting")
        self.assertEqual(rows["RECON-2026-10-08"]["outcome"], "plan approved; people do the steps")

    def test_self_approval_is_flagged_but_not_for_incident_fixes(self):
        own = [dict(ACCESS[0], ref="#8", status="APPLIED", requested_by="Admin", decided_by="admin")]
        with sources(access=own):
            out = history.approvals()
        rows = {r["ref"]: r for r in out["rows"]}
        self.assertTrue(rows["#8"]["self_approved"])
        self.assertFalse(rows["#3"]["self_approved"])                  # ravi asked, admin decided
        self.assertFalse(rows["RECON-2026-10-08"]["self_approved"])    # an admin approves fixes directly, by design
        self.assertEqual(out["totals"]["self_approved"], 1)

    def test_totals_ignore_the_filters(self):
        with sources():
            out = history.approvals(decision="rejected", person="priya")
        self.assertEqual(len(out["rows"]), 1)
        self.assertEqual(out["totals"]["pending"], 1)
        self.assertEqual(out["totals"]["approved"], 2)
        self.assertEqual(out["totals"]["pending_by_kind"], {"access": 0, "reinsert": 1, "remediation": 0})


class RouteTests(unittest.TestCase):
    def client(self, role):
        app = FastAPI()
        app.include_router(history_routes.router)
        app.dependency_overrides[estate_routes.current_user] = lambda: {"username": "u", "role": role}
        return TestClient(app)

    def test_viewer_is_refused(self):
        self.assertEqual(self.client("viewer").get("/api/estate/history/approvals").status_code, 403)
        self.assertEqual(self.client("viewer").get("/api/estate/history/approvals.csv").status_code, 403)

    def test_activity_log_is_not_exposed(self):
        self.assertEqual(self.client("admin").get("/api/estate/history/activity").status_code, 404)

    def test_admin_gets_approvals_and_csv(self):
        with sources():
            c = self.client("admin")
            self.assertEqual(len(c.get("/api/estate/history/approvals").json()["rows"]), 4)
            r = c.get("/api/estate/history/approvals.csv?decision=approved")
        self.assertEqual(r.status_code, 200)
        self.assertIn("attachment", r.headers["content-disposition"])
        self.assertEqual(len(r.text.strip().splitlines()), 3)          # header + 2 approved


class ReinsertAuditTests(unittest.TestCase):
    REQ = {"request_id": 5, "entity": "payments", "status": "PENDING", "requested_by": "ravi"}

    def test_rejecting_a_reinsert_request_is_audited(self):
        conn = MagicMock()
        with patch.object(dq.ds, "one", return_value=dict(self.REQ)), \
                patch.object(dq.ds, "connect", return_value=conn), \
                patch("estate.store.audit") as audit:
            dq.decide(5, "reject", "admin")
        audit.assert_called_once_with("admin", "REINSERT_REJECTED", "request 5 payments", "requested by ravi")

    def test_approving_a_reinsert_request_is_audited_with_its_result(self):
        with patch.object(dq.ds, "one", return_value=dict(self.REQ)), \
                patch.object(dq, "_apply", return_value={"status": "APPLIED", "result": "9 payments record(s) loaded", "loaded": 9}), \
                patch("estate.store.audit") as audit:
            dq.decide(5, "approve", "admin")
        audit.assert_called_once_with("admin", "REINSERT_APPLIED", "request 5 payments", "9 payments record(s) loaded")


if __name__ == "__main__":
    unittest.main()
