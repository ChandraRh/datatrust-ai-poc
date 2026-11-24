"""Nodes, edges and ids of the lineage map.

Node kinds   table, view (datasets in a database), file (a file or folder in storage), pipeline, function, job
Edge kinds   flow (data moves: dataset -> job, job -> dataset, table -> view), runs (a job starts another job)

A dataset id only depends on WHERE the data lives (system, server, database, schema, name), never on who saw it,
so the same table found by two collectors is one node.
"""
from __future__ import annotations

import re

SYSTEMS = {"postgresql": "PostgreSQL", "sqlserver": "SQL Server", "snowflake": "Snowflake", "adls": "Storage", "file": "Files"}
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "(local)", "."}
DEFAULT_SCHEMA = {"postgresql": "public", "sqlserver": "dbo"}
# schemas that hold the pipelines' own bookkeeping (run logs, step logs, settings): hidden unless asked for
CONTROL_SCHEMAS = {"ops", "audit", "dt_audit", "log", "logs", "logging", "control", "etl_control", "monitor", "telemetry",
                   "app", "portal"}


def norm_host(host: str | None) -> str:
    h = str(host or "").strip().lower()
    h = re.sub(r"^tcp:", "", h)
    h = re.split(r"[,:]", h)[0] if not h.startswith("[") else h       # drop the port: "server,1433" / "server:5432"
    h = h.rstrip(".")
    return "localhost" if h in LOCAL_HOSTS else h


def dataset_id(system: str, host: str | None, database: str | None, schema: str | None, name: str) -> str:
    system = (system or "").lower()
    schema = (schema or DEFAULT_SCHEMA.get(system, "")).strip().strip('"[]').lower()
    name = str(name).strip().strip('"[]').lower()
    return f"{system}://{norm_host(host)}/{str(database or '').strip().lower()}/{schema}.{name}"


def file_id(system: str, account: str | None, path: str) -> str:
    return f"{(system or 'file').lower()}://{str(account or '').lower()}/{str(path).strip('/').lower()}"


def job_id(kind: str, owner: str, name: str) -> str:
    return f"job:{kind}:{owner}:{name}".lower()


class Graph:
    """What one collector found. Nodes by id (first writer keeps the label; details are merged), edges deduplicated."""

    def __init__(self):
        self.nodes: dict = {}
        self.edges: dict = {}
        self.notes: list = []

    def node(self, nid: str, kind: str, label: str, **attrs) -> str:
        n = self.nodes.get(nid)
        if n is None:
            self.nodes[nid] = {"id": nid, "kind": kind, "label": label, **{k: v for k, v in attrs.items() if v is not None}}
        else:
            for k, v in attrs.items():
                if v is not None and k not in n:
                    n[k] = v
            if n["kind"] == "table" and kind == "view":                 # the catalog knows better than a guess
                n["kind"] = "view"
        return nid

    def dataset(self, system, host, database, schema, name, kind="table", **attrs) -> str:
        schema = schema or DEFAULT_SCHEMA.get(str(system).lower(), "")
        return self.node(dataset_id(system, host, database, schema, name), kind, f"{schema}.{name}" if schema else str(name),
                         system=SYSTEMS.get(str(system).lower(), system), database=database, schema=str(schema).lower(),
                         name=str(name).lower(), host=norm_host(host), control=str(schema).lower() in CONTROL_SCHEMAS, **attrs)

    def edge(self, src: str, dst: str, evidence: str, kind: str = "flow", detail: str | None = None) -> None:
        if not src or not dst or src == dst:
            return
        key = (src, dst, kind)
        e = self.edges.get(key)
        if e is None:
            self.edges[key] = {"from": src, "to": dst, "kind": kind, "evidence": [evidence], "detail": [detail] if detail else []}
        else:
            if evidence not in e["evidence"]:
                e["evidence"].append(evidence)
            if detail and detail not in e["detail"]:
                e["detail"].append(detail)

    def note(self, text: str) -> None:
        if text not in self.notes:
            self.notes.append(text)

    def to_json(self) -> dict:
        return {"nodes": list(self.nodes.values()), "edges": list(self.edges.values()), "notes": self.notes}
