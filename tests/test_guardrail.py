import networkx as nx

from agent.guardrail import validate_case_file
from agent.loop import _filter_graph_to_subgraph
from graph.builder import to_graph_export
from shared.schemas import AccusationClaim, CaseFile, GraphEdge, GraphExport


def _graph_export_with_edge(edge_id: str, edge_type: str = "EXECUTED_PAYMENT") -> GraphExport:
    return GraphExport(nodes=[], edges=[GraphEdge(id=edge_id, source="a", target="b", type=edge_type)])


def _graph_export_with_payment(edge_id: str, reference: str | None) -> GraphExport:
    return GraphExport(nodes=[], edges=[
        GraphEdge(id=edge_id, source="a", target="b", type="EXECUTED_PAYMENT",
                  attributes={"amount": 400000.0, "reference": reference}),
    ])


def _sample_graph() -> nx.MultiDiGraph:
    """A tiny graph with two real payment edges, only one of which a
    given test will treat as 'touched' by the agent."""
    g = nx.MultiDiGraph()
    g.add_node("RFC1", type="Company", label="Shell Co")
    g.add_node("RFC2", type="Company", label="Real Co")
    g.add_node("acc-RFC1", type="BankAccount", label="CLABE1")
    g.add_node("acc-RFC2", type="BankAccount", label="CLABE2")
    g.add_edge("acc-RFC1", "acc-RFC2", key="pay-1", type="EXECUTED_PAYMENT", amount=1000)
    g.add_edge("acc-RFC2", "acc-RFC1", key="pay-2", type="EXECUTED_PAYMENT", amount=500)
    return g


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


def test_filter_graph_to_subgraph_keeps_only_touched_edges():
    g = _sample_graph()
    sub = _filter_graph_to_subgraph(g, touched_node_ids=set(), touched_edge_ids={"pay-1"})
    export = to_graph_export(sub)
    assert {e.id for e in export.edges} == {"pay-1"}
    assert {n.id for n in export.nodes} == {"acc-RFC1", "acc-RFC2"}


def test_filter_graph_to_subgraph_includes_directly_touched_nodes_with_no_edges():
    g = _sample_graph()
    sub = _filter_graph_to_subgraph(g, touched_node_ids={"RFC1"}, touched_edge_ids=set())
    export = to_graph_export(sub)
    assert export.edges == []
    assert {n.id for n in export.nodes} == {"RFC1"}


def test_guardrail_rejects_claim_citing_a_real_edge_the_agent_never_touched():
    """evidence_trail is now the touched-only subgraph (agent/loop.py's
    _filter_graph_to_subgraph), not the whole graph -- so a claim citing
    a real edge the agent's tool calls never actually surfaced must still
    be dropped, even though that edge genuinely exists somewhere in g."""
    g = _sample_graph()
    subgraph = _filter_graph_to_subgraph(g, touched_node_ids=set(), touched_edge_ids={"pay-1"})
    evidence = to_graph_export(subgraph)

    draft = CaseFile(
        investigation_id="inv-1", scheme_narrative="test",
        implicated_suppliers=[
            AccusationClaim(supplier_rfc="RFC1", rule_broken="round-tripping", peso_amount=500,
                             evidence_edge_ids=["pay-2"]),
        ],
        evidence_trail=evidence,
        leads_not_pursued=[], total_amount_at_risk=500,
    )
    cleaned, rejections = validate_case_file(g, draft)
    assert cleaned.implicated_suppliers == []
    assert len(rejections) == 1


def test_guardrail_rejects_claim_backed_only_by_a_relational_edge():
    """A SHARES_PHONE edge is real and resolves fine, but by itself it
    doesn't establish a rule violation with a peso amount (FR-16) --
    see the ACCUSATION_WORTHY_EDGE_TYPES comment in agent/guardrail.py
    for the reasoning behind rejecting this."""
    draft = CaseFile(
        investigation_id="inv-1", scheme_narrative="test",
        implicated_suppliers=[
            AccusationClaim(supplier_rfc="RFC1", rule_broken="shell-company cluster",
                             peso_amount=1000, evidence_edge_ids=["shares-phone-edge"]),
        ],
        evidence_trail=_graph_export_with_edge("shares-phone-edge", edge_type="SHARES_PHONE"),
        leads_not_pursued=[], total_amount_at_risk=1000,
    )
    cleaned, rejections = validate_case_file(nx.MultiDiGraph(), draft)
    assert cleaned.implicated_suppliers == []
    assert len(rejections) == 1


def test_guardrail_rejects_claim_whose_cited_payment_has_a_matching_invoice():
    """Live repro (case_files/bcb3fb14-8f61-40d6-8fbf-2a5b90c405fc.json,
    RFC IDQ053050PCD): the agent cited a real, correctly-typed
    EXECUTED_PAYMENT edge for a "payment with no matching invoice"
    claim, but that edge's own reference field pointed at a real
    invoice -- the opposite of what was claimed. Existence + type
    checks alone let this through; the guardrail must also check that
    the cited edge's own data supports the specific rule asserted."""
    draft = CaseFile(
        investigation_id="inv-1", scheme_narrative="test",
        implicated_suppliers=[
            AccusationClaim(supplier_rfc="IDQ053050PCD",
                             rule_broken="Payment with no matching invoice",
                             peso_amount=400000.0, evidence_edge_ids=["pay-matched"]),
        ],
        evidence_trail=_graph_export_with_payment(
            "pay-matched", reference="4e622b23-9999-48d9-8da5-93fc44b770fe"),
        leads_not_pursued=[], total_amount_at_risk=400000.0,
    )
    cleaned, rejections = validate_case_file(nx.MultiDiGraph(), draft)
    assert cleaned.implicated_suppliers == []
    assert len(rejections) == 1
    assert "contradicting the claimed rule" in rejections[0]


def test_guardrail_keeps_claim_citing_a_genuinely_unmatched_payment():
    """The legitimate version of the case above: the cited payment
    really has no reference, so "no matching invoice" is true and the
    claim should survive."""
    draft = CaseFile(
        investigation_id="inv-1", scheme_narrative="test",
        implicated_suppliers=[
            AccusationClaim(supplier_rfc="LVD269539B4K",
                             rule_broken="Payment with no matching invoice",
                             peso_amount=120000.0, evidence_edge_ids=["pay-unmatched"]),
        ],
        evidence_trail=_graph_export_with_payment("pay-unmatched", reference=None),
        leads_not_pursued=[], total_amount_at_risk=120000.0,
    )
    cleaned, rejections = validate_case_file(nx.MultiDiGraph(), draft)
    assert len(cleaned.implicated_suppliers) == 1
    assert rejections == []


def test_guardrail_keeps_valid_claim_and_drops_only_the_invalid_one():
    valid = AccusationClaim(supplier_rfc="RFC1", rule_broken="blacklist", peso_amount=1000,
                             evidence_edge_ids=["real-edge"])
    invalid = AccusationClaim(supplier_rfc="RFC2", rule_broken="round-tripping", peso_amount=500,
                               evidence_edge_ids=["does-not-exist"])
    draft = CaseFile(
        investigation_id="inv-1", scheme_narrative="test",
        implicated_suppliers=[valid, invalid],
        evidence_trail=_graph_export_with_edge("real-edge"),
        leads_not_pursued=[], total_amount_at_risk=1500,
    )
    cleaned, rejections = validate_case_file(nx.MultiDiGraph(), draft)
    assert cleaned.implicated_suppliers == [valid]
    assert len(rejections) == 1
    assert "RFC2" in rejections[0]


# ---------------------------------------------------------------------------
# The peso figure must be backed by the money on the cited edges (FR-16)
# ---------------------------------------------------------------------------

def _priced_trail(*edges: tuple[str, float]) -> GraphExport:
    """An evidence trail whose payment edges carry real amounts, the way
    graph/builder.py exports them (attributes.amount)."""
    return GraphExport(nodes=[], edges=[
        GraphEdge(id=edge_id, source="acc-A", target="acc-B",
                  type="EXECUTED_PAYMENT", attributes={"amount": amount})
        for edge_id, amount in edges])


def _priced_draft(claims: list[AccusationClaim], *edges: tuple[str, float]) -> CaseFile:
    return CaseFile(
        investigation_id="inv-amt", scheme_narrative="test",
        implicated_suppliers=claims, evidence_trail=_priced_trail(*edges),
        leads_not_pursued=[],
        total_amount_at_risk=sum(c.peso_amount for c in claims))


def _claim(amount: float, edge_ids: list[str], rfc: str = "RFC1") -> AccusationClaim:
    return AccusationClaim(supplier_rfc=rfc, rule_broken="payment with no matching invoice",
                           peso_amount=amount, evidence_edge_ids=edge_ids)


def test_guardrail_rejects_an_amount_the_cited_edges_do_not_back():
    """Resolving the edge ids only proved the rule broken. Nothing checked
    the figure itself, so a claim of 99 million citing one 78,891.61
    payment passed -- and that figure is what the UI prints in large
    type."""
    draft = _priced_draft([_claim(99_000_000.0, ["pay-1"])], ("pay-1", 78_891.61))

    cleaned, rejections = validate_case_file(nx.MultiDiGraph(), draft)

    assert cleaned.implicated_suppliers == []
    assert cleaned.total_amount_at_risk == 0.0
    assert "99,000,000.00" in rejections[0] and "78,891.61" in rejections[0]


def test_guardrail_keeps_a_claim_that_matches_its_edges():
    draft = _priced_draft(
        [_claim(220_413.74, ["pay-1", "pay-2", "pay-3", "pay-4"])],
        ("pay-1", 78_891.61), ("pay-2", 33_509.47),
        ("pay-3", 32_763.57), ("pay-4", 75_249.09))

    cleaned, rejections = validate_case_file(nx.MultiDiGraph(), draft)

    assert len(cleaned.implicated_suppliers) == 1
    assert rejections == []
    assert cleaned.total_amount_at_risk == 220_413.74


def test_guardrail_allows_claiming_less_than_the_evidence_supports():
    """Under-claiming is conservative, not dishonest: the agent may cite
    context edges it isn't accusing over. Only over-claiming is a lie."""
    draft = _priced_draft(
        [_claim(100_000.0, ["pay-1", "pay-2"])],
        ("pay-1", 78_891.61), ("pay-2", 75_249.09))

    cleaned, rejections = validate_case_file(nx.MultiDiGraph(), draft)

    assert len(cleaned.implicated_suppliers) == 1
    assert rejections == []


def test_guardrail_tolerates_rounding_in_the_models_arithmetic():
    draft = _priced_draft([_claim(79_286.0, ["pay-1"])], ("pay-1", 78_891.61))
    cleaned, _ = validate_case_file(nx.MultiDiGraph(), draft)
    assert len(cleaned.implicated_suppliers) == 1      # 0.5% over, within 1%


def test_guardrail_still_accepts_a_claim_whose_edges_carry_no_amount():
    """Invoice and relational edges have no amount, and no detector fills
    supporting_edge_ids yet. Rejecting those would kill legitimate
    accusations, so an unpriced trail keeps the old behaviour."""
    draft = CaseFile(
        investigation_id="inv-unpriced", scheme_narrative="test",
        implicated_suppliers=[_claim(1000.0, ["inv-1"])],
        evidence_trail=GraphExport(nodes=[], edges=[
            GraphEdge(id="inv-1", source="RFC1", target="uuid-1", type="ISSUED_INVOICE")]),
        leads_not_pursued=[], total_amount_at_risk=1000.0)

    cleaned, rejections = validate_case_file(nx.MultiDiGraph(), draft)

    assert len(cleaned.implicated_suppliers) == 1
    assert rejections == []


def test_guardrail_counts_a_duplicated_accusation_once():
    """Two identical claims passed every check and their pesos were added
    twice: 157,783.22 reported off a single 78,891.61 payment."""
    claim = _claim(78_891.61, ["pay-1"])
    draft = _priced_draft([claim, claim.model_copy()], ("pay-1", 78_891.61))

    cleaned, rejections = validate_case_file(nx.MultiDiGraph(), draft)

    assert len(cleaned.implicated_suppliers) == 1
    assert cleaned.total_amount_at_risk == 78_891.61
    assert "duplicate" in rejections[0]


def test_guardrail_keeps_two_real_accusations_against_the_same_supplier():
    """Same RFC, different evidence, is two findings -- not a duplicate."""
    draft = _priced_draft(
        [_claim(78_891.61, ["pay-1"]), _claim(75_249.09, ["pay-2"])],
        ("pay-1", 78_891.61), ("pay-2", 75_249.09))

    cleaned, rejections = validate_case_file(nx.MultiDiGraph(), draft)

    assert len(cleaned.implicated_suppliers) == 2
    assert rejections == []
    assert cleaned.total_amount_at_risk == 154_140.70
