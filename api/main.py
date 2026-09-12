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
from contextlib import asynccontextmanager

from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from agent import ollama_client
from agent.cloud import call_cloud_model
from agent.loop import run_investigation
from agent.qa import answer_question
from api.state import (
    case_files_dir,
    load_case_files_from_disk,
    save_case_file,
    state,
)
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

@asynccontextmanager
async def lifespan(_app: FastAPI):
    """Reload the case files from previous runs before serving traffic,
    so /case-file/{id} and /ask work for investigations that happened
    before this process started."""
    count = load_case_files_from_disk()
    if count:
        print(f"[api] reloaded {count} case file(s) from {case_files_dir()}")
    yield


app = FastAPI(title="The Forensic Auditor API", lifespan=lifespan)

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
    # One investigation at a time: the cancel flag and the in-memory
    # state are process-wide (api/state.py), so two concurrent runs would
    # cancel each other and race on the same graph.
    if state.investigation_running:
        raise HTTPException(409, "An investigation is already running; cancel it first")

    step_queue: queue.Queue = queue.Queue()
    sentinel = object()

    def on_step(step: InvestigationStep) -> None:
        step_queue.put(json.dumps({"type": "step", "data": step.model_dump(mode="json")}))

    def worker() -> None:
        # Everything in here must be inside try/finally. Without it, any
        # exception in run_investigation (a 7B returning {"rfc": ...}
        # instead of {"supplier_rfc": ...} used to raise ValidationError)
        # killed this thread before the sentinel was queued, and
        # event_stream() below blocked on queue.get() forever: the SSE
        # stayed open, the UI spun, and nothing ever said why. That is
        # the worst possible failure mode in front of a judge.
        try:
            case_file = run_investigation(state.graph, req.hint, on_step=on_step)
            state.case_files[case_file.investigation_id] = case_file
            # Snapshot to disk so a restart -- or a dead LAN model in
            # front of the judges -- doesn't cost us a run we already
            # paid minutes of model time for (SRS section 9).
            save_case_file(case_file)
            step_queue.put(json.dumps(
                {"type": "done", "investigation_id": case_file.investigation_id}))
        except Exception as exc:  # noqa: BLE001 -- the stream must always close
            step_queue.put(json.dumps({
                "type": "error",
                "message": f"{type(exc).__name__}: {exc}",
            }))
        finally:
            step_queue.put(sentinel)
            state.investigation_running = False

    state.investigation_running = True
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
    """The judge's live follow-up question (FR-19). All grounding logic
    belongs to agent/qa.py -- this endpoint only resolves the
    investigation_id and hands off. answer_question() accepts the full
    graph to match the shape of other agent/ entry points but grounds
    its answer in case_file.evidence_trail alone. It never raises
    (NFR-5): with no CLOUD_LLM_API_KEY it answers with the LAN model
    instead, and only if BOTH models are unreachable does it return a
    plain-language explanation -- so there is nothing to catch here."""
    cf = state.case_files.get(investigation_id)
    if cf is None:
        raise HTTPException(404, "Unknown investigation_id")
    return answer_question(state.graph, cf, req.question)


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


# ---------------------------------------------------------------------------
# Dashboard (ui/web) -- served from this same process
# ---------------------------------------------------------------------------
# One thing to start for the demo, and the page calls the API on its own
# origin, so no CORS hop and no second port to remember. Mounted last so
# it can never shadow an API route.

# README promised a one-shot cloud fallback when the LAN server dies
# mid-demo, but nobody ever registered one -- /health/ollama reported
# cloud_fallback:false. It only actually fires if CLOUD_LLM_API_KEY is
# set; without a key call_cloud_model raises and the client re-raises the
# original connection error, which is the honest outcome.
def _cloud_fallback(messages: list[dict]) -> str:
    prompt = "\n\n".join(m.get("content", "") for m in messages if m.get("content"))
    return call_cloud_model(prompt)


ollama_client.register_cloud_fallback(_cloud_fallback)


_WEB_DIR = Path(__file__).resolve().parents[1] / "ui" / "web"


@app.get("/", include_in_schema=False)
def root() -> RedirectResponse:
    return RedirectResponse(url="/app/")


if _WEB_DIR.is_dir():
    app.mount("/app", StaticFiles(directory=_WEB_DIR, html=True), name="dashboard")
