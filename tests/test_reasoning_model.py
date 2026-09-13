"""
AGENT_LLM: the ReAct loop reasoning on the LAN Ollama or on Snowflake
Cortex (agent/reasoning_model.py). No network: cortex_complete is
monkeypatched, and the Ollama side keeps its own tests.
"""
from __future__ import annotations

import json

import networkx as nx
import pytest

from agent import ollama_client, reasoning_model
from agent.loop import run_investigation
from agent.ollama_client import OllamaError
from data.snowflake_client import SnowflakeError
from shared.schemas import InvestigationStepType

FINAL_STEP = json.dumps({
    "thought": "Ya tengo suficiente.",
    "final_case_file": {"scheme_narrative": "Sin fraude probado.",
                        "implicated_suppliers": [], "leads_not_pursued": []},
})


@pytest.fixture
def on_cortex(monkeypatch):
    """AGENT_LLM=cortex with credentials present; returns the recorded
    calls. Each test sets what the fake answers."""
    monkeypatch.setenv("AGENT_LLM", "cortex")
    monkeypatch.setenv("SNOWFLAKE_ACCOUNT", "TEST-ACCOUNT")
    monkeypatch.setenv("SNOWFLAKE_PAT", "test-pat")
    calls: list[dict] = []

    def answer_with(reply):
        def fake(prompt, model, **kwargs):
            calls.append({"prompt": prompt, "model": model, **kwargs})
            if isinstance(reply, Exception):
                raise reply
            return reply() if callable(reply) else reply
        monkeypatch.setattr(reasoning_model, "cortex_complete", fake)
        return calls

    return answer_with


def test_ollama_is_the_default(monkeypatch):
    monkeypatch.delenv("AGENT_LLM", raising=False)
    assert reasoning_model.provider() == "ollama"
    monkeypatch.setenv("AGENT_LLM", "something-else")
    assert reasoning_model.provider() == "ollama"


def test_cortex_is_chosen_case_insensitively(monkeypatch):
    monkeypatch.setenv("AGENT_LLM", " Cortex ")
    assert reasoning_model.provider() == "cortex"
    assert reasoning_model.model_name() == "llama3.1-70b"
    monkeypatch.setenv("CORTEX_MODEL", "llama3.3-70b")
    assert reasoning_model.model_name() == "llama3.3-70b"


def test_a_step_goes_to_cortex_with_the_reasoning_temperature(on_cortex):
    calls = on_cortex(FINAL_STEP)

    case_file = run_investigation(nx.MultiDiGraph(), "hint")

    assert case_file.scheme_narrative == "Sin fraude probado."
    assert calls[0]["model"] == "llama3.1-70b"
    assert calls[0]["temperature"] == 0.2
    assert calls[0]["max_tokens"] == reasoning_model.CORTEX_MAX_TOKENS


def test_cortex_down_ends_the_run_with_a_readable_step(on_cortex):
    """Same as a dead LAN server: say why in the live log, return an
    honest empty case file, never a stack trace."""
    on_cortex(SnowflakeError("Snowflake SQL API returned 401"))
    steps = []

    case_file = run_investigation(nx.MultiDiGraph(), "hint", on_step=steps.append)

    assert case_file.implicated_suppliers == []
    failure = [s.content for s in steps if s.type == InvestigationStepType.OBSERVATION]
    assert failure == ["[Snowflake Cortex (llama3.1-70b): Snowflake Cortex did not answer "
                       "with llama3.1-70b: Snowflake SQL API returned 401]"]


def test_cortex_without_credentials_says_what_to_set(monkeypatch):
    monkeypatch.setenv("AGENT_LLM", "cortex")
    monkeypatch.delenv("SNOWFLAKE_ACCOUNT", raising=False)
    monkeypatch.delenv("SNOWFLAKE_PAT", raising=False)

    with pytest.raises(OllamaError, match="SNOWFLAKE_ACCOUNT"):
        reasoning_model.complete_result("prompt")


def test_detener_during_a_cortex_step_cancels_the_run(on_cortex):
    """Cortex is not streamed, so the cancel lands when the step returns."""
    def pressed_detener_meanwhile():
        ollama_client.request_cancel()
        return FINAL_STEP
    on_cortex(pressed_detener_meanwhile)

    case_file = run_investigation(nx.MultiDiGraph(), "hint")

    assert "cancelled" in case_file.scheme_narrative.lower()


def test_a_previous_cancel_does_not_kill_the_next_run(on_cortex):
    on_cortex(FINAL_STEP)
    ollama_client.request_cancel()      # left over from the last Detener

    case_file = run_investigation(nx.MultiDiGraph(), "hint")

    assert case_file.scheme_narrative == "Sin fraude probado."
