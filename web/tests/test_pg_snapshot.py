"""Change tracking without a superuser (web/estate/pg_snapshot.py): the differences between two catalog snapshots,
written as the statements the schema-change guard already understands."""
import json
import unittest

import schema_guard
from estate import pg_snapshot as snap


def rel(schema, name, cols, kind="r"):
    return {"schema_name": schema, "object_name": name, "kind": kind,
            "columns": json.dumps([[i + 1, c, t, nn] for i, (c, t, nn) in enumerate(cols)])}


PAY = [("payment_id", "text", True), ("amount", "numeric", False), ("email", "text", False)]


class DiffTests(unittest.TestCase):
    def queries(self, before, now):
        return [c[4] for c in snap.diff(before, now)]

    def test_nothing_changed_logs_nothing(self):
        self.assertEqual(snap.diff({1: rel("silver", "payments", PAY)}, {1: rel("silver", "payments", PAY)}), [])

    def test_created_and_dropped(self):
        rows = snap.diff({1: rel("silver", "old", PAY)}, {2: rel("gold", "v_sales", PAY, "v")})
        self.assertIn(("DROP TABLE", "table", "silver", "silver.old", "DROP TABLE silver.old"), rows)
        self.assertIn(("CREATE VIEW", "view", "gold", "gold.v_sales", "CREATE VIEW gold.v_sales"), rows)

    def test_column_dropped_added_renamed_retyped(self):
        now = {1: {**rel("silver", "payments", []), "columns": json.dumps([
            [1, "payment_id", "text", True], [2, "amount", "integer", False], [4, "country", "text", False]])}}
        before = {1: {**rel("silver", "payments", []), "columns": json.dumps([
            [1, "payment_id", "text", True], [2, "amount", "numeric", False], [3, "email", "text", False]])}}
        self.assertEqual(self.queries(before, now), [
            "ALTER TABLE silver.payments ALTER COLUMN amount TYPE integer (was numeric)",
            "ALTER TABLE silver.payments DROP COLUMN email",
            "ALTER TABLE silver.payments ADD COLUMN country text"])
        renamed = {1: rel("silver", "payments", [("payment_id", "text", True), ("amount", "numeric", False), ("mail", "text", False)])}
        self.assertEqual(self.queries({1: rel("silver", "payments", PAY)}, renamed),
                         ["ALTER TABLE silver.payments RENAME COLUMN email TO mail"])

    def test_a_renamed_table_keeps_its_old_name_in_the_statement(self):
        self.assertEqual(self.queries({1: rel("silver", "payments", PAY)}, {1: rel("silver", "payments_v2", PAY)}),
                         ["ALTER TABLE silver.payments RENAME TO payments_v2"])

    def test_the_guard_reads_breaking_changes_from_these_statements(self):
        for q, label in [("ALTER TABLE silver.payments DROP COLUMN email", "column dropped"),
                         ("ALTER TABLE silver.payments RENAME COLUMN email TO mail", "column renamed"),
                         ("ALTER TABLE silver.payments ALTER COLUMN amount TYPE integer (was numeric)", "column type changed"),
                         ("ALTER TABLE silver.payments RENAME TO payments_v2", "renamed")]:
            found = next((l for p, l in schema_guard.BREAKING if __import__("re").search(p, q.lower())), None)
            self.assertEqual(found, label, q)
        q = "ALTER TABLE silver.payments ADD COLUMN country text".lower()
        self.assertFalse(any(__import__("re").search(p, q) for p, _ in schema_guard.BREAKING))


if __name__ == "__main__":
    unittest.main()
