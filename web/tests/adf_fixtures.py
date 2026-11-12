"""Realistic Azure Data Factory REST payloads (shapes as returned by api-version 2018-06-01), used by the ADF tests.

Factory: ADLS CSV -> Azure PostgreSQL.
  PL_Master          Execute Pipeline -> PL_Load_Sales
  PL_Load_Sales      Copy orders (ADLS orders/<runDate>/orders.csv -> public.orders)
                     -> Copy customers (ADLS customers/customers.csv -> public.customers)   [fails today: duplicate key]
                     -> on failure: Notify (Web)
  TR_Daily           schedule trigger, every 1 day, started, runs PL_Master
"""
from datetime import datetime, timedelta, timezone

SUB = "00000000-1111-2222-3333-444444444444"
CFG = {"subscription_id": SUB, "resource_group": "rg-data", "factory_name": "adf-sales-dev", "auth_mode": "default",
       "lookback_days": 30}

NOW = datetime.now(timezone.utc).replace(microsecond=0)


def t(hours_ago: float, extra_s: float = 0) -> str:
    return (NOW - timedelta(hours=hours_ago) + timedelta(seconds=extra_s)).strftime("%Y-%m-%dT%H:%M:%S.1234567Z")


LINKED = [
    {"name": "LS_ADLS", "properties": {"type": "AzureBlobFS", "typeProperties": {"url": "https://dtlake.dfs.core.windows.net/"}}},
    {"name": "LS_PG", "properties": {"type": "AzurePostgreSql", "typeProperties": {
        "connectionString": "host=localhost;port=5432;database=__DB__;uid=adf_loader;EncryptionMethod=1",
        "password": {"type": "SecureString", "value": "**********"}}}},
]

DATASETS = [
    {"name": "DS_CSV", "properties": {
        "type": "DelimitedText", "linkedServiceName": {"referenceName": "LS_ADLS", "type": "LinkedServiceReference"},
        "parameters": {"folder": {"type": "string"}, "fileName": {"type": "string", "defaultValue": "data.csv"}},
        "typeProperties": {"location": {"type": "AzureBlobFSLocation", "fileSystem": "landing",
                                        "folderPath": {"value": "@dataset().folder", "type": "Expression"},
                                        "fileName": {"value": "@dataset().fileName", "type": "Expression"}},
                           "columnDelimiter": ",", "firstRowAsHeader": True}}},
    {"name": "DS_PG", "properties": {
        "type": "AzurePostgreSqlTable", "linkedServiceName": {"referenceName": "LS_PG", "type": "LinkedServiceReference"},
        "parameters": {"table": {"type": "string"}},
        "typeProperties": {"schema": "public", "table": {"value": "@dataset().table", "type": "Expression"}}}},
]


def _copy(name, folder_expr, file, table, depends=None):
    return {"name": name, "type": "Copy",
            "dependsOn": [{"activity": d, "dependencyConditions": ["Succeeded"]} for d in (depends or [])],
            "policy": {"timeout": "0.12:00:00", "retry": 0},
            "inputs": [{"referenceName": "DS_CSV", "type": "DatasetReference",
                        "parameters": {"folder": folder_expr, "fileName": file}}],
            "outputs": [{"referenceName": "DS_PG", "type": "DatasetReference", "parameters": {"table": table}}],
            "typeProperties": {"source": {"type": "DelimitedTextSource", "storeSettings": {"type": "AzureBlobFSReadSettings"}},
                               "sink": {"type": "AzurePostgreSqlSink", "writeMethod": "CopyCommand"},
                               "enableSkipIncompatibleRow": False}}


PIPELINES = [
    {"name": "PL_Load_Sales", "properties": {
        "parameters": {"runDate": {"type": "string", "defaultValue": "2026-11-01"}},
        "folder": {"name": "sales"},
        "activities": [
            _copy("Copy orders", "@concat('orders/', pipeline().parameters.runDate)", "orders.csv", "orders"),
            _copy("Copy customers", "customers", "customers.csv", "customers", ["Copy orders"]),
            {"name": "Notify", "type": "WebActivity", "dependsOn": [{"activity": "Copy customers", "dependencyConditions": ["Failed"]}],
             "typeProperties": {"url": "https://example.invalid/hook", "method": "POST"}},
        ]}},
    {"name": "PL_Master", "properties": {"activities": [
        {"name": "Run sales load", "type": "ExecutePipeline",
         "typeProperties": {"pipeline": {"referenceName": "PL_Load_Sales", "type": "PipelineReference"},
                            "waitOnCompletion": True, "parameters": {"runDate": "@pipeline().parameters.runDate"}}}],
        "parameters": {"runDate": {"type": "string"}}}},
]

TRIGGERS = [{"name": "TR_Daily", "properties": {"type": "ScheduleTrigger", "runtimeState": "Started",
                                                "pipelines": [{"pipelineReference": {"referenceName": "PL_Master"}}],
                                                "typeProperties": {"recurrence": {"frequency": "Day", "interval": 1,
                                                                                  "startTime": "2026-10-01T02:00:00Z"}}}}]


def run(run_id, pipeline, status, start_h, dur_s, params=None, parent=None, message=""):
    r = {"runId": run_id, "pipelineName": pipeline, "status": status, "runStart": t(start_h),
         "runEnd": t(start_h, dur_s) if status not in ("InProgress", "Queued") else None,
         "durationInMs": int(dur_s * 1000) if status not in ("InProgress", "Queued") else None,
         "parameters": params or {}, "lastUpdated": t(start_h, dur_s), "isLatest": True, "runGroupId": run_id, "message": message,
         "invokedBy": ({"name": "Run sales load", "id": "x", "invokedByType": "PipelineActivity", "pipelineName": "PL_Master",
                        "pipelineRunId": parent} if parent else {"name": "TR_Daily", "id": "tr", "invokedByType": "ScheduleTrigger"})}
    return r


DUP_MSG = ("Failure happened on 'Sink' side. ErrorCode=UserErrorPostgreSqlCopyCommandFailed,'Type=Microsoft.DataTransfer."
           "Common.Shared.HybridDeliveryException,Message=COPY command failed,Source=Microsoft.DataTransfer.Connectors."
           "PostgreSql,''Type=Npgsql.PostgresException,Message=23505: duplicate key value violates unique constraint "
           "\"customers_pkey\",Source=Npgsql,'")


def activity(run_id, pipeline, name, atype, status, start_h, dur_s, output=None, error=None):
    return {"activityRunId": f"{run_id}-{name.replace(' ', '_')}", "pipelineRunId": run_id, "pipelineName": pipeline,
            "activityName": name, "activityType": atype, "status": status, "activityRunStart": t(start_h),
            "activityRunEnd": t(start_h, dur_s), "durationInMs": int(dur_s * 1000), "input": {},
            "output": output or {}, "error": error or {"errorCode": "", "message": "", "failureType": "", "target": name},
            "userProperties": {}}


def copy_out(rows, skipped=0):
    return {"dataRead": rows * 50, "dataWritten": rows * 50, "filesRead": 1, "rowsRead": rows + skipped, "rowsCopied": rows,
            "rowsSkipped": skipped, "copyDuration": 4, "throughput": 12.5,
            "executionDetails": [{"source": {"type": "AzureBlobFS"}, "sink": {"type": "AzurePostgreSql"}, "status": "Succeeded",
                                  "usedDataIntegrationUnits": 4, "usedParallelCopies": 1,
                                  "detailedDurations": {"queuingDuration": 2, "transferDuration": 3}}]}


def history():
    """5 good daily runs, then today's failed one. Returns (pipeline_runs, {run_id: activity_runs})."""
    runs, acts = [], {}
    for day in range(5, 0, -1):
        h = 24 * day + 1
        m, c = f"m{day}", f"c{day}"
        runs += [run(m, "PL_Master", "Succeeded", h, 70, {"runDate": f"d{day}"}),
                 run(c, "PL_Load_Sales", "Succeeded", h - 0.001, 60, {"runDate": "2026-11-01"}, parent=m)]
        acts[m] = [activity(m, "PL_Master", "Run sales load", "ExecutePipeline", "Succeeded", h, 65, {"pipelineRunId": c})]
        acts[c] = [activity(c, "PL_Load_Sales", "Copy orders", "Copy", "Succeeded", h - 0.001, 20, copy_out(3)),
                   activity(c, "PL_Load_Sales", "Copy customers", "Copy", "Succeeded", h - 0.0005, 20, copy_out(2))]
    runs += [run("m0", "PL_Master", "Failed", 1, 50, {"runDate": "d0"},
                 message="Operation on target Run sales load failed: Operation on target Copy customers failed: " + DUP_MSG),
             run("c0", "PL_Load_Sales", "Failed", 0.999, 40, {"runDate": "2026-11-01"}, parent="m0",
                 message="Operation on target Copy customers failed: " + DUP_MSG)]
    acts["m0"] = [activity("m0", "PL_Master", "Run sales load", "ExecutePipeline", "Failed", 1, 45, {"pipelineRunId": "c0"},
                           {"errorCode": "2011", "message": "Operation on target Copy customers failed: " + DUP_MSG,
                            "failureType": "UserError", "target": "Run sales load"})]
    acts["c0"] = [activity("c0", "PL_Load_Sales", "Copy orders", "Copy", "Succeeded", 0.999, 15, copy_out(3)),
                  activity("c0", "PL_Load_Sales", "Copy customers", "Copy", "Failed", 0.995, 10, {},
                           {"errorCode": "2200", "message": DUP_MSG, "failureType": "UserError", "target": "Copy customers"}),
                  activity("c0", "PL_Load_Sales", "Notify", "WebActivity", "Succeeded", 0.99, 1, {})]
    return runs, acts


class FakeClient:
    """Stands in for AdfClient; same method names and return shapes."""

    def __init__(self, db_name="datatrust_poc"):
        import json
        self.linked = json.loads(json.dumps(LINKED).replace("__DB__", db_name))
        self.runs, self.acts = history()
        self.calls = []

    def factory(self):
        return {"name": CFG["factory_name"], "location": "westeurope"}

    def pipelines(self):
        return PIPELINES

    def datasets(self):
        return DATASETS

    def linked_services(self):
        return self.linked

    def triggers(self):
        return TRIGGERS

    def pipeline_runs(self, after, before):
        self.calls.append(("runs", after, before))
        return self.runs

    def activity_runs(self, run_id, after, before):
        return self.acts.get(run_id, [])

    def trigger_runs(self, after, before):
        return [{"triggerName": "TR_Daily", "triggerRunId": "trr1", "triggerType": "ScheduleTrigger",
                 "triggerRunTimestamp": t(1), "status": "Succeeded", "triggeredPipelines": {"PL_Master": "m0"}, "properties": {}}]
