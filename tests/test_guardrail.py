import networkx as nx

from agent.guardrail import validate_case_file
from shared.schemas import AccusationClaim, CaseFile, GraphEdge, GraphExport


def _graph_export_with_edge(edge_id: str) -> GraphExport:
    return GraphExport(nodes=[], edges=[GraphEdge(id=edge_id, source="a", target="b", type="EXECUTED_PAYMENT")])


def test_guardrail_rejects_unbacked_claim():
    draft = CaseFile(
        investigation_id="inv-1", scheme_narrative="test",
        implicated_suppliers=[
            AccusationClaim(supplier_rfc="RFC1", rule_broken="blacklist", peso_amount=1000,
                             evidence_edge_ids=["does-not-exist"]),
        ],
        evidence_trail=_graph_export_with_edge("real-edge"),
        leads_not_pursued=[], total_amount_at_risk=1000,
    )
    cleaned, rejections = validate_case_file(nx.MultiDiGraph(), draft)
    assert cleaned.implicated_suppliers == []
    assert len(rejections) == 1


def test_guardrail_keeps_backed_claim():
    draft = CaseFile(
        investigation_id="inv-1", scheme_narrative="test",
        implicated_suppliers=[
            AccusationClaim(supplier_rfc="RFC1", rule_broken="blacklist", peso_amount=1000,
                             evidence_edge_ids=["real-edge"]),
        ],
        evidence_trail=_graph_export_with_edge("real-edge"),
        leads_not_pursued=[], total_amount_at_risk=1000,
    )
    cleaned, rejections = validate_case_file(nx.MultiDiGraph(), draft)
    assert len(cleaned.implicated_suppliers) == 1
    assert rejections == []


def test_guardrail_rejects_zero_amount():
    draft = CaseFile(
        investigation_id="inv-1", scheme_narrative="test",
        implicated_suppliers=[
            AccusationClaim(supplier_rfc="RFC1", rule_broken="blacklist", peso_amount=0,
                             evidence_edge_ids=["real-edge"]),
        ],
        evidence_trail=_graph_export_with_edge("real-edge"),
        leads_not_pursued=[], total_amount_at_risk=0,
    )
    cleaned, rejections = validate_case_file(nx.MultiDiGraph(), draft)
    assert cleaned.implicated_suppliers == []
    assert len(rejections) == 1
