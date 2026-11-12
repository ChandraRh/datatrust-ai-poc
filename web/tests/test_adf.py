"""Azure Data Factory connector: expression/dataset resolution, error diagnosis, REST client paging/retries, metrics,
webhook auth. No Azure or database needed (python -m unittest discover -s tests, from web/)."""
import time
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from adf import diagnose, resolve, store
from adf.client import AdfClient, AdfError
from tests import adf_fixtures as fx


# ============================================================================ resolve
class ExpressionTests(unittest.TestCase):
    ctx = {"pipeline": {"parameters": {"runDate": "2026-11-05", "Region": "EU"}, "RunId": "r1",
                        "TriggerTime": "2026-11-05T02:00:00+00:00"},
           "dataset": {"fileName": "orders.csv"}, "now": datetime(2026, 11, 5, 2, 0, tzinfo=timezone.utc)}

    def test_parameters_concat_and_interpolation(self):
        self.assertEqual(resolve.resolve_value("@pipeline().parameters.runDate", self.ctx), "2026-11-05")
        self.assertEqual(resolve.resolve_value("@concat('in/', pipeline().parameters.region, '/', dataset().fileName)", self.ctx),
                         "in/EU/orders.csv")
        self.assertEqual(resolve.resolve_value("raw/@{pipeline().parameters.runDate}/x.csv", self.ctx), "raw/2026-11-05/x.csv")
        self.assertEqual(resolve.resolve_value({"value": "@dataset().fileName", "type": "Expression"}, self.ctx), "orders.csv")
        self.assertEqual(resolve.resolve_value("plain/path", self.ctx), "plain/path")
        self.assertEqual(resolve.resolve_value("@@literal", self.ctx), "@literal")

    def test_dates(self):
        self.assertEqual(resolve.resolve_value("@formatDateTime(pipeline().TriggerTime, 'yyyy/MM/dd')", self.ctx), "2026/11/05")
        self.assertEqual(resolve.resolve_value("@{formatDateTime(utcnow(), 'yyyyMMdd')}.csv", self.ctx), "20261105.csv")
        self.assertEqual(resolve.resolve_value("@adddays(pipeline().TriggerTime, -1, 'yyyy-MM-dd')", self.ctx), "2026-11-04")

    def test_runtime_only_values_are_reported_not_guessed(self):
        for expr in ("@item().name", "@activity('Lookup').output.firstRow.x", "@pipeline().parameters.missing",
                     "@variables('v')", "@unknownFn(1)"):
            with self.assertRaises(resolve.Unresolved, msg=expr):
                resolve.resolve_value(expr, self.ctx)


class DatasetTests(unittest.TestCase):
    def setUp(self):
        self.datasets = {d["name"]: d for d in fx.DATASETS}
        self.linked = {d["name"]: d for d in fx.FakeClient("salesdb").linked}
        self.copy = resolve.flatten_activities(fx.PIPELINES[0]["properties"]["activities"])[0]

    def test_copy_assets_resolved_with_run_parameters(self):
        ctx = resolve.run_context({"run_id": "r", "parameters": {"runDate": "2026-11-05"}, "run_start": "2026-11-05T02:00:00Z",
                                   "raw": {}}, "PL_Load_Sales")
        a = resolve.copy_assets(self.copy, self.datasets, self.linked, ctx)
        s, t = a["source"], a["target"]
        self.assertEqual((s["kind"], s["account"], s["container"], s["path"]), ("storage", "dtlake", "landing", "orders/2026-11-05/orders.csv"))
        self.assertEqual(s["system"], "ADLS Gen2")
        self.assertTrue(s["format"]["header"])
        self.assertEqual((t["kind"], t["host"], t["database"], t["schema"], t["table"]), ("postgres", "localhost", "salesdb", "public", "orders"))
        self.assertEqual(t["write"]["method"], "CopyCommand")
        self.assertEqual(s["unresolved"] + t["unresolved"], [])

    def test_without_run_values_unresolved_parts_are_listed(self):
        a = resolve.copy_assets(self.copy, self.datasets, self.linked, {})
        self.assertIn("parameter folder", a["source"]["unresolved"])

    def test_postgres_v2_linked_service_and_table_name(self):
        ls = {"name": "PG2", "properties": {"type": "PostgreSqlV2", "typeProperties": {"server": "pg.example.com", "port": 6432,
                                                                                        "database": "dw", "username": "u"}}}
        ds = {"name": "D", "properties": {"type": "PostgreSqlV2Table", "linkedServiceName": {"referenceName": "PG2"},
                                          "typeProperties": {"tableName": "sales.fact_orders"}}}
        a = resolve.dataset_asset(ds, {}, {}, {"PG2": ls})
        self.assertEqual((a["host"], a["port"], a["database"], a["schema"], a["table"]), ("pg.example.com", 6432, "dw", "sales", "fact_orders"))

    def test_blob_storage_connection_string_account(self):
        ls = {"name": "B", "properties": {"type": "AzureBlobStorage", "typeProperties": {
            "connectionString": "DefaultEndpointsProtocol=https;AccountName=rawfiles;EndpointSuffix=core.windows.net"}}}
        self.assertEqual(resolve.linked_service_info(ls, {}, {})["account"], "rawfiles")

    def test_nested_activities_are_flattened(self):
        acts = [{"name": "Each file", "type": "ForEach", "typeProperties": {"activities": [{"name": "Copy one", "type": "Copy"}]}},
                {"name": "Check", "type": "IfCondition", "typeProperties": {"ifFalseActivities": [{"name": "Fail it", "type": "Fail"}]}}]
        flat = resolve.flatten_activities(acts)
        self.assertEqual([(a["name"], a["parent"]) for a in flat],
                         [("Each file", None), ("Copy one", "Each file"), ("Check", None), ("Fail it", "Check")])


# ============================================================================ diagnose
class DiagnoseTests(unittest.TestCase):
    def check(self, code, message, cause, system=None, **kw):
        d = diagnose.classify(code, message, **kw)
        self.assertEqual(d["cause_id"], cause, d)
        if system:
            self.assertEqual(d["system"], system)
        return d

    def test_nested_postgres_duplicate_key(self):
        d = self.check("2200", fx.DUP_MSG, "duplicate_key", diagnose.TARGET)
        self.assertEqual(d["details"]["constraint"], "customers_pkey")
        self.assertIn("customers_pkey", d["explanation"])

    def test_missing_source_file_names_the_path(self):
        msg = ("ErrorCode=AdlsGen2OperationFailed,'Type=...,Message=ADLS Gen2 operation failed for: Operation returned an invalid "
               "status code 'NotFound'. Account: 'dtlake'. FileSystem: 'landing'. Path: 'orders/2026-11-05/orders.csv'. "
               "ErrorCode: 'PathNotFound'. Message: 'The specified path does not exist.'")
        d = self.check("2200", msg, "source_path_missing", diagnose.SOURCE)
        self.assertIn("orders/2026-11-05/orders.csv", d["explanation"])
        self.assertTrue(any("dtlake" in f for f in d["fixes"]))
        self.assertTrue(d["retry_helps"])

    def test_many_causes(self):
        self.check("2200", "ErrorCode=AdlsGen2OperationFailed, ErrorCode: 'AuthorizationPermissionMismatch'", "storage_permission")
        self.check("2200", "This request is not authorized to perform this operation. ErrorCode: 'AuthorizationFailure'", "storage_firewall")
        self.check("2200", "Npgsql.PostgresException: 28P01: password authentication failed for user \"adf\"", "pg_auth", diagnose.CREDS)
        self.check("2200", "no pg_hba.conf entry for host \"20.1.2.3\"", "pg_firewall", diagnose.NETWORK)
        self.check("2200", "42P01: relation \"public.orders\" does not exist", "table_missing")
        self.check("2200", "42703: column \"region\" of relation \"orders\" does not exist", "column_missing")
        self.check("2200", "22P02: invalid input syntax for type integer: \"abc\"", "type_conversion")
        self.check("2200", "ErrorCode=DelimitedTextMoreColumnsThanDefined, row 17", "bad_file_format")
        self.check("2200", "23502: null value in column \"customer_id\" violates not-null constraint", "not_null")
        self.check("2200", "The integration runtime 'shir-01' is offline", "ir_unavailable")
        self.check("2200", "A connection attempt failed because the connected party did not properly respond", "network")
        self.check("2011", "Activity Copy1 timed out after 00:30:00", "activity_timeout")
        self.check(None, "bad data", "fail_activity", activity_type="Fail")
        self.check(None, "", "cancelled", status="Cancelled")
        self.check("9999", "something new", "transient_system", failure_type="SystemError")
        self.check("9999", "something new", "user_error", failure_type="UserError")

    def test_leaf_message(self):
        self.assertEqual(diagnose.leaf_message("Operation on target A failed: Operation on target B failed: real"), "real")

    def test_failure_chain_follows_child_pipeline(self):
        runs, acts = fx.history()
        stored = {r["runId"]: {"run_id": r["runId"], "pipeline_name": r["pipelineName"], "status": r["status"],
                               "message": r["message"]} for r in runs}

        def task(a):
            return {"activity_run_id": a["activityRunId"], "run_id": a["pipelineRunId"], "pipeline_name": a["pipelineName"],
                    "activity_name": a["activityName"], "activity_type": a["activityType"], "status": a["status"],
                    "run_start": a["activityRunStart"], "run_end": a["activityRunEnd"], "output": a["output"],
                    "error_code": a["error"].get("errorCode") or None, "error_message": a["error"].get("message") or None,
                    "failure_type": a["error"].get("failureType") or None}
        lookup = lambda rid: (stored[rid], [task(a) for a in acts.get(rid, [])])          # noqa: E731
        roots = diagnose.failure_chain(stored["m0"], [task(a) for a in acts["m0"]], lookup)
        self.assertEqual(len(roots), 1)
        self.assertEqual(roots[0]["activity_name"], "Copy customers")
        self.assertEqual(roots[0]["cause_id"], "duplicate_key")
        self.assertEqual(roots[0]["path"], ["PL_Master", "Run sales load (Execute Pipeline)", "PL_Load_Sales", "Copy customers"])

    def test_failure_without_activity_details_uses_run_message(self):
        roots = diagnose.failure_chain({"run_id": "x", "pipeline_name": "P", "status": "Failed", "message": fx.DUP_MSG}, [])
        self.assertEqual(roots[0]["cause_id"], "duplicate_key")


# ============================================================================ client
class Token:
    def __init__(self):
        self.token, self.expires_on = "tok", time.time() + 3600


class Cred:
    def __init__(self):
        self.calls = 0

    def get_token(self, scope):
        self.calls += 1
        return Token()


class Resp:
    def __init__(self, status, body=None, headers=None):
        self.status_code, self._body, self.headers = status, body or {}, headers or {}
        self.content = b"x"
        self.reason, self.text = "reason", str(body)

    def json(self):
        return self._body


class Session:
    def __init__(self, responses):
        self.responses, self.requests = list(responses), []

    def request(self, method, url, json=None, timeout=None, headers=None):
        self.requests.append((method, url, json, headers))
        return self.responses.pop(0)


class ClientTests(unittest.TestCase):
    def client(self, responses):
        self.session = Session(responses)
        return AdfClient(fx.CFG, credential=Cred(), session=self.session)

    def test_run_query_follows_continuation_token(self):
        c = self.client([Resp(200, {"value": [{"runId": "a"}], "continuationToken": "next"}), Resp(200, {"value": [{"runId": "b"}]})])
        out = c.pipeline_runs(fx.NOW, fx.NOW)
        self.assertEqual([r["runId"] for r in out], ["a", "b"])
        self.assertEqual(self.session.requests[1][2]["continuationToken"], "next")
        url = self.session.requests[0][1]
        self.assertIn(f"/subscriptions/{fx.SUB}/resourceGroups/rg-data/providers/Microsoft.DataFactory/factories/adf-sales-dev/queryPipelineRuns", url)
        self.assertIn("api-version=2018-06-01", url)
        self.assertEqual(self.session.requests[0][3]["Authorization"], "Bearer tok")

    def test_list_follows_next_link(self):
        c = self.client([Resp(200, {"value": [{"name": "p1"}], "nextLink": "https://management.azure.com/next?api-version=2018-06-01"}),
                         Resp(200, {"value": [{"name": "p2"}]})])
        self.assertEqual([p["name"] for p in c.pipelines()], ["p1", "p2"])
        self.assertEqual(self.session.requests[1][1], "https://management.azure.com/next?api-version=2018-06-01")

    def test_throttling_is_retried_then_errors_are_readable(self):
        c = self.client([Resp(429, {}, {"Retry-After": "0"}), Resp(200, {"value": []})])
        self.assertEqual(c.triggers(), [])
        c = self.client([Resp(403, {"error": {"code": "AuthorizationFailed", "message": "no access"}})])
        with self.assertRaises(AdfError) as e:
            c.factory()
        self.assertEqual((e.exception.status, e.exception.code), (403, "AuthorizationFailed"))

    def test_settings_are_validated(self):
        with self.assertRaises(ValueError):
            store.validate_config({**fx.CFG, "subscription_id": "not-a-guid"})
        with self.assertRaises(ValueError):
            store.validate_config({**fx.CFG, "auth_mode": "service_principal", "tenant_id": fx.SUB, "client_id": fx.SUB,
                                   "client_secret_env": "my secret!"})
        ok = store.validate_config({**fx.CFG, "auth_mode": "service_principal", "tenant_id": fx.SUB, "client_id": fx.SUB,
                                    "client_secret_env": "ADF_CLIENT_SECRET", "anything_else": "dropped"})
        self.assertNotIn("anything_else", ok)


# ============================================================================ store helpers
class StoreHelperTests(unittest.TestCase):
    def test_timestamps_with_seven_fraction_digits(self):
        self.assertEqual(store._ts("2026-11-05T02:00:01.1234567Z"), datetime(2026, 11, 5, 2, 0, 1, 123456, tzinfo=timezone.utc))
        self.assertIsNone(store._ts(None))

    def test_copy_and_dataflow_metrics(self):
        m = store.copy_metrics("Copy", fx.copy_out(10, skipped=2))
        self.assertEqual((m["rows_read"], m["rows_written"], m["rows_skipped"], m["files_read"]), (12, 10, 2, 1))
        df = store.copy_metrics("ExecuteDataFlow", {"runStatus": {"metrics": {"sink1": {"rowsWritten": 7, "sources": {"src": {"rowsRead": 9}}}}}})
        self.assertEqual((df["rows_read"], df["rows_written"]), (9, 7))


# ============================================================================ webhook
class WebhookTests(unittest.TestCase):
    def setUp(self):
        from adf import routes
        app = FastAPI()
        app.include_router(routes.hooks)
        self.client = TestClient(app)

    def test_token_required_and_only_requests_a_sync(self):
        with patch.object(store, "check_webhook_token", return_value=False), patch.object(store, "request_sync") as req:
            self.assertEqual(self.client.post("/api/adf/hooks/1", headers={"X-DataTrust-Token": "bad"}).status_code, 401)
            req.assert_not_called()
        with patch.object(store, "check_webhook_token", return_value=True) as chk, patch.object(store, "request_sync") as req:
            r = self.client.post("/api/adf/hooks/1", headers={"X-DataTrust-Token": "good"}, json={"status": "Failed"})
            self.assertEqual(r.status_code, 202)
            chk.assert_called_once_with(1, "good")
            req.assert_called_once_with(1)


if __name__ == "__main__":
    unittest.main()
