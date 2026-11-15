"""Owners per source / table / pipeline (owners.py): the most specific wins; the Connect word match is only a fallback."""
import unittest
from unittest.mock import patch

import owners


def a(scope, target, pid, name):
    return {"scope": scope, "target": target, "person_id": pid, "name": name, "email": f"{name.lower()}@co.example", "chat_link": None}


ROWS = [a("adf:3", "", 1, "Asha"), a("adf:3", "PL_01_File_Monitoring", 2, "Ravi"), a("db:3", "sales.orders", 3, "Meera")]


class OwnerTests(unittest.TestCase):
    def test_the_pipeline_owner_wins_over_the_source_owner(self):
        inc = {"id": "adf:3:PL_01_File_Monitoring:RUN_FAILED:r1", "adf": {"pipeline": "PL_01_File_Monitoring"}, "metrics": {}}
        o = owners.for_incident(inc, "adf:3", ROWS)
        self.assertEqual([(x["name"], x["why"], x["guess"]) for x in o], [("Ravi", "owns pipeline PL_01_File_Monitoring", False)])

    def test_another_pipeline_falls_back_to_the_source_owner(self):
        inc = {"id": "adf:3:PL_02:RUN_FAILED:r1", "adf": {"pipeline": "PL_02"}, "metrics": {}}
        self.assertEqual([x["name"] for x in owners.for_incident(inc, "adf:3", ROWS)], ["Asha"])

    def test_a_table_owner_for_a_rule_incident(self):
        inc = {"id": "db3:CHECK-9", "metrics": {"target": "sales.orders"}}
        self.assertEqual([x["why"] for x in owners.for_incident(inc, "db:3", ROWS)], ["owns table sales.orders"])

    def test_owners_of_one_source_never_own_another(self):
        inc = {"id": "db5:CHECK-1", "metrics": {"target": "sales.orders"}, "title": "x", "summary": ""}
        with patch("estate.store.match_people", return_value=[]):
            self.assertEqual(owners.for_incident(inc, "db:5", ROWS), [])  # Meera owns sales.orders in db:3, not db:5

    def test_nobody_set_uses_the_connect_guess_marked_as_a_guess(self):
        inc = {"id": "db5:CHECK-1", "metrics": {"target": "custom query"}, "title": "payments late", "summary": ""}
        guess = [{"person_id": 7, "name": "Kiran", "email": "k@co.example", "matched": ["payments"]}]
        with patch("estate.store.match_people", return_value=guess):
            o = owners.for_incident(inc, "db:5", [])
        self.assertEqual([(x["name"], x["guess"]) for x in o], [("Kiran", True)])

    def test_what_an_incident_is_about(self):
        self.assertEqual(owners.target_of({"id": "adf:3:PL:RUN_FAILED:r", "adf": {"pipeline": "PL"}}), ("PL", "pipeline"))
        self.assertEqual(owners.target_of({"id": "db3:CHECK-1", "metrics": {"target": "sales.orders"}}), ("sales.orders", "table"))
        self.assertEqual(owners.target_of({"id": "db3:CHECK-1", "metrics": {"target": "custom query"}}), (None, ""))
        self.assertEqual(owners.target_of({"id": "DQ-x", "metrics": {"entity": "payments"}}), ("silver.payments", "table"))

    def test_bad_input_is_refused(self):
        with self.assertRaisesRegex(ValueError, "source key"):
            owners.set_owner("sales", "", 1, "admin")
        with patch.object(owners.appdb, "one", return_value={}):
            with self.assertRaisesRegex(ValueError, "people directory"):
                owners.set_owner("db:3", "", 999, "admin")


if __name__ == "__main__":
    unittest.main()
