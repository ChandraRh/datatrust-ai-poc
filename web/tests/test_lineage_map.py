"""Lineage map (web/lineage): same table = same node across sources, SQL understanding, merge and the per-source view."""
import unittest
from unittest.mock import patch

from lineage import collect, graph, model, sql

SALES = ("pg-sales-hv.postgres.database.azure.com", "salesdb")


class IdsTests(unittest.TestCase):
    def test_a_table_seen_by_adf_and_by_the_connection_is_one_id(self):
        adf = model.dataset_id("postgresql", "PG-SALES-HV.postgres.database.azure.com:5432", "SalesDB", "bronze", '"payments"')
        conn = model.dataset_id("postgresql", "pg-sales-hv.postgres.database.azure.com", "salesdb", "Bronze", "payments")
        self.assertEqual(adf, conn)

    def test_hosts_are_normalised(self):
        self.assertEqual(model.norm_host("tcp:myserver,1433"), "myserver")
        self.assertEqual(model.norm_host("127.0.0.1"), "localhost")
        self.assertEqual(model.dataset_id("sqlserver", "srv", "db", None, "orders"), "sqlserver://srv/db/dbo.orders")

    def test_bookkeeping_schemas_are_marked(self):
        g = model.Graph()
        nid = g.dataset("postgresql", *SALES, "ops", "pipeline_run")
        self.assertTrue(g.nodes[nid]["control"])
        self.assertFalse(g.nodes[g.dataset("postgresql", *SALES, "bronze", "payments")]["control"])


class SqlTests(unittest.TestCase):
    def test_insert_select_with_cte_and_adf_parameters(self):
        r = sql.analyze("WITH c AS (SELECT run_id FROM ops.pipeline_run WHERE batch_id = @batch_id) "
                        "INSERT INTO ops.pipeline_step_log (a) SELECT c.run_id FROM c JOIN quality.rejected_records r ON r.x = c.x")
        self.assertEqual(r["writes"], {("ops", "pipeline_step_log")})
        self.assertEqual(r["reads"], {("ops", "pipeline_run"), ("quality", "rejected_records")})   # the CTE is not a table

    def test_function_calls_both_ways(self):
        self.assertIn(("quality", "validate_payments"), sql.analyze("SELECT * FROM quality.validate_payments(@run_id, @b);")["calls"])
        self.assertIn(("ops", "start_run"), sql.analyze("SELECT ops.start_run(@a, @b) AS run_id;")["calls"])

    def test_update_merge_delete(self):
        self.assertEqual(sql.analyze("UPDATE silver.p SET x = 1 FROM bronze.p b WHERE b.id = p.id")["writes"], {("silver", "p")})
        m = sql.analyze("MERGE INTO silver.c t USING bronze.c s ON t.id = s.id WHEN MATCHED THEN UPDATE SET n = s.n")
        self.assertEqual((m["writes"], m["reads"]), ({("silver", "c")}, {("bronze", "c")}))
        d = sql.analyze("DELETE FROM silver.rejected_records WHERE entity = 'x'")
        self.assertEqual((d["writes"], d["reads"]), ({("silver", "rejected_records")}, set()))

    def test_tsql_exec(self):
        self.assertEqual(sql.analyze("EXEC dbo.load_orders @day = '2026'", "tsql")["calls"], {("dbo", "load_orders")})

    def test_procedure_code_and_dynamic_sql(self):
        r = sql.procedural("""BEGIN INSERT INTO quality.rejected_records (a) SELECT p.a FROM bronze.payments p;
                              UPDATE ops.pipeline_run SET x = 1; PERFORM ops.log_step(1);
                              EXECUTE format('DELETE FROM bronze.%I WHERE batch_id = $1', t) USING b; END""")
        self.assertEqual(r["writes"], {("quality", "rejected_records"), ("ops", "pipeline_run")})
        self.assertIn(("bronze", "payments"), r["reads"])
        self.assertIn(("ops", "log_step"), r["calls"])
        self.assertEqual(r["dynamic"], 1)

    def test_catalog_keeps_only_real_objects(self):
        cat = collect.Catalog("postgresql", *SALES, "public")
        cat.relations = {("bronze", "payments"): "table", ("public", "orders"): "table"}
        self.assertEqual(cat.find(("bronze", "payments"), cat.relations), ("bronze", "payments"))
        self.assertEqual(cat.find((None, "orders"), cat.relations), ("public", "orders"))
        self.assertIsNone(cat.find((None, "v_count"), cat.relations))          # a variable, not a table

    def test_adf_paths_are_readable(self):
        self.assertEqual(collect._pretty_path("landing/@pipeline().parameters.p_batch_id/@pipeline().parameters.p_trigger_file"),
                         "landing/{p_batch_id}/{p_trigger_file}")


def snap(scope, name, g):
    return {"scope": scope, "name": name, "built_at": "2026-11-24T10:00:00+00:00", "seconds": 1, "ok": True, "error": None, "graph": g.to_json()}


def two_sources():
    """ADF pipeline PL_01 loads salesdb bronze.payments; Sales DB's function reads it and writes quality.rejected_records."""
    adf = model.Graph()
    p = adf.node(model.job_id("adf", "3", "PL_01"), "pipeline", "PL_01", scope="adf:3", source_name="ADF")
    adf.edge(p, adf.dataset("postgresql", *SALES, "bronze", "payments", via_adf=True), "Copy step")
    adf.edge(p, adf.dataset("postgresql", *SALES, "ops", "pipeline_run", via_adf=True), "Script step")
    db = model.Graph()
    own = {"scope": "db:3", "source_name": "Sales DB"}
    b = db.dataset("postgresql", *SALES, "bronze", "payments", **own)
    fn = db.node(model.job_id("fn", f"{SALES[0]}/{SALES[1]}", "quality.validate_payments"), "function", "quality.validate_payments()", **own)
    r = db.dataset("postgresql", *SALES, "quality", "rejected_records", **own)
    v = db.dataset("postgresql", *SALES, "quality", "v_rejects", kind="view", **own)
    db.edge(b, fn, "function code")
    db.edge(fn, r, "function code")
    db.edge(r, v, "catalog")
    db.dataset("postgresql", *SALES, "public", "unused", **own)             # no link: not drawn
    return [snap("adf:3", "ADF", adf), snap("db:3", "Sales DB", db)]


SCOPES = [{"key": "adf:3", "kind": "adf", "id": 3, "name": "ADF", "poc": False},
          {"key": "db:3", "kind": "db", "id": 3, "name": "Sales DB", "poc": False}]


class MergeAndViewTests(unittest.TestCase):
    def test_merge_joins_the_same_table_and_the_database_owns_it(self):
        nodes, edges = graph.merge(two_sources())
        bp = nodes[model.dataset_id("postgresql", *SALES, "bronze", "payments")]
        self.assertEqual(bp["scope"], "db:3")
        self.assertEqual({s["scope"] for s in bp["seen_by"]}, {"adf:3", "db:3"})

    def test_edges_to_unknown_nodes_are_dropped(self):
        adf = model.Graph()
        p = adf.node(model.job_id("adf", "3", "PL_01"), "pipeline", "PL_01", scope="adf:3")
        adf.edge(p, model.job_id("fn", "x/y", "pg_catalog.now"), "Script step", kind="runs")
        self.assertEqual(graph.merge([snap("adf:3", "ADF", adf)])[1], [])

    def view(self, scope_key, control=False):
        with patch.object(graph, "health"), patch.object(graph, "_attach_owner_by_location"):
            return graph.view(next(s for s in SCOPES if s["key"] == scope_key), two_sources(), SCOPES, control=control)

    def labels(self, v):
        return {n["label"] for n in v["nodes"]}

    def test_the_factory_sees_into_the_database_it_loads(self):
        v = self.view("adf:3")
        self.assertTrue({"PL_01", "bronze.payments", "quality.validate_payments()", "quality.rejected_records"} <= self.labels(v))
        self.assertNotIn("quality.v_rejects", self.labels(v))                 # 3 steps into Sales DB: beyond CROSS_HOPS
        self.assertGreater(v["cross_source"], 0)

    def test_the_database_sees_the_pipeline_that_writes_into_it(self):
        v = self.view("db:3")
        self.assertIn("PL_01", self.labels(v))
        self.assertNotIn("public.unused", self.labels(v))
        self.assertEqual(v["hidden"]["unlinked"], 1)

    def test_bookkeeping_tables_hidden_unless_asked(self):
        self.assertNotIn("ops.pipeline_run", self.labels(self.view("adf:3")))
        self.assertIn("ops.pipeline_run", self.labels(self.view("adf:3", control=True)))


if __name__ == "__main__":
    unittest.main()
