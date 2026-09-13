"""
The seam between the ReAct loop and the LAN model hop.

These are the cases that decide whether a demo recovers or dies: the
server answers, the server is gone, the user hits Detener.
"""
from __future__ import annotations

import json

import networkx as nx
import requests

from agent import ollama_client
from agent.loop import run_investigation
from shared.schemas import InvestigationStepType
from tests.test_ollama_client import FakeResponse, chunk, done_chunk, fake_transport

FINAL_STEP = {
    "thought": "Ya tengo suficiente.",
    "final_case_file": {
        "scheme_narrative": "Facturación falsa vía proveedor fantasma.",
        "implicated_suppliers": [],
        "leads_not_pursued": [],
    },
}


def _collect(graph=None):
    steps = []
    case_file = run_investigation(graph if graph is not None else nx.MultiDiGraph(),
                                  hint="algo raro en los pagos", on_step=steps.append)
    return case_file, steps


def test_loop_reaches_a_case_file_through_the_client(monkeypatch):
    fake_transport(monkeypatch, post=FakeResponse(
        lines=[chunk(json.dumps(FINAL_STEP)), done_chunk()]))

    case_file, steps = _collect()

    assert case_file.scheme_narrative == "Facturación falsa vía proveedor fantasma."
    assert steps[-1].type == InvestigationStepType.CONCLUSION


def test_warehouse_leads_open_the_investigation(monkeypatch):
    """DATA_SOURCE=snowflake attaches the SQL/Cortex leads to the graph.
    They used to trim the graph and vanish; the agent has to see them,
    and so does the live log."""
    calls = fake_transport(monkeypatch, post=FakeResponse(
        lines=[chunk(json.dumps(FINAL_STEP)), done_chunk()]))
    graph = nx.MultiDiGraph()
    graph.graph["warehouse_leads"] = [{
        "id": "l1", "detector": "vague_concept_cortex", "entity_ids": ["MPX402432U1B", "inv-1"],
        "reason": "MPX402432U1B has 1 invoice(s) with a vague, unverifiable concepto",
        "supporting_edge_ids": ["issued-inv-1"]}]

    _, steps = _collect(graph)

    assert steps[0].type == InvestigationStepType.OBSERVATION
    assert steps[0].content.startswith("[Snowflake: 1 lead(s)")
    assert steps[0].referenced_ids == ["MPX402432U1B"]
    assert "vague_concept_cortex" in json.dumps(calls[0]["json"])


def test_a_gemini_narrative_says_so_in_the_log(monkeypatch):
    """Both failure paths of the narrative synthesis always showed up in
    the log; the success did not, so the one cloud call worth showing
    happened invisibly."""
    fake_transport(monkeypatch, post=FakeResponse(
        lines=[chunk(json.dumps(FINAL_STEP)), done_chunk()]))
    monkeypatch.setenv("CLOUD_LLM_API_KEY", "test-key")
    monkeypatch.setattr("agent.loop.call_cloud_model", lambda prompt: "Narrativa para el juez.")

    case_file, steps = _collect()

    assert case_file.scheme_narrative == "Narrativa para el juez."
    assert any(s.content.startswith("[Gemini rewrote") for s in steps)


def test_loop_uses_a_low_temperature_for_reasoning(monkeypatch):
    """Prose temperature (0.7) makes the model wander off the JSON rails."""
    calls = fake_transport(monkeypatch, post=FakeResponse(
        lines=[chunk(json.dumps(FINAL_STEP)), done_chunk()]))
    _collect()
    assert calls[0]["json"]["options"]["temperature"] == 0.2


def test_loop_reports_an_unreachable_server_in_plain_words(monkeypatch):
    fake_transport(monkeypatch, post=requests.exceptions.ConnectionError("laptop asleep"))

    case_file, steps = _collect()

    messages = [s.content for s in steps]
    assert any("No pude conectar" in m for m in messages)
    assert any("firewall" in m for m in messages)
    # ...and it stops instead of hammering a dead server MAX_STEPS times.
    assert len([s for s in steps if s.type == InvestigationStepType.OBSERVATION]) == 1
    assert case_file.implicated_suppliers == []


def test_loop_surfaces_a_missing_model(monkeypatch):
    fake_transport(monkeypatch, post=FakeResponse(
        status_code=404, json_body={"error": "model 'qwen2.5:7b' not found"}))
    _, steps = _collect()
    assert any("ollama pull" in s.content for s in steps)


def test_cancelling_mid_answer_ends_the_investigation(monkeypatch):
    def interrupted(url, **kwargs):
        response = FakeResponse()

        def stream():
            yield json.dumps(chunk('{"thought": "empiezo a')).encode()
            ollama_client.request_cancel()      # user hits Detener
            yield json.dumps(chunk(' investigar"}')).encode()
            yield json.dumps(done_chunk()).encode()

        response.iter_lines = stream
        return response

    fake_transport(monkeypatch, post=interrupted)

    case_file, steps = _collect()

    assert "cancelled" in case_file.scheme_narrative.lower()
    assert steps[-1].type == InvestigationStepType.CONCLUSION
    assert "cancelled" in steps[-1].content.lower()
