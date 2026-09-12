"""
Grounded Q&A follow-up (FR-19). `answer_question` is the single public
entry point here and the ONLY function Diego should import from
api/main.py to replace the /case-file/{investigation_id}/ask stub --
this signature is frozen; everything else in agent/ is Angel's and may
change shape.

Spends the second (and last) of NFR-4's <=2 cloud-model calls per
investigation; the first is agent/loop.py's narrative synthesis. There
is no tool-calling loop here -- the judge's question is answered purely
from `case_file.evidence_trail`, which is already the touched-only
subgraph agent/loop.py::_filter_graph_to_subgraph built during the
investigation, not the full graph. That's what makes "say so explicitly
if it's not in the evidence trail" (QA_SYSTEM_PROMPT) actually mean
something: the model has no path back into data it didn't already cite.
"""
from __future__ import annotations

import json

import networkx as nx

from agent.cloud import call_cloud_model
from agent.ollama_client import OllamaError, complete_result
from agent.prompts import QA_SYSTEM_PROMPT
from shared.schemas import AskResponse, CaseFile


def answer_question(g: nx.MultiDiGraph, case_file: CaseFile, question: str) -> AskResponse:
    """Answers one free-text follow-up question, grounded only in
    `case_file.evidence_trail` -- never unconstrained model memory
    (FR-19). `g`, the full investigation graph, is accepted to match the
    shape of other agent/ entry points and to leave room for a future
    re-query capability, but is intentionally not consulted: grounding
    the answer in the case file's own (already-trimmed) evidence trail,
    rather than the full graph, is what lets the model honestly say
    "that's not in the evidence I gathered" instead of quietly reaching
    for facts the investigation never actually verified."""
    del g  # not used -- see docstring

    prompt = _build_qa_prompt(case_file, question)
    try:
        # No cloud_available() pre-check on purpose: call_cloud_model
        # already raises CloudError when the key is missing, and gating
        # here would skip the call entirely -- including a stubbed one.
        answer = call_cloud_model(prompt).strip()
    except RuntimeError as cloud_error:  # CloudError, or any stub's own
        # No CLOUD_LLM_API_KEY is the normal case right now, and the
        # judge's question is the most watched moment of the demo -- the
        # LAN model already answers every other step, so use it here
        # rather than showing an apology. Quality is lower than the
        # cloud model's; a real answer beats none.
        try:
            answer = complete_result(prompt, temperature=0.3).content.strip()
        except OllamaError as local_error:
            return AskResponse(
                answer=f"Neither model is reachable right now, so this question can't be "
                       f"answered. Cloud: {cloud_error}. Local: {local_error}.",
            )

    if not answer:
        answer = "The model returned an empty response; try rephrasing the question."

    # Ground the answer visibly: the UI renders these as "FUENTES
    # CONSULTADAS", and they are what the judge can check us against.
    referenced = [c.supplier_rfc for c in case_file.implicated_suppliers]
    referenced += [eid for c in case_file.implicated_suppliers for eid in c.evidence_edge_ids]
    return AskResponse(answer=answer, referenced_ids=referenced)


def _build_qa_prompt(case_file: CaseFile, question: str) -> str:
    context = {
        "scheme_narrative": case_file.scheme_narrative,
        "implicated_suppliers": [c.model_dump() for c in case_file.implicated_suppliers],
        "leads_not_pursued": [d.model_dump() for d in case_file.leads_not_pursued],
        "total_amount_at_risk": case_file.total_amount_at_risk,
        "evidence_trail": case_file.evidence_trail.model_dump(),
    }
    return (
        f"{QA_SYSTEM_PROMPT}\n\n"
        f"Case file and evidence trail (JSON):\n{json.dumps(context, default=str)}\n\n"
        f"Judge's question: {question}\n\n"
        "Answer now."
    )
