"""
Deterministic detectors (FR-5 - FR-10). Each returns a list[Lead] --
leads, never verdicts (FR-10): the investigation agent decides what to
do with them. agent/tools.py's run_detector() dispatches into this
module by name.
"""
from __future__ import annotations

import uuid

import networkx as nx

from shared.schemas import Lead


def detect_blacklist_matches(g: nx.MultiDiGraph) -> list[Lead]:
    leads = []
    for n, data in g.nodes(data=True):
        if data.get("type") != "Company":
            continue
        status = data.get("blacklist_status", "none")
        if status and status != "none":
            leads.append(Lead(
                id=str(uuid.uuid4()), detector="blacklist_match", entity_ids=[n],
                reason=f"{n} appears on SAT's Article 69-B list with status '{status}'",
            ))
    return leads


def detect_invoice_payment_mismatch(g: nx.MultiDiGraph, tolerance: float = 0.01) -> list[Lead]:
    leads = []
    invoice_amounts = {n: data["amount"] for n, data in g.nodes(data=True) if data.get("type") == "Invoice"}
    paid_against_invoice: dict[str, float] = dict.fromkeys(invoice_amounts, 0.0)

    for _, _, data in g.edges(data=True):
        if data.get("type") != "EXECUTED_PAYMENT":
            continue
        ref = data.get("reference")
        if ref in paid_against_invoice:
            paid_against_invoice[ref] += data.get("amount", 0.0)

    for inv_id, amount in invoice_amounts.items():
        paid = paid_against_invoice.get(inv_id, 0.0)
        if paid == 0.0:
            leads.append(Lead(id=str(uuid.uuid4()), detector="invoice_payment_mismatch",
                               entity_ids=[inv_id], reason=f"Invoice {inv_id} has no matching payment"))
        elif abs(paid - amount) > max(tolerance * amount, 1.0):
            leads.append(Lead(id=str(uuid.uuid4()), detector="invoice_payment_mismatch",
                               entity_ids=[inv_id],
                               reason=f"Invoice {inv_id} amount {amount} vs. paid {paid} -- mismatch"))
    return leads


def detect_cycles(g: nx.MultiDiGraph, max_hops: int = 6) -> list[Lead]:
    """Round-tripping / kickback loops: directed cycles in the
    bank-account payment sub-graph."""
    payment_edges = [(u, v) for u, v, data in g.edges(data=True) if data.get("type") == "EXECUTED_PAYMENT"]
    pg = nx.DiGraph()
    pg.add_edges_from(payment_edges)

    leads = []
    for cycle in nx.simple_cycles(pg, length_bound=max_hops):
        if len(cycle) < 2:
            continue
        leads.append(Lead(id=str(uuid.uuid4()), detector="cycle", entity_ids=list(cycle),
                           reason=f"Payment cycle detected across {len(cycle)} accounts: "
                                  f"{' -> '.join(cycle)} -> {cycle[0]}"))
    return leads


def detect_shared_attribute_clusters(g: nx.MultiDiGraph) -> list[Lead]:
    """Connected components over SHARES_* edges (FR-8)."""
    shared_types = {"SHARES_ADDRESS", "SHARES_PHONE", "SHARES_BANK_ACCOUNT"}
    sub_edges = [(u, v) for u, v, data in g.edges(data=True) if data.get("type") in shared_types]
    ug = nx.Graph()
    ug.add_edges_from(sub_edges)

    leads = []
    for component in nx.connected_components(ug):
        if len(component) < 2:
            continue
        leads.append(Lead(id=str(uuid.uuid4()), detector="shared_attribute_cluster",
                           entity_ids=sorted(component),
                           reason=f"{len(component)} companies share an address, phone, or bank "
                                  f"account -- possible shell-company cluster"))
    return leads


def detect_high_centrality(g: nx.MultiDiGraph, top_n: int = 5) -> list[Lead]:
    """Pass-through / intermediary detection (FR-9)."""
    payment_edges = [(u, v) for u, v, data in g.edges(data=True) if data.get("type") == "EXECUTED_PAYMENT"]
    pg = nx.DiGraph()
    pg.add_edges_from(payment_edges)
    if pg.number_of_nodes() < 3:
        return []

    centrality = nx.betweenness_centrality(pg)
    ranked = sorted(centrality.items(), key=lambda kv: kv[1], reverse=True)[:top_n]

    leads = []
    for node, score in ranked:
        if score <= 0:
            continue
        leads.append(Lead(id=str(uuid.uuid4()), detector="high_centrality", entity_ids=[node],
                           reason=f"{node} sits on an unusually high share of payment paths "
                                  f"(betweenness={score:.3f}) -- possible pass-through account"))
    return leads


DETECTORS = {
    "blacklist_match": detect_blacklist_matches,
    "invoice_payment_mismatch": detect_invoice_payment_mismatch,
    "cycle": detect_cycles,
    "shared_attribute_cluster": detect_shared_attribute_clusters,
    "high_centrality": detect_high_centrality,
}


def run_all(g: nx.MultiDiGraph) -> list[Lead]:
    leads = []
    for fn in DETECTORS.values():
        leads.extend(fn(g))
    return leads
