import networkx as nx

from agent.qa import answer_question
from shared.schemas import AccusationClaim, AskResponse, CaseFile, GraphEdge, GraphExport, GraphNode


def _sample_case_file() -> CaseFile:
    evidence = GraphExport(
        nodes=[
            GraphNode(id="RFC1", type="Company", label="Shell Co",
                      attributes={"blacklist_status": "definitivo"}),
        ],
        edges=[
            GraphEdge(id="pay-1", source="acc-RFC1", target="acc-RFC2", type="EXECUTED_PAYMENT",
                      attributes={"amount": 15000}),
        ],
    )
    return CaseFile(
        investigation_id="inv-qa-test",
        scheme_narrative="RFC1, a SAT-blacklisted shell company, moved $15,000 MXN "
                          "with no matching invoice.",
        implicated_suppliers=[
            AccusationClaim(supplier_rfc="RFC1", rule_broken="On SAT's Art. 69-B blacklist",
                             peso_amount=15000, evidence_edge_ids=["pay-1"]),
        ],
        evidence_trail=evidence,
        leads_not_pursued=[],
        total_amount_at_risk=15000.0,
    )


def test_answer_question_returns_a_nonempty_grounded_answer(monkeypatch):
    """This stubs agent.qa.call_cloud_model instead of hitting a real
    cloud model, since dev machines / CI won't always have
    CLOUD_LLM_API_KEY configured (see .env.example). Once real
    credentials are available, re-run this scenario WITHOUT the
    monkeypatch (call answer_question directly against the live
    OpenRouter model) to confirm end to end that it actually returns a
    grounded, non-empty answer -- this test only proves that
    answer_question() builds a correct prompt and passes the model's
    response straight through to AskResponse, not that a real model
    behaves well."""
    def fake_call_cloud_model(prompt: str) -> str:
        # Sanity-check the prompt actually carries the grounding context
        # (evidence_trail + case file) that FR-19 requires.
        assert "15000" in prompt
        assert "pay-1" in prompt
        return "The total peso amount at risk is $15,000 MXN, tied to payment pay-1."

    monkeypatch.setattr("agent.qa.call_cloud_model", fake_call_cloud_model)

    g = nx.MultiDiGraph()
    response = answer_question(g, _sample_case_file(), "What is the total amount at risk?")

    assert isinstance(response, AskResponse)
    assert response.answer.strip() != ""
