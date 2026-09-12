"""
API tests (Diego). TestClient over api.main.app -- no Ollama, no cloud
key, no network: anything that would reach a model is monkeypatched.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from api.main import app
from api.state import state
from shared.schemas import AccusationClaim, CaseFile, GraphEdge, GraphExport, GraphNode


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture(autouse=True)
def clean_state():
    state.estate = None
    state.graph = None
    state.case_files = {}
    state.investigation_running = False
    yield
    state.investigation_running = False


def sample_case_file(investigation_id: str = "inv-1") -> CaseFile:
    return CaseFile(
        investigation_id=investigation_id,
        scheme_narrative="AUD010101XXX paid AANS850115SE5 with no matching invoices.",
        implicated_suppliers=[AccusationClaim(
            supplier_rfc="AANS850115SE5", rule_broken="Payment with no matching invoice",
            peso_amount=180_114.65, evidence_edge_ids=["pay-1"])],
        evidence_trail=GraphExport(
            nodes=[GraphNode(id="AANS850115SE5", type="Company", label="Proveedor")],
            edges=[GraphEdge(id="pay-1", source="AUD010101XXX",
                             target="AANS850115SE5", type="EXECUTED_PAYMENT")]),
        leads_not_pursued=[], total_amount_at_risk=180_114.65)


# ---------------------------------------------------------------------------
# /case-file/{id}/ask -- the judge's follow-up (FR-19)
# ---------------------------------------------------------------------------

def test_ask_unknown_investigation_id_is_404(client):
    assert client.post("/case-file/does-not-exist/ask",
                       json={"question": "why?"}).status_code == 404


def test_ask_hands_off_to_the_agent_and_is_not_a_stub(client, monkeypatch):
    """The endpoint must call agent.qa.answer_question, not answer on its
    own: all grounding logic lives in agent/, api/ only resolves the id."""
    seen = {}

    def fake_answer(g, case_file, question):
        seen["case_file_id"] = case_file.investigation_id
        seen["question"] = question
        from shared.schemas import AskResponse
        return AskResponse(answer="Because its RFC is shared.", referenced_ids=["pay-1"])

    monkeypatch.setattr("api.main.answer_question", fake_answer)
    state.case_files["inv-1"] = sample_case_file()

    body = client.post("/case-file/inv-1/ask", json={"question": "why RFC1?"}).json()

    assert seen == {"case_file_id": "inv-1", "question": "why RFC1?"}
    assert body["answer"] == "Because its RFC is shared."
    assert body["referenced_ids"] == ["pay-1"]
    assert "[stub]" not in body["answer"]


def test_ask_answer_question_signature_is_the_frozen_one():
    """agent/qa.py calls this signature frozen; api/ must keep matching
    it rather than growing its own Q&A logic."""
    import inspect

    from agent.qa import answer_question

    assert list(inspect.signature(answer_question).parameters) == ["g", "case_file", "question"]
