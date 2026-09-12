"""
Hand-rolled ReAct investigation loop (FR-11 - FR-15).

Design: the local model (Ollama) drives every intermediate step; a
cloud model call is reserved for the case-file synthesis and the /ask
follow-up (NFR-4's call-budget target of <=2 cloud calls per
investigation). This module intentionally avoids a heavy agent
framework (see the SRS, section 2.4) so the evidence guardrail has a
single, obvious place to run: right before a case file is returned.
"""
from __future__ import annotations

import json
import os
import uuid
from typing import Callable

import networkx as nx
import requests

from agent.guardrail import validate_case_file
from agent.prompts import SYSTEM_PROMPT
from agent.tools import TOOLS
from graph.builder import to_graph_export
from shared.schemas import (
    AccusationClaim,
    CaseFile,
    DroppedLead,
    InvestigationStep,
    InvestigationStepType,
)

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434/api/generate")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "llama3.1:8b")
MAX_STEPS = int(os.environ.get("AGENT_MAX_STEPS", "12"))


def _call_local_model(prompt: str) -> str:
    """Calls Ollama's /api/generate. This is the only function you need
    to change if the demo machine ends up using a different local
    serving setup (e.g. llama.cpp's server, LM Studio)."""
    resp = requests.post(OLLAMA_URL, json={
        "model": OLLAMA_MODEL, "prompt": prompt, "stream": False,
        "options": {"temperature": 0.2},
    }, timeout=60)
    resp.raise_for_status()
    return resp.json()["response"]


def _parse_step(raw: str) -> dict:
    """Model responses are expected to be a single JSON object per the
    system prompt. Real models occasionally wrap it in prose or code
    fences -- this does the minimal cleanup; tighten once you're
    testing against the real model the team settles on."""
    text = raw.strip()
    if text.startswith("```"):
        text = text.strip("`")
        text = text.split("\n", 1)[-1] if "\n" in text else text
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError(f"Model did not return JSON: {raw!r}")
    return json.loads(text[start:end + 1])


def run_investigation(
    g: nx.MultiDiGraph,
    hint: str,
    on_step: Callable[[InvestigationStep], None] | None = None,
) -> CaseFile:
    """Runs the ReAct loop end to end and returns a validated CaseFile.

    `on_step` is an optional callback -- api/main.py wires this to the
    SSE stream (FR-20) so the UI can render each step as it happens.
    """
    investigation_id = str(uuid.uuid4())
    transcript = [f"HINT: {hint}"]
    step_index = 0

    def emit(step_type: InvestigationStepType, content: str, refs: list[str] | None = None) -> None:
        nonlocal step_index
        step = InvestigationStep(investigation_id=investigation_id, step_index=step_index,
                                  type=step_type, content=content, referenced_ids=refs or [])
        step_index += 1
        if on_step:
            on_step(step)

    loop_count = 0
    while loop_count < MAX_STEPS:
        loop_count += 1
        prompt = f"{SYSTEM_PROMPT}\n\nTranscript so far:\n" + "\n".join(transcript) + \
            "\n\nRespond with the next step now."

        try:
            raw = _call_local_model(prompt)
        except requests.RequestException as e:
            emit(InvestigationStepType.OBSERVATION, f"[local model unreachable: {e}]")
            break

        try:
            parsed = _parse_step(raw)
        except ValueError as e:
            emit(InvestigationStepType.OBSERVATION, f"[parse error, retrying] {e}")
            continue

        emit(InvestigationStepType.THOUGHT, parsed.get("thought", ""))

        if "final_case_file" in parsed:
            draft = _build_case_file(investigation_id, g, parsed["final_case_file"])
            cleaned, rejections = validate_case_file(g, draft)
            for reason in rejections:
                emit(InvestigationStepType.LEAD_DROPPED, reason)
            emit(InvestigationStepType.CONCLUSION, cleaned.scheme_narrative)
            return cleaned

        action = parsed.get("action")
        action_input = parsed.get("action_input", {}) or {}
        emit(InvestigationStepType.ACTION, f"{action}({action_input})",
             refs=[str(v) for v in action_input.values()])

        if action not in TOOLS:
            observation = {"error": f"Unknown tool '{action}'"}
        else:
            try:
                observation = TOOLS[action](g, **action_input)
            except TypeError as e:
                observation = {"error": f"Bad arguments for {action}: {e}"}

        obs_str = json.dumps(observation, default=str)
        emit(InvestigationStepType.OBSERVATION, obs_str)
        transcript.append(f"ACTION: {action}({action_input})")
        transcript.append(f"OBSERVATION: {obs_str}")

    # Ran out of steps without a final case file -- return an honest
    # empty-handed result rather than forcing a guess (Judgment criterion).
    emit(InvestigationStepType.CONCLUSION,
         "Investigation budget exhausted without sufficient evidence for an accusation.")
    return CaseFile(
        investigation_id=investigation_id,
        scheme_narrative="No fraud could be proven within the investigation budget.",
        implicated_suppliers=[], evidence_trail=to_graph_export(nx.MultiDiGraph()),
        leads_not_pursued=[], total_amount_at_risk=0.0,
    )


def _build_case_file(investigation_id: str, g: nx.MultiDiGraph, raw: dict) -> CaseFile:
    claims = [AccusationClaim(**c) for c in raw.get("implicated_suppliers", [])]
    dropped = [DroppedLead(**d) for d in raw.get("leads_not_pursued", [])]
    total = sum(c.peso_amount for c in claims)

    # TODO (Dev 2): the evidence trail should be the subgraph touching
    # only the edges the agent actually cited/observed, not the whole
    # graph -- instrument the tool calls in the loop above to collect
    # observed edge ids and pass them through here once that's wired up.
    evidence = to_graph_export(g)

    return CaseFile(
        investigation_id=investigation_id, scheme_narrative=raw.get("scheme_narrative", ""),
        implicated_suppliers=claims, evidence_trail=evidence,
        leads_not_pursued=dropped, total_amount_at_risk=total,
    )
