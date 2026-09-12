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
from pydantic import BaseModel

from agent import ollama_client
from agent.loop import run_investigation
from api.state import state
from data.generator.estate_generator import generate, inject_pattern
from graph.builder import build_graph, to_graph_export
from shared.config import (
    OllamaSettings,
    config_path,
    env_overrides,
    load_settings,
    save_settings,
)
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
    # While you're there: pass that same cloud client to
    # ollama_client.register_cloud_fallback() at startup, so a LAN server
    # that dies mid-demo degrades to the cloud instead of ending the run.
    # Stubbed with the real response shape so Dev 3/Dev 4 can integrate
    # against it immediately.
    return AskResponse(answer=f"[stub] Answering '{req.question}' is not wired up yet.")


@app.post("/investigate/cancel")
def investigate_cancel() -> dict:
    """Stops the generation the agent is streaming right now (FR-20).

    The loop checks the flag once per streamed line, so this lands within
    a token or two; it does not kill the thread, it lets the loop return
    an honest cancelled case file.

    The flag is process-wide, like the rest of the in-memory state here
    (api/state.py): one investigation at a time, which is the demo's
    model. Give each run its own CancelToken if that ever stops holding.
    """
    ollama_client.request_cancel()
    return {"cancelled": True}


# ---------------------------------------------------------------------------
# Local-model connection (Ollama on another machine in the LAN)
# ---------------------------------------------------------------------------

class OllamaStatus(BaseModel):
    """What the settings panel needs to render in one round trip."""

    settings: OllamaSettings
    env_overrides: dict[str, str] = {}
    config_file: str


class ProbeResponse(BaseModel):
    ok: bool
    models: list[str] = []
    message: str = ""


class OllamaHealth(ProbeResponse):
    url: str
    model: str
    model_available: bool | None = None
    #: Whether a cloud model is registered to cover a dead LAN server.
    cloud_fallback: bool = False


def _status() -> OllamaStatus:
    return OllamaStatus(settings=load_settings(refresh=True),
                        env_overrides=env_overrides(),
                        config_file=str(config_path()))


@app.get("/config/ollama", response_model=OllamaStatus)
def get_ollama_config() -> OllamaStatus:
    return _status()


@app.put("/config/ollama", response_model=OllamaStatus)
def put_ollama_config(settings: OllamaSettings) -> OllamaStatus:
    """Persists the server/model choice to the user config file.

    An OLLAMA_* environment variable still outranks the file, so the
    response echoes `env_overrides` -- the UI shows those as pinned
    instead of pretending the save had no effect.
    """
    save_settings(settings)
    return _status()


@app.get("/config/ollama/models", response_model=ProbeResponse)
def probe_ollama(url: str | None = None) -> ProbeResponse:
    """Backs the UI's "Buscar modelos" button. Never fails: a server
    that's off is an answer, not a 500."""
    ok, models, message = ollama_client.list_models(url)
    return ProbeResponse(ok=ok, models=models, message=message)


@app.get("/health/ollama", response_model=OllamaHealth)
def health_ollama() -> OllamaHealth:
    """Is the configured server reachable, and does it have our model?"""
    settings = load_settings(refresh=True)
    ok, models, message = ollama_client.list_models(settings.url)
    available: bool | None = None
    if ok:
        # `ollama list` shows "qwen2.5:7b"; a bare "qwen2.5" means :latest.
        wanted = settings.model if ":" in settings.model else f"{settings.model}:latest"
        available = wanted in models or settings.model in models
        if not available and models:
            message = (f"{settings.url} responde, pero no tiene '{settings.model}'. "
                       f"Disponibles: {', '.join(models)}.")
    return OllamaHealth(ok=ok, models=models, message=message,
                        url=settings.url, model=settings.model,
                        model_available=available,
                        cloud_fallback=ollama_client.has_cloud_fallback())


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}
