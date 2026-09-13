"""
FastAPI backend (SRS section 7). Endpoints match the frozen contract
exactly so Dev 4's frontend can be built against this file's shape from
hour 0, whether or not Dev 1/Dev 2's real logic underneath is finished.

Run: uvicorn api.main:app --reload --port 8000
"""
from __future__ import annotations

import json
import os
import queue
import threading
import time
import warnings
from collections import Counter
from contextlib import asynccontextmanager

from pathlib import Path

import networkx as nx
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from agent import ollama_client, reasoning_model
from agent import loop as agent_loop
from agent.cloud import DEFAULT_CLOUD_LLM_MODEL, call_cloud_model, cloud_available
from agent.loop import run_investigation
from agent.qa import answer_question
from api.state import (
    case_files_dir,
    load_case_files_from_disk,
    load_recording,
    load_steps,
    save_case_file,
    save_steps,
    state,
    steps_path,
)
from data.generator.estate_generator import generate, inject_pattern
from data.snowflake_client import snowflake_available
from graph.builder import build_graph, to_graph_export
from graph.sql_detectors import build_reduced_graph_from_snowflake
from shared.schemas import DataEstate
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


app = FastAPI(title="CORPIDE API", lifespan=lifespan)

app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

#: A question longer than this cannot fit alongside the evidence
#: trail in num_ctx, so the grounding would be dropped silently.
MAX_QUESTION_CHARS = 2000


def _build_graph_for_state(estate: DataEstate) -> nx.MultiDiGraph:
    """Where the DATA_SOURCE flag (docs/snowflake-integracion.md) lives.

    DATA_SOURCE unset or "local" (the default): unchanged behavior --
    the full local graph, exactly as before this integration existed.

    DATA_SOURCE=snowflake: loads `estate` into the warehouse, runs the
    4 SQL/Cortex detectors there, and returns a graph containing only
    the suspicious entities and their direct neighbors
    (graph/sql_detectors.py::build_reduced_graph_from_snowflake). ANY
    failure on that path (network, bad credentials, Cortex unavailable,
    a warehouse that never wakes up) is caught here and falls back to
    the full local graph with a warning -- this integration must never
    be why a demo run fails, per the doc's non-negotiable rule.
    """
    if os.environ.get("DATA_SOURCE", "local") != "snowflake":
        state.data_source = {"requested": "local", "active": "local"}
        return build_graph(estate)
    started = time.perf_counter()
    try:
        g = build_reduced_graph_from_snowflake(estate)
    except Exception as exc:  # noqa: BLE001 -- must never break estate/generate
        warnings.warn(
            f"DATA_SOURCE=snowflake failed ({type(exc).__name__}: {exc}) -- "
            "falling back to the local graph", RuntimeWarning, stacklevel=2)
        # A fallback nobody can see is how a demo "uses Snowflake" on
        # stage while running entirely on the laptop: the dashboard says so.
        state.data_source = {"requested": "snowflake", "active": "local",
                             "error": f"{type(exc).__name__}: {exc}"[:300]}
        return build_graph(estate)
    state.data_source = {
        "requested": "snowflake", "active": "snowflake",
        "seconds": round(time.perf_counter() - started, 1),
        "nodes_kept": g.number_of_nodes(),
        "nodes_total": g.graph.get("full_node_count"),
        "leads": dict(Counter(lead["detector"] for lead in g.graph.get("warehouse_leads", []))),
    }
    return g


def _refuse_while_investigating() -> None:
    """One investigation at a time, and no mutating the graph under it.

    Verified: with a run in flight, /estate/generate took the graph from
    241 nodes to 684 while the worker kept reasoning over the old object,
    so the case file cited edges the UI no longer showed.
    """
    if state.investigation_running:
        raise HTTPException(
            409, "An investigation is running; cancel it first (POST /investigate/cancel)")


@app.get("/investigate/status")
def investigate_status() -> dict:
    """Is the slot free? The only way to ask used to be POST
    /investigate, which starts a real run if it happens to be free.

    Also reports max_steps so the UI can draw a progress bar against the
    real budget instead of guessing at it.
    """
    return {"running": state.investigation_running,
            "run_id": state.current_run_id,
            "case_files": len(state.case_files),
            "max_steps": agent_loop.MAX_STEPS}


@app.post("/estate/generate")
def estate_generate(req: GenerateEstateRequest) -> dict:
    _refuse_while_investigating()
    try:
        state.estate = generate(seed=req.seed, num_suppliers=req.num_suppliers,
                                 num_blacklisted=req.num_blacklisted)
    except ValueError as exc:
        # num_blacklisted=-1 reached random.sample and raised. The proper
        # fix is Field(ge=0) in shared/schemas.py, which is the frozen
        # contract and not mine to change -- ask the team for it.
        raise HTTPException(400, str(exc)) from exc
    state.graph = _build_graph_for_state(state.estate)
    return {"seed": req.seed, "num_companies": len(state.estate.companies),
            "num_invoices": len(state.estate.invoices), "num_payments": len(state.estate.payments)}


@app.post("/estate/inject-scenario")
def estate_inject_scenario(req: InjectScenarioRequest) -> dict:
    """Scenario Injector (FR-4). Judge-facing, so a bad pattern name is
    a 400 that lists the valid ones -- not a 500 that makes the demo
    look broken in front of the person who typed it."""
    _refuse_while_investigating()
    if state.estate is None:
        raise HTTPException(400, "Call /estate/generate first")
    try:
        state.estate = inject_pattern(state.estate, req.pattern, **req.params)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except TypeError as exc:
        raise HTTPException(400, f"Bad params for '{req.pattern}': {exc}") from exc
    state.graph = _build_graph_for_state(state.estate)
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
    _refuse_while_investigating()

    step_queue: queue.Queue = queue.Queue()
    sentinel = object()

    recorded: list[dict] = []

    def on_step(step: InvestigationStep) -> None:
        payload = step.model_dump(mode="json")
        recorded.append(payload)
        step_queue.put(json.dumps({"type": "step", "data": payload}))

    state.current_run_id += 1
    my_run_id = state.current_run_id

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
            if save_case_file(case_file):
                # Only worth keeping alongside a case file worth keeping.
                # The graph goes with it: ids are not reproducible from
                # the seed, so a replay needs the exact graph it ran over.
                save_steps(case_file.investigation_id, recorded,
                           to_graph_export(state.graph).model_dump(mode="json"))
            step_queue.put(json.dumps(
                {"type": "done", "investigation_id": case_file.investigation_id}))
        except Exception as exc:  # noqa: BLE001 -- the stream must always close
            step_queue.put(json.dumps({
                "type": "error",
                "message": f"{type(exc).__name__}: {exc}",
            }))
        finally:
            step_queue.put(sentinel)
            # Only release the slot if it is still ours: a cancelled run
            # keeps dying in the background, and freeing the slot here
            # would kick out whatever the operator started meanwhile.
            if state.current_run_id == my_run_id:
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


class CaseFileSummary(BaseModel):
    investigation_id: str
    num_implicated_suppliers: int
    total_amount_at_risk: float
    narrative_preview: str
    #: Whether the step stream was recorded, i.e. whether this one can
    #: be replayed. Older snapshots predate the recording.
    has_steps: bool = False


@app.get("/case-files", response_model=list[CaseFileSummary])
def list_case_files() -> list[CaseFileSummary]:
    """Every case file in memory, including the ones reloaded from disk.

    Without this the offline fallback (SRS section 9) is "remember the
    uuid": if a judge asks to see the previous investigation there is no
    way to find its id. Ordered biggest-exposure first, which is also
    the one worth showing.
    """
    summaries = [
        CaseFileSummary(
            investigation_id=cf.investigation_id,
            num_implicated_suppliers=len(cf.implicated_suppliers),
            total_amount_at_risk=cf.total_amount_at_risk,
            narrative_preview=cf.scheme_narrative[:160],
            has_steps=load_recording(cf.investigation_id)["graph"] is not None,
        )
        for cf in state.case_files.values()
    ]
    return sorted(summaries, key=lambda s: s.total_amount_at_risk, reverse=True)


@app.get("/case-file/{investigation_id}", response_model=CaseFile)
def get_case_file(investigation_id: str) -> CaseFile:
    cf = state.case_files.get(investigation_id)
    if cf is None:
        raise HTTPException(404, "Unknown investigation_id")
    return cf


@app.get("/case-file/{investigation_id}/steps")
def get_case_file_steps(investigation_id: str) -> dict:
    """The step stream that produced this case file, for replay.

    Lets the UI rehearse a real investigation -- same events, same
    animations -- without spending another minute of model time, and is
    the offline fallback SRS section 9 asks for.
    """
    if investigation_id not in state.case_files:
        raise HTTPException(404, "Unknown investigation_id")
    recording = load_recording(investigation_id)
    if not recording["steps"]:
        raise HTTPException(404, "No step stream was saved for this investigation")
    return {"investigation_id": investigation_id,
            "steps": recording["steps"],
            "graph": recording["graph"]}


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

    # shared/schemas.py declares question as a bare str and is frozen, so
    # bound it here. An empty question burns ~26s of the shared CPU on an
    # accidental Enter; a 100k one overflows num_ctx=8192 and silently
    # pushes the evidence trail -- the whole grounding -- out of the
    # prompt, answering with HTTP 200 and no evidence behind it.
    question = req.question.strip()
    if not question:
        raise HTTPException(422, "question is empty")
    if len(question) > MAX_QUESTION_CHARS:
        raise HTTPException(
            422, f"question is too long ({len(question)} chars, max {MAX_QUESTION_CHARS}): "
                 "it would push the evidence trail out of the model's context")

    return answer_question(state.graph, cf, question)


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
    # Free the slot immediately. The flag is only checked between
    # streamed lines (agent/ollama_client.py), so a run waiting on the
    # first byte of a cold 7B keeps blocking for up to OLLAMA_TIMEOUT --
    # measured at 300s with the team's .env. Leaving the 409 up for five
    # minutes after someone pressed Detener is a dead demo; the old
    # worker still exits on its own, and current_run_id stops it from
    # releasing the next run's slot.
    was_running = state.investigation_running
    state.investigation_running = False
    return {"cancelled": True, "was_running": was_running}


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
    #: Same value as `ok`, under the name the team's task doc promised
    #: Victor's UI. Keep both: the dashboard reads `ok`, and anything
    #: written against the doc reads `ollama_reachable`.
    ollama_reachable: bool
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
    """Backs the Settings panel's model search button. Never fails: a server
    that's off is an answer, not a 500."""
    try:
        ok, models, message = ollama_client.list_models(url)
    except Exception as exc:  # noqa: BLE001 -- this endpoint must not 500
        # list_models swallows requests' exceptions, but not everything:
        # urlsplit raises ValueError on "http://[", and urllib3 raises
        # LocationParseError (not a RequestException) on a host label
        # over 63 chars. Both reached here as a 500.
        return ProbeResponse(ok=False, models=[],
                             message=f"Invalid URL: {type(exc).__name__}: {exc}")
    return ProbeResponse(ok=ok, models=models, message=message)


@app.get("/health/ollama", response_model=OllamaHealth)
def health_ollama() -> OllamaHealth:
    """Is the configured server reachable, and does it have our model?

    Lets the UI show a status indicator BEFORE starting an investigation
    in front of the judges, instead of finding out mid-demo that the
    model isn't running. Never fails: an unreachable server is an answer
    (ollama_reachable=false), not a 500.
    """
    settings = load_settings(refresh=True)
    try:
        ok, models, message = ollama_client.list_models(settings.url)
    except Exception as exc:  # noqa: BLE001 -- the pre-flight light must never 500
        return OllamaHealth(
            ok=False, ollama_reachable=False, models=[],
            message=f"Invalid OLLAMA_URL: {type(exc).__name__}: {exc}",
            url=settings.url, model=settings.model, model_available=None,
            cloud_fallback=ollama_client.has_cloud_fallback())
    available: bool | None = None
    if ok:
        # `ollama list` shows "qwen2.5:7b"; a bare "qwen2.5" means :latest.
        wanted = settings.model if ":" in settings.model else f"{settings.model}:latest"
        available = wanted in models or settings.model in models
        if not available and models:
            message = (f"{settings.url} answers, but does not have '{settings.model}'. "
                       f"Available: {', '.join(models)}.")
    return OllamaHealth(ok=ok, ollama_reachable=ok, models=models, message=message,
                        url=settings.url, model=settings.model,
                        model_available=available,
                        cloud_fallback=ollama_client.has_cloud_fallback())


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.get("/health/integrations")
def health_integrations() -> dict:
    """The sidebar's lights besides Ollama: which model reasons, can
    Gemini be called, and where did the graph on screen come from.
    Reports configuration and the
    last graph build only -- no network call -- so polling it is free
    and it answers instantly even when the warehouse is asleep.
    """
    return {
        #: Who reasons each step. With "cortex" the LAN Ollama is not in
        #: the path, and the dashboard stops probing it.
        "agent": {"provider": reasoning_model.provider(), "model": reasoning_model.model_name()},
        "gemini": {"configured": cloud_available(),
                   "model": os.environ.get("CLOUD_LLM_MODEL") or DEFAULT_CLOUD_LLM_MODEL},
        "snowflake": {"configured": snowflake_available(),
                      "requested": os.environ.get("DATA_SOURCE", "local") == "snowflake"},
        #: None until the first /estate/generate of this process.
        "last_build": state.data_source,
    }


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


# Only register it if it can actually fire. Registering unconditionally
# made /health/ollama report cloud_fallback:true with no
# CLOUD_LLM_API_KEY set -- the pre-flight light everyone checks BEFORE
# demoing, saying there is a safety net that would raise RuntimeError.
if cloud_available():
    ollama_client.register_cloud_fallback(_cloud_fallback)


_WEB_DIR = Path(__file__).resolve().parents[1] / "ui" / "web"


@app.middleware("http")
async def no_cache_dashboard(request, call_next):
    """Never let a browser cache the dashboard.

    The frontend changes many times an hour during the hackathon, and a
    cached app.js against a fresh index.html is a silent, confusing
    half-broken page: new markup driven by old code. Costs nothing --
    everything is served from localhost. vendor/ is exempt because
    vis-network is 468KB and never changes.
    """
    response = await call_next(request)
    path = request.url.path
    if path.startswith("/app") and "/vendor/" not in path:
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
        response.headers["Pragma"] = "no-cache"
    return response


@app.get("/", include_in_schema=False)
def root() -> RedirectResponse:
    return RedirectResponse(url="/app/")


if _WEB_DIR.is_dir():
    app.mount("/app", StaticFiles(directory=_WEB_DIR, html=True), name="dashboard")
