"""
FastAPI backend (SRS section 7). Endpoints match the frozen contract
exactly so Dev 4's frontend can be built against this file's shape from
hour 0, whether or not Dev 1/Dev 2's real logic underneath is finished.

Run: uvicorn api.main:app --reload --port 8000
"""
from __future__ import annotations

import json
import queue
import threading

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse

from agent.loop import run_investigation
from api.state import state
from data.generator.estate_generator import generate, inject_pattern
from graph.builder import build_graph, to_graph_export
from shared.schemas import (
    AskRequest,
    AskResponse,
    CaseFile,
    GenerateEstateRequest,
    GraphExport,
    InjectScenarioRequest,
    InvestigateRequest,
    InvestigationStep,
)

app = FastAPI(title="The Forensic Auditor API")

app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


@app.post("/estate/generate")
def estate_generate(req: GenerateEstateRequest) -> dict:
    state.estate = generate(seed=req.seed, num_suppliers=req.num_suppliers,
                             num_blacklisted=req.num_blacklisted)
    state.graph = build_graph(state.estate)
    return {"seed": req.seed, "num_companies": len(state.estate.companies),
            "num_invoices": len(state.estate.invoices), "num_payments": len(state.estate.payments)}


@app.post("/estate/inject-scenario")
def estate_inject_scenario(req: InjectScenarioRequest) -> dict:
    if state.estate is None:
        raise HTTPException(400, "Call /estate/generate first")
    state.estate = inject_pattern(state.estate, req.pattern, **req.params)
    state.graph = build_graph(state.estate)
    return {"pattern": req.pattern, "num_companies": len(state.estate.companies)}


@app.get("/graph/export", response_model=GraphExport)
def graph_export() -> GraphExport:
    if state.graph is None:
        raise HTTPException(400, "Call /estate/generate first")
    return to_graph_export(state.graph)


@app.post("/investigate")
def investigate(req: InvestigateRequest) -> StreamingResponse:
    """Streams each agent step over SSE as it happens (FR-20), then a
    final `done` event carrying the investigation_id to fetch the case
    file from. Uses a background thread + queue because run_investigation
    is a synchronous, blocking loop (it makes blocking HTTP calls to
    Ollama) -- Starlette runs a sync generator like event_stream() in a
    thread pool, so a blocking queue.get() here is safe."""
    if state.graph is None:
        raise HTTPException(400, "Call /estate/generate first")

    step_queue: queue.Queue = queue.Queue()
    sentinel = object()

    def on_step(step: InvestigationStep) -> None:
        step_queue.put(json.dumps({"type": "step", "data": step.model_dump(mode="json")}))

    def worker() -> None:
        case_file = run_investigation(state.graph, req.hint, on_step=on_step)
        state.case_files[case_file.investigation_id] = case_file
        step_queue.put(json.dumps({"type": "done", "investigation_id": case_file.investigation_id}))
        step_queue.put(sentinel)

    threading.Thread(target=worker, daemon=True).start()

    def event_stream():
        while True:
            item = step_queue.get()
            if item is sentinel:
                break
            yield f"data: {item}\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream")


@app.get("/case-file/{investigation_id}", response_model=CaseFile)
def get_case_file(investigation_id: str) -> CaseFile:
    cf = state.case_files.get(investigation_id)
    if cf is None:
        raise HTTPException(404, "Unknown investigation_id")
    return cf


@app.post("/case-file/{investigation_id}/ask", response_model=AskResponse)
def ask(investigation_id: str, req: AskRequest) -> AskResponse:
    cf = state.case_files.get(investigation_id)
    if cf is None:
        raise HTTPException(404, "Unknown investigation_id")
    # TODO (Dev 2, FR-19): call the cloud model with QA_SYSTEM_PROMPT +
    # cf.evidence_trail as grounding context, using the same tool
    # registry so the agent can re-query the graph live if needed.
    # Stubbed with the real response shape so Dev 3/Dev 4 can integrate
    # against it immediately.
    return AskResponse(answer=f"[stub] Answering '{req.question}' is not wired up yet.")


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}
