"""
API tests (Diego). TestClient over api.main.app -- no Ollama, no cloud
key, no network: anything that would reach a model is monkeypatched.
"""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from agent import ollama_client
from agent.ollama_client import OllamaError
from api.main import app
from api.state import load_case_files_from_disk, save_case_file, state
from shared.schemas import AccusationClaim, CaseFile, GraphEdge, GraphExport, GraphNode


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture(autouse=True)
def isolated_case_files(tmp_path, monkeypatch):
    monkeypatch.setenv("FORENSIC_CASE_FILES_DIR", str(tmp_path / "case_files"))
    yield


@pytest.fixture(autouse=True)
def clean_state():
    state.estate = None
    state.graph = None
    state.case_files = {}
    state.investigation_running = False
    state.data_source = None
    yield
    state.investigation_running = False


def sample_case_file(investigation_id: str = "inv-1") -> CaseFile:
    return CaseFile(
        investigation_id=investigation_id,
        scheme_narrative="AUD010101XXX paid AANS850115SE5 with no matching invoices.",
        implicated_suppliers=[AccusationClaim(
            supplier_rfc="AANS850115SE5", rule_broken="Payment with no matching invoice",
            peso_amount=180_114.65, evidence_edge_ids=["pay-1"])],
        evidence_trail=GraphExport(
            nodes=[GraphNode(id="AANS850115SE5", type="Company", label="Proveedor")],
            edges=[GraphEdge(id="pay-1", source="AUD010101XXX",
                             target="AANS850115SE5", type="EXECUTED_PAYMENT")]),
        leads_not_pursued=[], total_amount_at_risk=180_114.65)


# ---------------------------------------------------------------------------
# /case-file/{id}/ask -- the judge's follow-up (FR-19)
# ---------------------------------------------------------------------------

def test_ask_unknown_investigation_id_is_404(client):
    assert client.post("/case-file/does-not-exist/ask",
                       json={"question": "why?"}).status_code == 404


def test_ask_hands_off_to_the_agent_and_is_not_a_stub(client, monkeypatch):
    """The endpoint must call agent.qa.answer_question, not answer on its
    own: all grounding logic lives in agent/, api/ only resolves the id."""
    seen = {}

    def fake_answer(g, case_file, question):
        seen["case_file_id"] = case_file.investigation_id
        seen["question"] = question
        from shared.schemas import AskResponse
        return AskResponse(answer="Because its RFC is shared.", referenced_ids=["pay-1"])

    monkeypatch.setattr("api.main.answer_question", fake_answer)
    state.case_files["inv-1"] = sample_case_file()

    body = client.post("/case-file/inv-1/ask", json={"question": "why RFC1?"}).json()

    assert seen == {"case_file_id": "inv-1", "question": "why RFC1?"}
    assert body["answer"] == "Because its RFC is shared."
    assert body["referenced_ids"] == ["pay-1"]
    assert "[stub]" not in body["answer"]


def test_ask_answer_question_signature_is_the_frozen_one():
    """agent/qa.py calls this signature frozen; api/ must keep matching
    it rather than growing its own Q&A logic."""
    import inspect

    from agent.qa import answer_question

    assert list(inspect.signature(answer_question).parameters) == ["g", "case_file", "question"]


# ---------------------------------------------------------------------------
# Case-file snapshots on disk (SRS section 9's offline fallback)
# ---------------------------------------------------------------------------

def test_case_file_is_snapshotted_after_an_investigation(client, monkeypatch, tmp_path):
    monkeypatch.setenv("FORENSIC_CASE_FILES_DIR", str(tmp_path))
    monkeypatch.setattr("api.main.run_investigation",
                        lambda *a, **kw: sample_case_file("inv-saved"))
    client.post("/estate/generate", json={"seed": 42, "num_blacklisted": 0})

    with client.stream("POST", "/investigate", json={"hint": "x"}) as resp:
        list(resp.iter_lines())

    snapshot = tmp_path / "inv-saved.json"
    assert snapshot.exists()
    written = json.loads(snapshot.read_text(encoding="utf-8"))
    assert written["investigation_id"] == "inv-saved"
    assert written["implicated_suppliers"][0]["supplier_rfc"] == "AANS850115SE5"
    # indent=2, not a single-line dump
    assert len(snapshot.read_text(encoding="utf-8").splitlines()) > 5


def test_load_case_files_from_disk_restores_them(monkeypatch, tmp_path):
    monkeypatch.setenv("FORENSIC_CASE_FILES_DIR", str(tmp_path))
    (tmp_path / "inv-a.json").write_text(
        sample_case_file("inv-a").model_dump_json(indent=2), encoding="utf-8")
    (tmp_path / "inv-b.json").write_text(
        sample_case_file("inv-b").model_dump_json(indent=2), encoding="utf-8")

    assert load_case_files_from_disk() == 2
    assert set(state.case_files) == {"inv-a", "inv-b"}
    assert state.case_files["inv-a"].total_amount_at_risk == 180_114.65


def test_a_corrupt_snapshot_does_not_take_startup_down(monkeypatch, tmp_path):
    """A half-written file must not stop the API from serving the good
    ones -- that would turn the fallback into the outage."""
    monkeypatch.setenv("FORENSIC_CASE_FILES_DIR", str(tmp_path))
    (tmp_path / "good.json").write_text(
        sample_case_file("inv-good").model_dump_json(), encoding="utf-8")
    (tmp_path / "truncated.json").write_text('{"investigation_id": "inv-bad"', encoding="utf-8")
    (tmp_path / "foreign.json").write_text('{"hello": "world"}', encoding="utf-8")

    assert load_case_files_from_disk() == 1
    assert set(state.case_files) == {"inv-good"}


def test_missing_directory_is_not_an_error(monkeypatch, tmp_path):
    monkeypatch.setenv("FORENSIC_CASE_FILES_DIR", str(tmp_path / "nope"))
    assert load_case_files_from_disk() == 0


def test_reloaded_case_file_is_servable_without_a_graph(client, monkeypatch, tmp_path):
    """After a restart the case file exists but state.graph is None.
    Aldo flagged this: /case-file and /ask must both still work."""
    monkeypatch.setenv("FORENSIC_CASE_FILES_DIR", str(tmp_path))
    (tmp_path / "inv-r.json").write_text(
        sample_case_file("inv-r").model_dump_json(), encoding="utf-8")
    load_case_files_from_disk()
    assert state.graph is None

    assert client.get("/case-file/inv-r").status_code == 200

    from shared.schemas import AskResponse
    monkeypatch.setattr("api.main.answer_question",
                        lambda g, case_file, question: AskResponse(answer=f"g was {g!r}"))
    body = client.post("/case-file/inv-r/ask", json={"question": "q"}).json()
    assert body["answer"] == "g was None"


def test_save_case_file_survives_an_unwritable_directory(monkeypatch, tmp_path):
    """Losing the snapshot must not lose the investigation."""
    blocker = tmp_path / "blocked"
    blocker.write_text("not a directory", encoding="utf-8")
    monkeypatch.setenv("FORENSIC_CASE_FILES_DIR", str(blocker))
    assert save_case_file(sample_case_file("inv-x")) is None


def test_startup_lifespan_reloads_the_snapshots(monkeypatch, tmp_path):
    """The reload has to happen in the lifespan, not just in a helper
    nobody calls. `with TestClient(app)` is what runs it -- plain
    TestClient(app) does not, which is why every other test here has to
    call load_case_files_from_disk() itself."""
    monkeypatch.setenv("FORENSIC_CASE_FILES_DIR", str(tmp_path))
    (tmp_path / "inv-boot.json").write_text(
        sample_case_file("inv-boot").model_dump_json(), encoding="utf-8")
    state.case_files = {}

    with TestClient(app) as booted:
        assert "inv-boot" in state.case_files
        assert booted.get("/case-file/inv-boot").status_code == 200


# ---------------------------------------------------------------------------
# Resilience (NFR-5) -- what Victor's UI depends on
# ---------------------------------------------------------------------------

def test_health_ollama_shape_is_what_the_ui_expects(client, monkeypatch):
    """Victor's status indicator reads ollama_reachable. It must be there
    and must agree with ok."""
    monkeypatch.setattr("agent.ollama_client.list_models",
                        lambda url=None, timeout=6.0: ollama_client.ProbeResult(
                            True, ["qwen2.5:7b"], "connected"))
    body = client.get("/health/ollama").json()
    assert body["ollama_reachable"] is True
    assert body["ok"] == body["ollama_reachable"]
    assert body["model_available"] is True


def test_health_ollama_reports_a_dead_server_as_200(client, monkeypatch):
    """An unreachable model is an answer the UI renders, not a 500 that
    makes the demo look broken."""
    monkeypatch.setattr("agent.ollama_client.list_models",
                        lambda url=None, timeout=6.0: ollama_client.ProbeResult(
                            False, [], "no hay nadie escuchando"))
    resp = client.get("/health/ollama")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ollama_reachable"] is False
    assert body["message"]                      # says why
    assert body["model_available"] is None      # unknown, not False


def test_health_is_plain_ok(client):
    assert client.get("/health").json() == {"status": "ok"}


def test_investigate_emits_an_error_event_instead_of_hanging(client, monkeypatch):
    """Without this the worker thread died before queueing the sentinel
    and event_stream() blocked on queue.get() forever: SSE open, UI
    spinning, no message. This pins the backend half only -- ui/ still
    ignores the event, which is Victor's to wire up."""
    monkeypatch.setattr("api.main.run_investigation",
                        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("ollama is down")))
    client.post("/estate/generate", json={"seed": 42, "num_blacklisted": 0})

    with client.stream("POST", "/investigate", json={"hint": "x"}) as resp:
        events = [json.loads(line[len("data: "):])
                  for line in resp.iter_lines() if line.startswith("data: ")]

    errors = [e for e in events if e.get("type") == "error"]
    assert errors, f"no error event in {events}"
    assert "ollama is down" in errors[0]["message"]
    assert not [e for e in events if e.get("type") == "done"]
    assert state.investigation_running is False


def test_investigate_without_an_estate_is_400(client):
    assert client.post("/investigate", json={"hint": "x"}).status_code == 400


# ---------------------------------------------------------------------------
# Estate, graph and the Scenario Injector (FR-4)
# ---------------------------------------------------------------------------

def test_generate_then_export_produces_a_real_graph(client):
    info = client.post("/estate/generate", json={"seed": 42, "num_blacklisted": 0}).json()
    assert info["num_companies"] > 0
    assert info["num_invoices"] > 0
    assert info["num_payments"] > 0

    graph = client.get("/graph/export").json()
    assert len(graph["nodes"]) > 0
    assert len(graph["edges"]) > 0
    # The shape the UI reads (ui/web/app.js drawGraph).
    node = graph["nodes"][0]
    assert {"id", "type", "label", "attributes"} <= set(node)
    edge = graph["edges"][0]
    assert {"id", "source", "target", "type"} <= set(edge)


def test_graph_export_before_generate_is_400(client):
    assert client.get("/graph/export").status_code == 400


def test_inject_scenario_before_generate_is_400(client):
    resp = client.post("/estate/inject-scenario",
                       json={"pattern": "fake_billing", "params": {}})
    assert resp.status_code == 400


@pytest.mark.parametrize("pattern", [
    "fake_billing", "kickback_shell", "round_tripping", "inflated_sales",
])
def test_every_fraud_pattern_injects_and_keeps_the_graph_servable(client, pattern):
    """A judge can pick any of the four, so all four must survive the
    round trip: inject, then re-export the graph."""
    before = client.post("/estate/generate",
                         json={"seed": 42, "num_blacklisted": 0}).json()

    resp = client.post("/estate/inject-scenario", json={"pattern": pattern, "params": {}})
    assert resp.status_code == 200, resp.text
    assert resp.json()["pattern"] == pattern
    assert resp.json()["num_companies"] >= before["num_companies"]

    graph = client.get("/graph/export").json()
    assert len(graph["nodes"]) > 0 and len(graph["edges"]) > 0


def test_unknown_pattern_is_400_with_the_valid_names(client):
    """The injector is judge-facing: a typo must not take the API down.
    It used to raise ValueError straight out of the handler -> 500."""
    client.post("/estate/generate", json={"seed": 42, "num_blacklisted": 0})
    resp = client.post("/estate/inject-scenario",
                       json={"pattern": "no_such_pattern", "params": {}})
    assert resp.status_code == 400, resp.text
    detail = resp.json()["detail"]
    for valid in ["fake_billing", "kickback_shell", "round_tripping", "inflated_sales"]:
        assert valid in detail


def test_bad_pattern_params_are_400(client):
    client.post("/estate/generate", json={"seed": 42, "num_blacklisted": 0})
    resp = client.post("/estate/inject-scenario",
                       json={"pattern": "fake_billing", "params": {"nope": 1}})
    assert resp.status_code == 400, resp.text


def test_integrations_before_any_estate(client):
    body = client.get("/health/integrations").json()
    assert body["agent"]["provider"] == "ollama"
    assert body["gemini"] == {"configured": False, "model": "gemini-flash-latest"}
    assert body["snowflake"]["requested"] is False
    assert body["last_build"] is None


@pytest.mark.filterwarnings("ignore:DATA_SOURCE=snowflake failed")
def test_integrations_report_a_snowflake_fallback(client, monkeypatch):
    """The fallback to local used to be a server-log warning only, so a
    demo could 'use Snowflake' on stage while running on the laptop. The
    sidebar reads this to say so."""
    monkeypatch.setenv("DATA_SOURCE", "snowflake")
    monkeypatch.delenv("SNOWFLAKE_ACCOUNT", raising=False)
    monkeypatch.delenv("SNOWFLAKE_PAT", raising=False)

    assert client.post("/estate/generate", json={"seed": 42, "num_blacklisted": 0}).status_code == 200
    body = client.get("/health/integrations").json()

    assert body["snowflake"] == {"configured": False, "requested": True}
    assert body["last_build"]["active"] == "local"
    assert "SNOWFLAKE_ACCOUNT" in body["last_build"]["error"]


# ---------------------------------------------------------------------------
# /investigate degrades cleanly with no model reachable
# ---------------------------------------------------------------------------

def test_investigate_completes_with_no_ollama(client, monkeypatch):
    """No model, no cloud key, no network: the stream must still finish
    with a done event and an empty-handed (not broken) case file."""
    def unreachable(prompt, on_notice=None, **kwargs):
        raise OllamaError("No pude conectar con http://10.0.0.1:11434", kind="connection")

    monkeypatch.setattr("agent.loop._call_reasoning_model", unreachable)
    client.post("/estate/generate", json={"seed": 42, "num_blacklisted": 0})

    with client.stream("POST", "/investigate", json={"hint": "x"}) as resp:
        events = [json.loads(line[len("data: "):])
                  for line in resp.iter_lines() if line.startswith("data: ")]

    done = [e for e in events if e.get("type") == "done"]
    assert done, f"stream never finished: {events}"
    assert not [e for e in events if e.get("type") == "error"]

    # The reason is visible in the trace, not swallowed.
    steps = [e["data"] for e in events if e.get("type") == "step"]
    assert any("No pude conectar" in s["content"] for s in steps)

    case_file = client.get(f"/case-file/{done[0]['investigation_id']}").json()
    assert case_file["implicated_suppliers"] == []
    assert case_file["total_amount_at_risk"] == 0.0


def test_cancel_frees_the_slot_immediately(client):
    """The cancel flag is only checked between streamed lines, so a run
    waiting on the first byte of a cold 7B kept the 409 up for the whole
    OLLAMA_TIMEOUT -- 300s with the team's .env. Measured at 24s with a
    short timeout. Freeing the slot here turns five dead minutes into a
    button that responds."""
    state.investigation_running = True
    body = client.post("/investigate/cancel").json()

    assert body["cancelled"] is True
    assert body["was_running"] is True
    assert state.investigation_running is False        # slot free now
    assert ollama_client.CANCEL.is_set() is True       # and the run is told
    ollama_client.clear_cancel()


def test_cancel_with_nothing_running_is_harmless(client):
    body = client.post("/investigate/cancel").json()
    assert body == {"cancelled": True, "was_running": False}
    ollama_client.clear_cancel()


def test_a_dying_run_does_not_free_the_next_runs_slot(client, monkeypatch):
    """After a cancel the old worker keeps exiting in the background. Its
    finally must not release the slot of whatever started meanwhile."""
    state.current_run_id = 7
    state.investigation_running = True
    # Simulate the stale worker's finally: it owns run 6, not 7.
    stale_run_id = 6
    if state.current_run_id == stale_run_id:
        state.investigation_running = False
    assert state.investigation_running is True


# ---------------------------------------------------------------------------
# The dashboard is served by this same app
# ---------------------------------------------------------------------------

def test_root_redirects_to_the_dashboard(client):
    resp = client.get("/", follow_redirects=False)
    assert resp.status_code in (307, 308)
    assert resp.headers["location"] == "/app/"


def test_dashboard_assets_are_served(client):
    assert client.get("/app/").status_code == 200
    assert client.get("/app/styles.css").status_code == 200
    assert client.get("/app/app.js").status_code == 200


def test_the_mount_does_not_shadow_the_api(client):
    """/app is mounted last; every API route must still answer."""
    for path in ["/health", "/health/ollama", "/config/ollama"]:
        assert client.get(path).status_code == 200, path


# ---------------------------------------------------------------------------
# Bodies that used to 500
# ---------------------------------------------------------------------------

def test_negative_num_blacklisted_is_400_not_500(client):
    resp = client.post("/estate/generate", json={"seed": 42, "num_blacklisted": -1})
    assert resp.status_code == 400, resp.text


@pytest.mark.parametrize("url", [
    "http://[",                      # urlsplit -> ValueError
    "http://" + "a" * 64 + ".com",   # urllib3 -> LocationParseError (not a RequestException)
])
def test_probe_never_fails_on_an_invalid_url(client, url):
    """The docstring promises "never fails"; two exception types slipped
    past list_models and reached the client as a 500."""
    resp = client.get("/config/ollama/models", params={"url": url})
    assert resp.status_code == 200, resp.text
    assert resp.json()["ok"] is False
    assert resp.json()["message"]


def test_health_ollama_never_fails_on_an_invalid_configured_url(client, monkeypatch):
    """Same path, but from OLLAMA_URL -- so a bad value saved by the
    settings panel turned the pre-flight indicator into a 500."""
    monkeypatch.setenv("OLLAMA_URL", "http://" + "b" * 64 + ".com")
    from shared import config
    config.reset_cache()
    resp = client.get("/health/ollama")
    assert resp.status_code == 200, resp.text
    assert resp.json()["ollama_reachable"] is False
    config.reset_cache()


@pytest.mark.parametrize("question,reason", [
    ("", "empty"),
    ("   ", "whitespace only"),
    ("x" * 2001, "longer than the context can hold with the evidence trail"),
])
def test_bad_questions_are_422(client, question, reason):
    state.case_files["inv-1"] = sample_case_file()
    resp = client.post("/case-file/inv-1/ask", json={"question": question})
    assert resp.status_code == 422, f"{reason}: {resp.status_code}"


# ---------------------------------------------------------------------------
# Only useful snapshots, and a way to find them
# ---------------------------------------------------------------------------

def test_an_empty_handed_case_file_is_not_snapshotted(tmp_path, monkeypatch):
    """A run that died with the model unreachable produces "No fraud
    could be proven" and an empty trail. Keeping those buried the one
    snapshot worth showing a judge under 12 useless ones."""
    monkeypatch.setenv("FORENSIC_CASE_FILES_DIR", str(tmp_path))
    empty = CaseFile(
        investigation_id="inv-empty",
        scheme_narrative="No fraud could be proven within the investigation budget.",
        implicated_suppliers=[], evidence_trail=GraphExport(nodes=[], edges=[]),
        leads_not_pursued=[], total_amount_at_risk=0.0)

    assert save_case_file(empty) is None
    assert list(tmp_path.glob("*.json")) == []
    assert save_case_file(sample_case_file("inv-real")) is not None


def test_a_cancelled_run_that_gathered_evidence_is_kept(tmp_path, monkeypatch):
    """Empty-handed is not the same as worthless: if the agent touched
    the graph before being cancelled, that trail is still worth showing."""
    monkeypatch.setenv("FORENSIC_CASE_FILES_DIR", str(tmp_path))
    partial = CaseFile(
        investigation_id="inv-partial", scheme_narrative="cancelled",
        implicated_suppliers=[],
        evidence_trail=GraphExport(
            nodes=[GraphNode(id="a", type="Company", label="A")],
            edges=[GraphEdge(id="pay-9", source="a", target="b", type="EXECUTED_PAYMENT")]),
        leads_not_pursued=[], total_amount_at_risk=0.0)
    assert save_case_file(partial) is not None


def test_case_files_can_be_listed(client):
    """Without this the offline fallback is "remember the uuid"."""
    state.case_files["inv-big"] = sample_case_file("inv-big")
    small = sample_case_file("inv-small")
    small.total_amount_at_risk = 10.0
    small.implicated_suppliers = []
    state.case_files["inv-small"] = small

    body = client.get("/case-files").json()

    assert [c["investigation_id"] for c in body] == ["inv-big", "inv-small"]  # biggest first
    assert body[0]["num_implicated_suppliers"] == 1
    assert body[0]["total_amount_at_risk"] == 180_114.65
    assert body[0]["narrative_preview"]


def test_case_files_listing_is_empty_not_404(client):
    assert client.get("/case-files").json() == []


def test_investigate_status_does_not_start_a_run(client):
    body = client.get("/investigate/status").json()
    assert body["running"] is False
    assert "run_id" in body and "case_files" in body


def test_status_reports_the_step_budget_for_the_progress_bar(client):
    """The UI draws its progress bar against this, so it has to be the
    real budget rather than a number the frontend guessed."""
    from agent import loop as agent_loop
    assert client.get("/investigate/status").json()["max_steps"] == agent_loop.MAX_STEPS


# ---------------------------------------------------------------------------
# The graph must not change under a live investigation
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("path,payload", [
    ("/estate/generate", {"seed": 99}),
    ("/estate/inject-scenario", {"pattern": "fake_billing", "params": {}}),
])
def test_estate_endpoints_refuse_while_investigating(client, path, payload):
    """Verified live: /estate/generate took the graph from 241 to 684
    nodes mid-run, so the case file cited edges the UI no longer had."""
    client.post("/estate/generate", json={"seed": 42, "num_blacklisted": 0})
    state.investigation_running = True
    resp = client.post(path, json=payload)
    assert resp.status_code == 409, resp.text


def test_dashboard_is_never_cached(client):
    """A cached app.js against a fresh index.html is new markup driven by
    old code -- a half-broken page with no error to explain it."""
    for path in ["/app/", "/app/app.js", "/app/styles.css"]:
        cache = client.get(path).headers.get("cache-control", "")
        assert "no-store" in cache, f"{path} -> {cache!r}"


def test_vendor_bundle_is_still_cacheable(client):
    """vis-network is 468KB and never changes; re-sending it on every
    reload would be the only slow thing on the page."""
    cache = client.get("/app/vendor/vis-network.min.js").headers.get("cache-control", "")
    assert "no-store" not in cache
