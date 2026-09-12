"""
Builds a NetworkX graph from a DataEstate (SRS section 4.1) and derives
the SHARES_* edges that aren't explicit in the raw data.
"""
from __future__ import annotations

import networkx as nx

from shared.schemas import DataEstate, GraphEdge, GraphExport, GraphNode


def build_graph(estate: DataEstate) -> nx.MultiDiGraph:
    g = nx.MultiDiGraph()

    for c in estate.companies:
        g.add_node(c.rfc, type="Company", label=c.name, **c.model_dump(mode="json"))

    for p in estate.people:
        g.add_node(p.id, type="Person", label=p.name, **p.model_dump(mode="json"))

    for a in estate.accounts:
        g.add_node(a.account_id, type="BankAccount", label=a.clabe, **a.model_dump(mode="json"))

    for inv in estate.invoices:
        g.add_node(inv.uuid, type="Invoice", label=inv.folio,
                   **inv.model_dump(mode="json", exclude={"concepts"}))
        g.add_edge(inv.emisor_rfc, inv.uuid, key=f"issued-{inv.uuid}", type="ISSUED_INVOICE")
        g.add_edge(inv.uuid, inv.receptor_rfc, key=f"received-{inv.uuid}", type="RECEIVED_INVOICE")

    for pay in estate.payments:
        g.add_edge(pay.from_account, pay.to_account, key=f"pay-{pay.transaction_id}",
                   type="EXECUTED_PAYMENT", amount=pay.amount, date=str(pay.date),
                   reference=pay.reference, transaction_id=pay.transaction_id)

    # Company -> its own account, inferred from the naming convention
    # the generator uses (acc-<rfc>). Real ownership data would come
    # from explicit OWNS_ACCOUNT records if the estate ever models
    # multiple accounts per company.
    for a in estate.accounts:
        owner_rfc = a.account_id.replace("acc-", "", 1)
        if g.has_node(owner_rfc):
            g.add_edge(owner_rfc, a.account_id, key=f"owns-{a.account_id}", type="OWNS_ACCOUNT")

    _add_shared_attribute_edges(g, estate)
    return g


def _add_shared_attribute_edges(g: nx.MultiDiGraph, estate: DataEstate) -> None:
    """Derives SHARES_ADDRESS / SHARES_PHONE edges -- the raw material
    FR-8's shell-company clustering detector runs over."""
    by_address: dict[str, list[str]] = {}
    by_phone: dict[str, list[str]] = {}
    for c in estate.companies:
        by_address.setdefault(c.address, []).append(c.rfc)
        by_phone.setdefault(c.phone, []).append(c.rfc)

    for group, attr_name in ((by_address, "SHARES_ADDRESS"), (by_phone, "SHARES_PHONE")):
        for rfcs in group.values():
            if len(rfcs) < 2:
                continue
            for i in range(len(rfcs)):
                for j in range(i + 1, len(rfcs)):
                    g.add_edge(rfcs[i], rfcs[j], key=f"{attr_name.lower()}-{rfcs[i]}-{rfcs[j]}", type=attr_name)
                    g.add_edge(rfcs[j], rfcs[i], key=f"{attr_name.lower()}-{rfcs[j]}-{rfcs[i]}", type=attr_name)


def to_graph_export(g: nx.MultiDiGraph) -> GraphExport:
    nodes = [
        GraphNode(id=str(n), type=data.get("type", "Unknown"), label=str(data.get("label", n)),
                  attributes={k: v for k, v in data.items() if k not in ("type", "label")})
        for n, data in g.nodes(data=True)
    ]
    edges = [
        GraphEdge(id=str(key), source=str(u), target=str(v), type=data.get("type", "Unknown"),
                  attributes={k: v for k, v in data.items() if k != "type"})
        for u, v, key, data in g.edges(keys=True, data=True)
    ]
    return GraphExport(nodes=nodes, edges=edges)
