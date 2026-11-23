"""Two databases on one server (the Sales DB and the POC on pg-sales-hv): an ADF target is checked through the connection
for the database the linked service names, not whichever connection to that host comes first."""
import unittest
from unittest.mock import patch

from adf import probes
from checks import coverage
from estate import store as estate_store

HOST = "pg-sales-hv.postgres.database.azure.com"
CONNS = [
    {"connection_id": 1, "name": "DataTrust POC", "source_type": "postgresql", "host": HOST, "database": "datatrust_poc"},
    {"connection_id": 2, "name": "Sales DB (Azure)", "source_type": "postgresql", "host": HOST.upper(), "database": "salesdb"},
]


class SharedServerTests(unittest.TestCase):
    def test_coverage_picks_the_named_database(self):
        self.assertEqual(coverage._connection_for({"host": HOST, "database": "salesdb"}, {}, CONNS)["connection_id"], 2)
        self.assertEqual(coverage._connection_for({"host": HOST, "database": "datatrust_poc"}, {}, CONNS)["connection_id"], 1)
        self.assertEqual(coverage._connection_for({"host": HOST}, {}, CONNS)["connection_id"], 1)       # no database: as before

    def test_probes_pick_the_named_database(self):
        with patch.object(estate_store, "list_connections", return_value=CONNS), \
                patch("estate.postgres_connector.PostgresSource", side_effect=lambda c: c):
            src, db, via = probes.find_pg_connection({"host": HOST, "database": "salesdb"}, {})
        self.assertEqual((src["connection_id"], db), (2, "salesdb"))
        self.assertIn("Sales DB", via)


if __name__ == "__main__":
    unittest.main()
