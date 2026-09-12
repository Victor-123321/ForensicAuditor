"""
Tool implementations the investigation agent can call (FR-12). Kept as
plain Python functions operating on a NetworkX graph -- called directly
by agent/loop.py, no HTTP hop, so the loop stays fast (NFR-3).
"""
from __future__ import annotations

import networkx as nx

from graph import detectors as detector_module
from shared.schemas import Lead


def query_entity(g: nx.MultiDiGraph, entity_id: str) -> dict:
    if not g.has_node(entity_id):
        return {"error": f"No entity '{entity_id}' in the graph"}
    return dict(g.nodes[entity_id])


def run_detector(g: nx.MultiDiGraph, name: str, **kwargs) -> list[dict]:
    if name not in detector_module.DETECTORS:
        return [{"error": f"Unknown detector '{name}', choose from {list(detector_module.DETECTORS)}"}]
    leads: list[Lead] = detector_module.DETECTORS[name](g, **kwargs)
    return [lead.model_dump() for lead in leads]


def get_invoice(g: nx.MultiDiGraph, uuid: str) -> dict:
    return query_entity(g, uuid)


def trace_payment_path(g: nx.MultiDiGraph, from_id: str, to_id: str, max_hops: int = 6) -> dict:
    # Kept as a MultiDiGraph (instead of collapsing to DiGraph) so parallel
    # payments between the same two accounts keep their own edge ids --
    # those ids must match to_graph_export()'s str(key) convention so the
    # evidence-trail filter in agent/loop.py can resolve them.
    pg = nx.MultiDiGraph()
    for u, v, key, data in g.edges(keys=True, data=True):
        if data.get("type") == "EXECUTED_PAYMENT":
            pg.add_edge(u, v, key=key, **data)

    if not (pg.has_node(from_id) and pg.has_node(to_id)):
        return {"path": None, "reason": "one or both accounts not found in the payment graph"}
    try:
        path = nx.shortest_path(pg, from_id, to_id)
        if len(path) - 1 > max_hops:
            return {"path": None, "reason": f"shortest path exceeds max_hops={max_hops}"}
        edges_on_path = [
            {"id": str(key), "from": path[i], "to": path[i + 1], **data}
            for i in range(len(path) - 1)
            for key, data in pg.get_edge_data(path[i], path[i + 1]).items()
        ]
        return {"path": path, "edges": edges_on_path}
    except nx.NetworkXNoPath:
        return {"path": None, "reason": "no directed payment path exists"}


def check_blacklist(g: nx.MultiDiGraph, rfc: str) -> dict:
    if not g.has_node(rfc):
        return {"rfc": rfc, "found": False}
    return {"rfc": rfc, "found": True, "blacklist_status": g.nodes[rfc].get("blacklist_status", "none")}


def get_neighbors(g: nx.MultiDiGraph, node_id: str, edge_type: str | None = None) -> list[dict]:
    if not g.has_node(node_id):
        return []
    results = []
    for _, v, key, data in g.out_edges(node_id, keys=True, data=True):
        if edge_type and data.get("type") != edge_type:
            continue
        results.append({"neighbor": v, "direction": "out", "edge_id": str(key), **data})
    for u, _, key, data in g.in_edges(node_id, keys=True, data=True):
        if edge_type and data.get("type") != edge_type:
            continue
        results.append({"neighbor": u, "direction": "in", "edge_id": str(key), **data})
    return results


# Registry the ReAct loop dispatches against by name (agent/loop.py).
TOOLS = {
    "query_entity": query_entity,
    "run_detector": run_detector,
    "get_invoice": get_invoice,
    "trace_payment_path": trace_payment_path,
    "check_blacklist": check_blacklist,
    "get_neighbors": get_neighbors,
}
