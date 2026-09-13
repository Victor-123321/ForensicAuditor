"""
Hand-rolled ReAct investigation loop (FR-11 - FR-15).

Design: the reasoning model -- the LAN Ollama, or Snowflake Cortex with
AGENT_LLM=cortex (agent/reasoning_model.py) -- drives every intermediate
step (hypothesis, tool selection, observation) -- FR-15. Once it
signals it has enough evidence and returns a raw final_case_file,
the evidence guardrail runs first (agent/guardrail.py, FR-16/FR-17),
and only THEN is one cloud-model call spent (agent/cloud.py) to polish
the already-finalized scheme_narrative into plain language for a
non-technical reader -- implicated_suppliers, evidence_edge_ids and
leads_not_pursued are never touched by that call. This is cloud call
1 of NFR-4's <=2-per-investigation budget; call 2 is the /ask follow-up
(FR-19, api/main.py). This module intentionally avoids a heavy agent
framework (see the SRS, section 2.4) so the evidence guardrail has a
single, obvious place to run: right before a case file is returned.
"""
from __future__ import annotations

import json
import os
import uuid
from typing import Callable

import networkx as nx
from pydantic import ValidationError

from agent.cloud import call_cloud_model, cloud_available
from agent.guardrail import validate_case_file
from agent import reasoning_model
from agent.ollama_client import ChatResult, OllamaError
from agent.prompts import CASE_NARRATIVE_SYSTEM_PROMPT, SYSTEM_PROMPT
from agent.tools import TOOLS
from graph.builder import to_graph_export
from shared.schemas import (
    AccusationClaim,
    CaseFile,
    DroppedLead,
    InvestigationStep,
    InvestigationStepType,
)

MAX_STEPS = int(os.environ.get("AGENT_MAX_STEPS", "12"))

# Reasoning steps need the model to stay on the JSON rails, so they
# override the config's default temperature (which is tuned for prose).
REASONING_TEMPERATURE = 0.2

# Warehouse leads quoted into the transcript. Each one rides along in
# every later prompt, and num_ctx is 8192 on the team's server.
MAX_WAREHOUSE_LEADS = 15


def _call_reasoning_model(prompt: str, on_notice=None) -> ChatResult:
    """Asks the reasoning model for the next step: the LAN Ollama, or
    Snowflake Cortex with AGENT_LLM=cortex (agent/reasoning_model.py).
    Add another serving setup (llama.cpp's server, LM Studio) there."""
    return reasoning_model.complete_result(prompt, temperature=REASONING_TEMPERATURE,
                                           on_notice=on_notice)


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
    max_steps: int | None = None,
) -> CaseFile:
    """Runs the ReAct loop end to end and returns a validated CaseFile.

    `on_step` is an optional callback -- api/main.py wires this to the
    SSE stream (FR-20) so the UI can render each step as it happens.
    """
    investigation_id = str(uuid.uuid4())
    max_steps = MAX_STEPS if max_steps is None else max_steps
    transcript = [f"HINT: {hint}"]
    step_index = 0
    touched_node_ids: set[str] = set()
    touched_edge_ids: set[str] = set()

    def emit(step_type: InvestigationStepType, content: str, refs: list[str] | None = None) -> None:
        nonlocal step_index
        step = InvestigationStep(investigation_id=investigation_id, step_index=step_index,
                                  type=step_type, content=content, referenced_ids=refs or [])
        step_index += 1
        if on_step:
            on_step(step)

    # DATA_SOURCE=snowflake: the warehouse already ran the SQL and Cortex
    # detectors over the whole estate (graph/sql_detectors.py). Hand the
    # agent those leads as its first observation -- otherwise the only
    # detector that reads what an invoice SAYS was used to trim the graph
    # and then thrown away. Leads, not verdicts: an accusation still has
    # to cite edges the guardrail can resolve.
    warehouse_leads = g.graph.get("warehouse_leads") or []
    if warehouse_leads:
        obs_str = json.dumps(warehouse_leads[:MAX_WAREHOUSE_LEADS], default=str)
        emit(InvestigationStepType.OBSERVATION,
             f"[Snowflake: {len(warehouse_leads)} lead(s) from the warehouse detectors] {obs_str}",
             # One id per lead, not every invoice behind it: the UI's
             # camera visits each referenced node in turn.
             refs=list(dict.fromkeys(lead["entity_ids"][0] for lead in warehouse_leads)))
        transcript.append("OBSERVATION (warehouse pre-screen: SQL and Cortex detectors "
                          f"already run over the full estate in Snowflake): {obs_str}")

    loop_count = 0
    while loop_count < max_steps:
        loop_count += 1

        # Reserve the last step for the verdict. Measured against
        # qwen2.5:7b: the agent found the fraud (the payment, the pair of
        # companies, the edge id) and then spent its whole budget on more
        # tool calls, so the run fell through to the exhausted branch and
        # threw all of it away. Asking for the case file explicitly on
        # the final step turns that into an actual accusation.
        if loop_count >= max_steps:
            tail = ("\n\nThis is your LAST step -- you have no tool calls left. "
                    "Respond NOW with final_case_file, using only the evidence "
                    "already in the transcript above. If the evidence does not "
                    "support an accusation, say so in scheme_narrative and return "
                    "an empty implicated_suppliers list.")
        else:
            tail = "\n\nRespond with the next step now."

        prompt = f"{SYSTEM_PROMPT}\n\nTranscript so far:\n" + "\n".join(transcript) + tail

        try:
            result = _call_reasoning_model(
                prompt,
                on_notice=lambda text: emit(InvestigationStepType.OBSERVATION, f"[{text}]"),
            )
        except OllamaError as e:
            # The message is already written for a human ("no pude
            # conectar con http://...") -- surface it verbatim so the
            # demo says what to fix instead of just stopping.
            emit(InvestigationStepType.OBSERVATION, f"[{reasoning_model.label()}: {e}]")
            break

        if result.cancelled:
            emit(InvestigationStepType.CONCLUSION, "Investigation cancelled by the user.")
            return _empty_case_file(
                investigation_id,
                "Investigation cancelled before enough evidence was gathered.",
                g, touched_node_ids, touched_edge_ids)

        try:
            parsed = _parse_step(result.content)
        except ValueError as e:
            emit(InvestigationStepType.OBSERVATION, f"[parse error, retrying] {e}")
            continue

        emit(InvestigationStepType.THOUGHT, parsed.get("thought", ""))

        if "final_case_file" in parsed:
            try:
                draft = _build_case_file(investigation_id, g, parsed["final_case_file"],
                                          touched_node_ids, touched_edge_ids)
            except (ValidationError, TypeError, ValueError) as e:
                # The model got the shape wrong (wrong key, a peso amount
                # as "400,000"). Tell it what broke and let it retry
                # rather than dying with a stack trace mid-demo.
                emit(InvestigationStepType.OBSERVATION,
                     f"[final_case_file rejected: {e}. Re-send it with the exact "
                     f"field names from the system prompt.]")
                transcript.append(f"OBSERVATION: your final_case_file was malformed ({e})")
                continue
            cleaned, rejections = validate_case_file(g, draft)
            for reason in rejections:
                emit(InvestigationStepType.LEAD_DROPPED, reason)

            final = _synthesize_narrative(cleaned, emit)
            emit(InvestigationStepType.CONCLUSION, final.scheme_narrative)
            return final

        action = parsed.get("action")
        action_input = parsed.get("action_input", {}) or {}
        emit(InvestigationStepType.ACTION, f"{action}({action_input})",
             refs=[str(v) for v in action_input.values()])

        newly_touched: list[str] = []
        if action not in TOOLS:
            observation = {"error": f"Unknown tool '{action}'"}
        else:
            try:
                observation = TOOLS[action](g, **action_input)
            except TypeError as e:
                observation = {"error": f"Bad arguments for {action}: {e}"}
            else:
                before_nodes = set(touched_node_ids)
                before_edges = set(touched_edge_ids)
                _collect_touched_ids(action, action_input, observation,
                                      touched_node_ids, touched_edge_ids)
                newly_touched = sorted(
                    (touched_node_ids - before_nodes) | (touched_edge_ids - before_edges))

        obs_str = json.dumps(observation, default=str)
        # The ids THIS observation confirmed, so the UI can light up what
        # the agent just looked at. The action step can't carry them: for
        # run_detector its action_input is {"name": "blacklist_match"},
        # a detector name and not a node, so a run that opens with a
        # sweep gave the graph nothing to point at.
        emit(InvestigationStepType.OBSERVATION, obs_str, refs=newly_touched)
        transcript.append(f"ACTION: {action}({action_input})")
        transcript.append(f"OBSERVATION: {obs_str}")

    # Ran out of steps without a final case file -- return an honest
    # empty-handed result rather than forcing a guess (Judgment criterion).
    emit(InvestigationStepType.CONCLUSION,
         "Investigation budget exhausted without sufficient evidence for an accusation.")
    return _empty_case_file(
        investigation_id,
        "No fraud could be proven within the investigation budget.",
        g, touched_node_ids, touched_edge_ids)


def _empty_case_file(
    investigation_id: str,
    narrative: str,
    g: nx.MultiDiGraph | None = None,
    touched_node_ids: set[str] | None = None,
    touched_edge_ids: set[str] | None = None,
) -> CaseFile:
    """An honest empty-handed result -- used when the run is cancelled or
    the step budget runs out, rather than forcing a guess (Judgment).

    Empty-handed is not the same as amnesiac: the evidence trail still
    carries whatever subgraph the agent actually touched, so the UI can
    show the work even when no accusation survived.
    """
    if g is not None and (touched_node_ids or touched_edge_ids):
        subgraph = _filter_graph_to_subgraph(
            g, touched_node_ids or set(), touched_edge_ids or set())
    else:
        subgraph = nx.MultiDiGraph()

    return CaseFile(
        investigation_id=investigation_id, scheme_narrative=narrative,
        implicated_suppliers=[], evidence_trail=to_graph_export(subgraph),
        leads_not_pursued=[], total_amount_at_risk=0.0,
    )


def _build_narrative_prompt(cleaned: CaseFile) -> str:
    """Builds the cloud-model prompt for narrative synthesis. Includes
    the already-guardrail-cleaned implicated_suppliers/leads_not_pursued
    /total_amount_at_risk purely as read-only context the model must
    reflect, not alter -- see CASE_NARRATIVE_SYSTEM_PROMPT's rules."""
    suppliers_json = json.dumps([c.model_dump() for c in cleaned.implicated_suppliers], default=str)
    dropped_json = json.dumps([d.model_dump() for d in cleaned.leads_not_pursued], default=str)
    return (
        f"{CASE_NARRATIVE_SYSTEM_PROMPT}\n\n"
        f"Draft narrative from the investigation model:\n{cleaned.scheme_narrative}\n\n"
        f"Final implicated suppliers (do not change):\n{suppliers_json}\n\n"
        f"Leads considered but not pursued (do not change):\n{dropped_json}\n\n"
        f"Total peso amount at risk (do not change): {cleaned.total_amount_at_risk}\n\n"
        "Now write the polished plain-language narrative."
    )


def _synthesize_narrative(
    cleaned: CaseFile, emit: Callable[[InvestigationStepType, str], None]
) -> CaseFile:
    """Cloud call #1 of NFR-4's <=2-per-investigation budget (FR-15):
    polishes ONLY scheme_narrative for a non-technical reader.
    implicated_suppliers/evidence_edge_ids/leads_not_pursued are already
    final (local model + guardrail) and are never touched here. Falls
    back to the local model's own narrative if the cloud call fails,
    per NFR-5 (no live-network dependency should fail the demo)."""
    if not cloud_available():
        emit(InvestigationStepType.OBSERVATION,
             "[no CLOUD_LLM_API_KEY, keeping the local model's narrative]")
        return cleaned

    try:
        polished = call_cloud_model(_build_narrative_prompt(cleaned)).strip()
    except RuntimeError as e:  # CloudError, or any stub's own
        emit(InvestigationStepType.OBSERVATION,
             f"[cloud model unavailable, keeping local narrative: {e}]")
        return cleaned

    # The failure paths above always said so in the live log; the one
    # that worked was silent, so the only cloud call the judges get to
    # see happen left no trace on screen.
    emit(InvestigationStepType.OBSERVATION,
         "[Gemini rewrote the final narrative for a non-technical reader; "
         "accusations and evidence unchanged]")
    return cleaned.model_copy(update={"scheme_narrative": polished})


def _collect_touched_ids(
    action: str,
    action_input: dict,
    observation: object,
    touched_node_ids: set[str],
    touched_edge_ids: set[str],
) -> None:
    """Pulls the concrete node/edge ids a tool observation actually
    confirmed exist, per tool (FR-12), so the case file's evidence_trail
    can be built from only what the agent really saw rather than the
    whole graph. Only ids the observation itself vouches for are kept --
    e.g. a query_entity() that returned {"error": ...} contributes
    nothing, since that node was never confirmed to exist."""
    if action == "query_entity":
        entity_id = action_input.get("entity_id")
        if entity_id is not None and isinstance(observation, dict) and "error" not in observation:
            touched_node_ids.add(str(entity_id))

    elif action == "get_invoice":
        invoice_uuid = action_input.get("uuid")
        if invoice_uuid is not None and isinstance(observation, dict) and "error" not in observation:
            touched_node_ids.add(str(invoice_uuid))

    elif action == "check_blacklist":
        rfc = action_input.get("rfc")
        if rfc is not None and isinstance(observation, dict) and observation.get("exists"):
            touched_node_ids.add(str(rfc))

    elif action == "get_neighbors":
        node_id = action_input.get("node_id")
        if node_id is not None:
            touched_node_ids.add(str(node_id))
        if isinstance(observation, list):
            for entry in observation:
                if not isinstance(entry, dict):
                    continue
                if entry.get("neighbor") is not None:
                    touched_node_ids.add(str(entry["neighbor"]))
                if entry.get("edge_id") is not None:
                    touched_edge_ids.add(str(entry["edge_id"]))

    elif action == "trace_payment_path":
        if isinstance(observation, dict) and observation.get("path"):
            for node_id in observation["path"]:
                touched_node_ids.add(str(node_id))
            for edge in observation.get("edges") or []:
                if isinstance(edge, dict) and edge.get("id") is not None:
                    touched_edge_ids.add(str(edge["id"]))

    elif action == "run_detector":
        if isinstance(observation, list):
            for lead in observation:
                if not isinstance(lead, dict):
                    continue
                for entity_id in lead.get("entity_ids") or []:
                    touched_node_ids.add(str(entity_id))
                for edge_id in lead.get("supporting_edge_ids") or []:
                    touched_edge_ids.add(str(edge_id))


def _filter_graph_to_subgraph(
    g: nx.MultiDiGraph, touched_node_ids: set[str], touched_edge_ids: set[str]
) -> nx.MultiDiGraph:
    """Builds the minimal subgraph the agent actually gathered evidence
    from: every touched edge plus its two endpoint nodes, plus any
    touched node visited directly (e.g. via query_entity) even when no
    edge off it was cited. This -- not the full graph -- is what
    to_graph_export() should run over for a case file's evidence_trail,
    so the guardrail's edge-id checks (FR-16, FR-17) mean something."""
    sub = nx.MultiDiGraph()
    for u, v, key, data in g.edges(keys=True, data=True):
        if str(key) not in touched_edge_ids:
            continue
        if not sub.has_node(u):
            sub.add_node(u, **g.nodes[u])
        if not sub.has_node(v):
            sub.add_node(v, **g.nodes[v])
        sub.add_edge(u, v, key=key, **data)

    for node_id in touched_node_ids:
        if g.has_node(node_id) and not sub.has_node(node_id):
            sub.add_node(node_id, **g.nodes[node_id])

    return sub


def _build_case_file(
    investigation_id: str,
    g: nx.MultiDiGraph,
    raw: dict,
    touched_node_ids: set[str],
    touched_edge_ids: set[str],
) -> CaseFile:
    claims = [AccusationClaim(**c) for c in raw.get("implicated_suppliers", [])]
    dropped = [DroppedLead(**d) for d in raw.get("leads_not_pursued", [])]
    total = sum(c.peso_amount for c in claims)

    subgraph = _filter_graph_to_subgraph(g, touched_node_ids, touched_edge_ids)
    evidence = to_graph_export(subgraph)

    return CaseFile(
        investigation_id=investigation_id, scheme_narrative=raw.get("scheme_narrative", ""),
        implicated_suppliers=claims, evidence_trail=evidence,
        leads_not_pursued=dropped, total_amount_at_risk=total,
    )
