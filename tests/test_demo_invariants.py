"""
Invariants that protect the live demo.

Every test here corresponds to a defect that was found by running the
merged stack against the real model on 2026-09-12 and that the existing
86 tests happily passed over. They are grouped by what they protect
rather than by module, because that's how they'll be read when one of
them goes red an hour before the demo.
"""
from __future__ import annotations

import json

import networkx as nx
import pytest
from fastapi.testclient import TestClient

from agent import loop as loop_module
from agent.guardrail import validate_case_file
from agent.tools import check_blacklist
from api.main import app
from api.state import state
from graph.detectors import DETECTORS
from shared.schemas import (
    AccusationClaim,
    CaseFile,
    GraphEdge,
    GraphExport,
    GraphNode,
    InvestigationStepType,
)


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


def _claim(rfc: str, amount: float, edge_ids: list[str]) -> AccusationClaim:
    return AccusationClaim(supplier_rfc=rfc, rule_broken="payment with no matching invoice",
                           peso_amount=amount, evidence_edge_ids=edge_ids)


def _trail(edge_ids: list[str], edge_type: str = "EXECUTED_PAYMENT") -> GraphExport:
    return GraphExport(
        nodes=[GraphNode(id="a", type="Company", label="A"),
               GraphNode(id="b", type="Company", label="B")],
        edges=[GraphEdge(id=eid, source="a", target="b", type=edge_type) for eid in edge_ids],
    )


# ---------------------------------------------------------------------------
# The case file must not claim money it can't back
# ---------------------------------------------------------------------------

def test_total_at_risk_follows_the_surviving_claims():
    """A dropped accusation used to leave its pesos in the total, so the
    UI printed "0 acusados / $54,594.75 en riesgo" in large type."""
    draft = CaseFile(
        investigation_id="inv-1", scheme_narrative="test",
        implicated_suppliers=[
            _claim("GOOD1", 100.0, ["real-edge"]),
            _claim("BAD1", 54_494.75, ["ghost-edge"]),   # will be dropped
        ],
        evidence_trail=_trail(["real-edge"]),
        leads_not_pursued=[], total_amount_at_risk=54_594.75,
    )

    cleaned, rejections = validate_case_file(nx.MultiDiGraph(), draft)

    assert len(cleaned.implicated_suppliers) == 1
    assert len(rejections) == 1
    assert cleaned.total_amount_at_risk == 100.0


def test_total_at_risk_is_zero_when_every_claim_is_dropped():
    draft = CaseFile(
        investigation_id="inv-2", scheme_narrative="test",
        implicated_suppliers=[_claim("BAD1", 1000.0, ["does-not-exist"])],
        evidence_trail=_trail(["other-edge"]),
        leads_not_pursued=[], total_amount_at_risk=1000.0,
    )
    cleaned, _ = validate_case_file(nx.MultiDiGraph(), draft)
    assert cleaned.implicated_suppliers == []
    assert cleaned.total_amount_at_risk == 0.0


# ---------------------------------------------------------------------------
# Never accuse a company SAT cleared
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("status,should_flag", [
    ("definitivo", True),
    ("presunto", True),
    ("desvirtuado", False),          # the company disproved the presumption
    ("sentencia_favorable", False),  # the company won in court
    ("none", False),
])
def test_blacklist_detector_only_flags_accusable_statuses(status, should_flag):
    g = nx.MultiDiGraph()
    g.add_node("RFC1", type="Company", label="Proveedor", blacklist_status=status)
    leads = DETECTORS["blacklist_match"](g)
    assert bool(leads) is should_flag, f"status {status!r} flagged={bool(leads)}"


def test_check_blacklist_separates_existing_from_blacklisted():
    """exists=true used to be reported as found=true, and the model read
    that as "it's on the blacklist" and built a case on a clean company."""
    g = nx.MultiDiGraph()
    g.add_node("CLEAN1", type="Company", label="Clean", blacklist_status="none")
    g.add_node("GUILTY1", type="Company", label="Guilty", blacklist_status="definitivo")
    g.add_node("CLEARED1", type="Company", label="Cleared", blacklist_status="sentencia_favorable")

    clean = check_blacklist(g, "CLEAN1")
    assert clean["exists"] is True and clean["on_blacklist"] is False
    assert "found" not in clean          # the ambiguous key is gone for good

    guilty = check_blacklist(g, "GUILTY1")
    assert guilty["exists"] is True and guilty["on_blacklist"] is True

    cleared = check_blacklist(g, "CLEARED1")
    assert cleared["exists"] is True and cleared["on_blacklist"] is False
    assert "CLEARED" in cleared["note"].upper()

    missing = check_blacklist(g, "NOPE")
    assert missing["exists"] is False and missing["on_blacklist"] is False


# ---------------------------------------------------------------------------
# The SSE stream must always close
# ---------------------------------------------------------------------------

def test_investigate_stream_closes_with_an_error_event(client, monkeypatch):
    """If run_investigation raises, the worker thread used to die before
    queueing the sentinel and event_stream() blocked on queue.get()
    forever: SSE open, UI spinning, no message. Worst case live."""
    def explode(*args, **kwargs):
        raise ValueError("supplier_rfc\n  Field required")

    monkeypatch.setattr("api.main.run_investigation", explode)
    client.post("/estate/generate", json={"seed": 42, "num_blacklisted": 0})

    with client.stream("POST", "/investigate", json={"hint": "x"}) as resp:
        assert resp.status_code == 200
        payloads = [json.loads(line[len("data: "):])
                    for line in resp.iter_lines() if line.startswith("data: ")]

    # It closed (we got here), and it said why.
    assert any(p.get("type") == "error" for p in payloads)
    assert any("supplier_rfc" in p.get("message", "") for p in payloads)
    assert state.investigation_running is False   # released for the next run


def test_investigate_refuses_a_second_concurrent_run(client):
    client.post("/estate/generate", json={"seed": 42, "num_blacklisted": 0})
    state.investigation_running = True
    resp = client.post("/investigate", json={"hint": "x"})
    assert resp.status_code == 409


# ---------------------------------------------------------------------------
# An empty-handed result is not an amnesiac one
# ---------------------------------------------------------------------------

def test_exhausted_run_keeps_the_evidence_it_gathered():
    """The budget-exhausted branch used to export an empty graph, so 8
    real observations with valid ids showed up as nodes=0 edges=0."""
    g = nx.MultiDiGraph()
    g.add_node("acc-A", type="BankAccount", label="A")
    g.add_node("acc-B", type="BankAccount", label="B")
    g.add_edge("acc-A", "acc-B", key="pay-1", type="EXECUTED_PAYMENT", amount=400_000)

    case_file = loop_module._empty_case_file(
        "inv-3", "budget exhausted", g, {"acc-A", "acc-B"}, {"pay-1"})

    assert case_file.implicated_suppliers == []          # still honest
    assert len(case_file.evidence_trail.nodes) == 2      # but not amnesiac
    assert [e.id for e in case_file.evidence_trail.edges] == ["pay-1"]


def test_empty_case_file_without_a_graph_is_still_valid():
    case_file = loop_module._empty_case_file("inv-4", "cancelled")
    assert case_file.evidence_trail.nodes == []
    assert case_file.total_amount_at_risk == 0.0


# ---------------------------------------------------------------------------
# The last step must ask for a verdict
# ---------------------------------------------------------------------------

def test_final_step_demands_a_case_file(monkeypatch):
    """The measured failure: the agent had the fraud and spent its whole
    budget on more tool calls, so the run fell through to the exhausted
    branch and threw everything away."""
    prompts: list[str] = []

    class FakeResult:
        cancelled = False
        content = json.dumps({"thought": "looking", "action": "run_detector",
                              "action_input": {"name": "cycle"}})

    def fake_model(prompt, on_notice=None):
        prompts.append(prompt)
        return FakeResult()

    monkeypatch.setattr(loop_module, "_call_reasoning_model", fake_model)
    loop_module.run_investigation(nx.MultiDiGraph(), "hint", max_steps=3)

    assert len(prompts) == 3
    assert "Respond with the next step now." in prompts[0]
    assert "LAST step" in prompts[-1]
    assert "final_case_file" in prompts[-1]


def test_max_steps_is_a_parameter(monkeypatch):
    """MAX_STEPS was read once at import, so the API could not change the
    budget without restarting the process."""
    calls = []

    class FakeResult:
        cancelled = False
        content = json.dumps({"thought": "t", "action": "run_detector",
                              "action_input": {"name": "cycle"}})

    monkeypatch.setattr(loop_module, "_call_reasoning_model",
                        lambda prompt, on_notice=None: (calls.append(1), FakeResult())[1])
    loop_module.run_investigation(nx.MultiDiGraph(), "hint", max_steps=2)
    assert len(calls) == 2


# ---------------------------------------------------------------------------
# A malformed final_case_file must not kill the run
# ---------------------------------------------------------------------------

def test_malformed_final_case_file_is_retried_not_fatal(monkeypatch):
    """qwen2.5:7b returning {"rfc": ...} instead of {"supplier_rfc": ...}
    used to raise ValidationError straight out of run_investigation."""
    responses = [
        # wrong key name -> pydantic rejects it
        json.dumps({"thought": "done", "final_case_file": {
            "scheme_narrative": "fraud",
            "implicated_suppliers": [{"rfc": "X", "rule_broken": "r", "peso_amount": 1}],
        }}),
        # then a well-formed, empty-handed one
        json.dumps({"thought": "ok", "final_case_file": {
            "scheme_narrative": "nothing provable", "implicated_suppliers": [],
        }}),
    ]
    steps = []

    def fake_model(prompt, on_notice=None):
        class R:
            cancelled = False
            content = responses.pop(0)
        return R()

    monkeypatch.setattr(loop_module, "_call_reasoning_model", fake_model)
    monkeypatch.setattr(loop_module, "_synthesize_narrative", lambda cleaned, emit: cleaned)

    case_file = loop_module.run_investigation(
        nx.MultiDiGraph(), "hint", on_step=steps.append, max_steps=4)

    assert case_file.scheme_narrative == "nothing provable"
    assert any("rejected" in s.content for s in steps)


# ---------------------------------------------------------------------------
# /ask must answer, not apologise
# ---------------------------------------------------------------------------

def test_ask_endpoint_is_no_longer_a_stub(client, monkeypatch):
    monkeypatch.setattr("agent.qa.call_cloud_model", lambda prompt, **kw: "Because its RFC is shared.")

    state.case_files["inv-9"] = CaseFile(
        investigation_id="inv-9", scheme_narrative="shell ring",
        implicated_suppliers=[_claim("RFC1", 400_000.0, ["pay-1"])],
        evidence_trail=_trail(["pay-1"]), leads_not_pursued=[],
        total_amount_at_risk=400_000.0,
    )

    body = client.post("/case-file/inv-9/ask", json={"question": "why RFC1?"}).json()

    assert "[stub]" not in body["answer"]
    assert body["answer"] == "Because its RFC is shared."
    # Grounding the judge can check us against.
    assert "RFC1" in body["referenced_ids"]
    assert "pay-1" in body["referenced_ids"]


def test_ask_falls_back_to_the_lan_model_without_a_cloud_key(client, monkeypatch):
    """No CLOUD_LLM_API_KEY is the normal state; the judge's question is
    the most watched moment of the demo, so it must still answer."""
    def no_key(prompt, **kwargs):
        raise RuntimeError("CLOUD_LLM_API_KEY is not set")

    class LocalResult:
        content = "Answered by the LAN model."

    monkeypatch.setattr("agent.qa.call_cloud_model", no_key)
    monkeypatch.setattr("agent.qa.complete_result", lambda prompt, **kw: LocalResult())

    state.case_files["inv-10"] = CaseFile(
        investigation_id="inv-10", scheme_narrative="n",
        implicated_suppliers=[], evidence_trail=_trail([]),
        leads_not_pursued=[], total_amount_at_risk=0.0,
    )

    body = client.post("/case-file/inv-10/ask", json={"question": "q"}).json()
    assert body["answer"] == "Answered by the LAN model."


# ---------------------------------------------------------------------------
# The UI needs to know which nodes a step actually touched
# ---------------------------------------------------------------------------

def test_observations_carry_the_ids_they_confirmed(monkeypatch):
    """The action step can't carry them: run_detector's action_input is
    {"name": "blacklist_match"} -- a detector name, not a node -- so a
    run that opens with a sweep gave the graph nothing to point at, and
    the live highlight never fired."""
    graph = nx.MultiDiGraph()
    graph.add_node("RFC1", type="Company", label="Listada", blacklist_status="definitivo")
    graph.add_node("RFC2", type="Company", label="Limpia", blacklist_status="none")

    responses = [
        json.dumps({"thought": "sweep", "action": "run_detector",
                    "action_input": {"name": "blacklist_match"}}),
        json.dumps({"thought": "done", "final_case_file": {
            "scheme_narrative": "n", "implicated_suppliers": []}}),
    ]

    class Result:
        cancelled = False
        def __init__(self, content): self.content = content

    monkeypatch.setattr(loop_module, "_call_reasoning_model",
                        lambda prompt, on_notice=None: Result(responses.pop(0)))
    monkeypatch.setattr(loop_module, "_synthesize_narrative", lambda cleaned, emit: cleaned)

    steps = []
    loop_module.run_investigation(graph, "hint", on_step=steps.append, max_steps=4)

    observations = [s for s in steps if s.type == InvestigationStepType.OBSERVATION]
    assert observations, "no observation step"
    assert "RFC1" in observations[0].referenced_ids      # the flagged company
    assert "RFC2" not in observations[0].referenced_ids  # not flagged, not touched


def test_a_failed_tool_call_confirms_nothing(monkeypatch):
    """An error observation must not light up a node that was never
    confirmed to exist."""
    responses = [
        json.dumps({"thought": "guess", "action": "query_entity",
                    "action_input": {"entity_id": "DOES-NOT-EXIST"}}),
        json.dumps({"thought": "done", "final_case_file": {
            "scheme_narrative": "n", "implicated_suppliers": []}}),
    ]

    class Result:
        cancelled = False
        def __init__(self, content): self.content = content

    monkeypatch.setattr(loop_module, "_call_reasoning_model",
                        lambda prompt, on_notice=None: Result(responses.pop(0)))
    monkeypatch.setattr(loop_module, "_synthesize_narrative", lambda cleaned, emit: cleaned)

    steps = []
    loop_module.run_investigation(nx.MultiDiGraph(), "hint", on_step=steps.append, max_steps=4)

    observations = [s for s in steps if s.type == InvestigationStepType.OBSERVATION]
    assert observations[0].referenced_ids == []
