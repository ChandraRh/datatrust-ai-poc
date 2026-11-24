"""The lineage map one source sees: every source's graph merged, health added, cut to what concerns that source.

    merge(snaps)            one graph: a node seen by several sources is one node (its owner is the database it lives in)
    health(nodes, scopes)   per node: its data checks and open incidents -> ok / warn / bad
    view(scope, ...)        the picked source's own nodes + what other sources read from or write into them
                            (up to CROSS_HOPS steps into the other source), control tables hidden unless asked for
"""
from __future__ import annotations

from collections import deque

import appdb

from . import model

CROSS_HOPS = 2
BAD_SEV, WARN_SEV = {"CRITICAL", "HIGH"}, {"MEDIUM", "LOW"}
RANK = {None: 0, "ok": 1, "warn": 2, "bad": 3}


def merge(snaps: list) -> tuple[dict, list]:
    nodes, edges = {}, {}
    for snap in snaps:
        g = snap.get("graph") or {}
        for n in g.get("nodes") or []:
            cur = nodes.get(n["id"])
            seen = {"scope": snap["scope"], "name": snap["name"]}
            if cur is None:
                nodes[n["id"]] = {**n, "seen_by": [seen]}
                continue
            cur["seen_by"].append(seen)
            if n.get("scope", "").startswith("db:") and not str(cur.get("scope", "")).startswith("db:"):
                cur.update({k: v for k, v in n.items() if k not in ("seen_by",)})   # the database it lives in owns it
            elif cur["kind"] == "table" and n["kind"] == "view":
                cur["kind"] = "view"
        for e in g.get("edges") or []:
            key = (e["from"], e["to"], e["kind"])
            cur = edges.get(key)
            if cur is None:
                edges[key] = {**e, "evidence": list(e["evidence"]), "detail": list(e.get("detail") or [])}
            else:
                cur["evidence"] += [x for x in e["evidence"] if x not in cur["evidence"]]
    # an edge to something no source knows (e.g. a call to a built-in function) is dropped
    return nodes, [e for e in edges.values() if e["from"] in nodes and e["to"] in nodes]


def _attach_owner_by_location(nodes: dict, scopes: list) -> None:
    """A table only a Data Factory saw, in a database DataTrust is connected to: it belongs to that connection."""
    from estate import store
    where = {}
    for c in store.list_connections():
        system = {"postgresql": "postgresql", "sqlserver": "sqlserver", "snowflake": "snowflake"}.get(c["source_type"])
        if system:
            where[(model.SYSTEMS[system], model.norm_host(c["host"]), str(c["database"]).lower())] = c["connection_id"]
    names = {s["key"]: s["name"] for s in scopes}
    for n in nodes.values():
        if n.get("scope") or n["kind"] not in ("table", "view"):
            continue
        cid = where.get((n.get("system"), n.get("host"), str(n.get("database") or "").lower()))
        if cid and f"db:{cid}" in names:
            n["scope"], n["source_name"] = f"db:{cid}", names[f"db:{cid}"]


def health(nodes: dict, scopes: list) -> None:
    """Checks on a table and open incidents about it (or about a pipeline / job) -> status, with the reasons."""
    import scopes as sc
    from estate import store
    conns = {c["connection_id"]: c for c in store.list_connections()}
    by_table = {}
    for s in scopes:
        if s["kind"] == "adf":                                        # a factory's own pipeline rules -> its pipeline's box
            for chk in sc._pipeline_checks_of(s["id"]):
                if chk.get("last_status"):
                    nid = model.job_id("adf", str(s["id"]), (chk.get("config") or {}).get("pipeline", ""))
                    by_table.setdefault(nid, []).append({"name": chk["name"], "status": chk["last_status"], "check_id": chk["check_id"]})
            continue
        if s["kind"] != "db" or s["id"] not in conns:
            continue
        c = conns[s["id"]]
        system = {"postgresql": "postgresql", "sqlserver": "sqlserver", "snowflake": "snowflake"}.get(c["source_type"])
        for chk in sc._checks_of(s["id"], skip_dedupe=False):
            if not chk.get("table_name") or not chk.get("last_status"):
                continue
            nid = model.dataset_id(system, c["host"], chk.get("database") or c["database"], chk["schema_name"], chk["table_name"])
            by_table.setdefault(nid, []).append({"name": chk["name"], "status": chk["last_status"], "check_id": chk["check_id"]})
    incidents = appdb.rows("""SELECT incident_id, scope, title, severity, meta FROM app.incident_state
                              WHERE resolved_at IS NULL""") if appdb.table_exists("app.incident_state") else []
    poc = next((s for s in scopes if s.get("poc")), None)
    poc_conn = conns.get(poc["id"]) if poc else None
    by_node = {}
    for i in incidents:
        m = i.get("meta") or {}
        targets = []
        iid = i["incident_id"]
        if iid.startswith("adf:") and m.get("pipeline"):
            targets.append(model.job_id("adf", iid.split(":")[1], m["pipeline"]))
        elif poc_conn and i["scope"] == poc["key"]:
            host, db = poc_conn["host"], poc_conn["database"]
            entity = m.get("entity") or ("payments" if m.get("type") == "SETTLEMENT_MISMATCH" else None)
            if m.get("type") in ("DUPLICATES", "DATA_LOSS") and not entity:
                entity = iid.split("-", 1)[1] if "-" in iid else None
            layer = "bronze" if m.get("type") in ("DUPLICATES", "DATA_LOSS") else "silver"
            if entity:
                targets.append(model.dataset_id("postgresql", host, db, layer, entity))
            if m.get("type") == "PIPELINE_FAILURE" and m.get("layer"):
                targets.append(model.job_id("poc", f"{model.norm_host(host)}/{str(db).lower()}", str(m["layer"]).lower()))
        for t in targets:
            by_node.setdefault(t, []).append({"id": iid, "title": i["title"], "severity": i["severity"]})
    for nid, n in nodes.items():
        checks, incs = by_table.get(nid, []), by_node.get(nid, [])
        status = None
        if checks:
            status = "bad" if any(c["status"] in ("FAIL", "ERROR") for c in checks) else \
                     "warn" if any(c["status"] == "WARN" for c in checks) else "ok"
        for i in incs:
            s = "bad" if i["severity"] in BAD_SEV else "warn"
            status = s if RANK[s] > RANK[status] else status
        n["health"] = status
        if checks:
            n["checks"] = checks[:8]
        if incs:
            n["incidents"] = incs[:8]


def view(scope: dict, snaps: list, scopes: list, control: bool = False) -> dict:
    nodes, edges = merge(snaps)
    _attach_owner_by_location(nodes, scopes)
    mine = {nid for nid, n in nodes.items() if any(s["scope"] == scope["key"] for s in n["seen_by"]) or n.get("scope") == scope["key"]}
    out_e, in_e = {}, {}
    for e in edges:
        out_e.setdefault(e["from"], []).append(e)
        in_e.setdefault(e["to"], []).append(e)
    # the source's own part, then up to CROSS_HOPS steps into other sources (both directions)
    dist = {nid: 0 for nid in mine}
    q = deque(mine)
    while q:
        cur = q.popleft()
        for e in out_e.get(cur, []) + in_e.get(cur, []):
            nxt = e["to"] if e["from"] == cur else e["from"]
            d = dist[cur] + (0 if nxt in mine else 1)
            if d <= CROSS_HOPS and (nxt not in dist or d < dist[nxt]):
                dist[nxt] = d
                q.append(nxt)
    keep = set(dist)
    hidden_control = {nid for nid in keep if nodes[nid].get("control")} if not control else set()
    keep -= hidden_control
    kept_edges = [e for e in edges if e["from"] in keep and e["to"] in keep]
    linked = {e["from"] for e in kept_edges} | {e["to"] for e in kept_edges}
    always = {nid for nid in keep & mine if nodes[nid]["kind"] in ("pipeline", "job")}
    isolated = keep - linked - always
    keep -= isolated
    picked = {nid: nodes[nid] for nid in keep}
    health(picked, scopes)
    for nid, n in picked.items():
        n["mine"] = nid in mine
        n["other_source"] = not n["mine"] or (n.get("scope") not in (None, scope["key"]) and len(n["seen_by"]) > 1)
    cross = [e for e in kept_edges if picked[e["from"]]["mine"] != picked[e["to"]]["mine"]
             or {s["scope"] for s in picked[e["from"]]["seen_by"]} != {s["scope"] for s in picked[e["to"]]["seen_by"]}]
    return {"nodes": list(picked.values()), "edges": kept_edges,
            "hidden": {"control": len(hidden_control), "unlinked": len(isolated)},
            "cross_source": len({(e["from"], e["to"]) for e in cross}),
            "sources": [{"scope": s["scope"], "name": s["name"], "built_at": s["built_at"], "seconds": s["seconds"],
                         "ok": s["ok"], "error": s["error"], "notes": (s.get("graph") or {}).get("notes") or []} for s in snaps]}
