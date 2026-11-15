"""Time to fix (overdue) and grouping of incidents that share a cause; Google Chat: one card per cause, one overdue reminder."""
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import alerts
import builtin
import scopes

NOW = datetime(2026, 11, 16, 12, 0, tzinfo=timezone.utc)
CFG = {"enabled": True, "severity": "MEDIUM", "critical_h": 4, "high_h": 24, "medium_h": 72, "low_h": 168}


def inc(iid, sev, opened_h_ago, **kw):
    return {"id": iid, "severity": sev, "title": iid, "summary": "", "first_seen": (NOW - timedelta(hours=opened_h_ago)).isoformat(), **kw}


class TimeToFix(unittest.TestCase):
    def due(self, found, cfg=CFG):
        with patch.object(builtin, "setting", return_value=cfg):
            scopes.attach_due(found, NOW)
        return found

    def test_overdue_by_severity(self):
        a, b, c = self.due([inc("a", "CRITICAL", 5), inc("b", "CRITICAL", 3), inc("c", "MEDIUM", 5)])
        self.assertEqual((a["due"]["overdue"], b["due"]["overdue"], c["due"]["overdue"]), (True, False, False))
        self.assertEqual((a["due"]["target_h"], a["due"]["left_h"], a["due"]["open_h"]), (4, -1.0, 5.0))

    def test_switched_off_means_no_targets(self):
        (a,) = self.due([inc("a", "CRITICAL", 50)], {**CFG, "enabled": False})
        self.assertIsNone(a["due"])

    def test_no_first_seen_yet_means_no_target(self):
        (a,) = self.due([{"id": "a", "severity": "HIGH", "first_seen": None}])
        self.assertIsNone(a["due"])


class Groups(unittest.TestCase):
    def test_same_run_same_table_same_date(self):
        found = [
            {"id": "adf:3:PL:CHECK_FAILED.X.a:r1", "severity": "HIGH", "adf": {"pipeline": "PL", "run_id": "r1"}},
            {"id": "adf:3:PL:CHECK_FAILED.Y.b:r1", "severity": "MEDIUM", "adf": {"pipeline": "PL", "run_id": "r1"}},
            {"id": "adf:3:PL2:RUN_FAILED:r9", "severity": "HIGH", "adf": {"pipeline": "PL2", "run_id": "r9"}},
            {"id": "db3:CHECK-1", "severity": "HIGH", "metrics": {"target": "sales.orders"}},
            {"id": "db3:CHECK-2", "severity": "LOW", "metrics": {"target": "sales.orders"}},
            {"id": "RECON-2026-10-27", "severity": "CRITICAL", "business_date": "2026-10-27", "metrics": {}},
            {"id": "DQ-payments-STATUS_VALID-2026-10-27", "severity": "MEDIUM", "business_date": "2026-10-27", "metrics": {}},
        ]
        found.sort(key=lambda i: scopes.SEV_ORDER[i["severity"]])
        scopes.attach_groups(found)
        g = {i["id"]: i["group"] for i in found}
        self.assertEqual(g["adf:3:PL:CHECK_FAILED.X.a:r1"]["key"], g["adf:3:PL:CHECK_FAILED.Y.b:r1"]["key"])
        self.assertEqual(g["adf:3:PL:CHECK_FAILED.Y.b:r1"]["why"], "same run of PL")
        self.assertIsNone(g["adf:3:PL2:RUN_FAILED:r9"])                         # alone: no group
        self.assertEqual(g["db3:CHECK-2"]["why"], "same table sales.orders")
        self.assertEqual(g["DQ-payments-STATUS_VALID-2026-10-27"]["lead"], "RECON-2026-10-27")  # the most severe leads
        self.assertEqual(g["RECON-2026-10-27"]["size"], 2)


class ChatCards(unittest.TestCase):
    def test_new_incidents_of_one_cause_go_out_as_one_card(self):
        grp = {"key": "run:PL:r1", "why": "same run of PL"}
        new = [{"id": "a", "scope": "adf:3", "group": grp}, {"id": "b", "scope": "adf:3", "group": grp},
               {"id": "c", "scope": "adf:3", "group": None}, {"id": "d", "scope": "adf:4", "group": grp}]
        self.assertEqual([[x["id"] for x in b] for b in alerts._batches(new)], [["a", "b"], ["c"], ["d"]])  # other source: own card

    def test_the_card_lists_the_related_ones(self):
        lead = {"id": "a", "severity": "HIGH", "title": "A", "summary": "s", "group": {"why": "same run of PL"},
                "due": {"target_h": 24}}
        with patch.object(alerts, "public_url", return_value="https://dt.example"):
            card = alerts.incident_card(lead, [], [{"severity": "MEDIUM", "title": "B"}])
        text = str(card)
        self.assertIn("1 related incident(s)", text)
        self.assertIn("same run of PL", text)
        self.assertIn("24 h for high incidents", text)

    def test_overdue_reminder_text_names_target_and_owner(self):
        i = {"id": "a", "severity": "CRITICAL", "title": "Revenue gap", "due": {"open_h": 6, "target_h": 4}}
        with patch.object(alerts, "public_url", return_value="https://dt.example"):
            t = alerts.overdue_text(i, [{"name": "Chandra"}])
            self.assertIn("Open 6 h; the time to fix for critical incidents is 4 h. Owner: Chandra", t)
            self.assertIn("nobody yet", alerts.overdue_text(i, []))

    def test_first_run_sums_up_what_is_overdue_already_instead_of_a_burst(self):
        class Conn:
            def __init__(self): self.sql = []
            def execute(self, q, p=None):
                self.sql.append(q)
                return type("R", (), {"fetchone": lambda s: {"incident_id": "x"}})()
            def commit(self): pass
        current = {f"i{n}": {"id": f"i{n}", "severity": "HIGH", "title": f"t{n}", "due": {"overdue": True, "open_h": 30, "target_h": 24}}
                   for n in range(8)}
        conn, sent = Conn(), []
        with patch.object(alerts, "send", side_effect=lambda m, thread=None: sent.append(m)), \
                patch.object(alerts, "public_url", return_value="https://dt.example"):
            out = alerts._overdue_reminders(conn, current, {}, [])
        self.assertEqual(out, [])                                                 # no reminder each
        self.assertEqual(len(sent), 1)                                            # one summary
        self.assertIn("8 incident(s) are already past their target", str(sent[0]))

    def test_one_reminder_per_incident_never_repeated(self):
        class Conn:
            def execute(self, q, p=None): return type("R", (), {"fetchone": lambda s: {"incident_id": "a"}})()
            def commit(self): pass
        cur = {"a": {"id": "a", "severity": "HIGH", "title": "t", "due": {"overdue": True, "open_h": 30, "target_h": 24}},
               "b": {"id": "b", "severity": "HIGH", "title": "t", "due": {"overdue": True, "open_h": 30, "target_h": 24}}}
        known = {alerts.OVERDUE_BASELINE: {}, "a": {"overdue_alerted_at": None, "resolved_alerted_at": None},
                 "b": {"overdue_alerted_at": "2026-11-16 10:00", "resolved_alerted_at": None}}
        sent = []
        with patch.object(alerts, "send", side_effect=lambda m, thread=None: sent.append(thread)), \
                patch.object(alerts, "public_url", return_value="https://dt.example"), patch.object(alerts, "_owners", return_value=[]):
            out = alerts._overdue_reminders(Conn(), cur, known, [])
        self.assertEqual((out, sent), (["a"], ["a"]))                             # b was reminded before: not again


if __name__ == "__main__":
    unittest.main()
