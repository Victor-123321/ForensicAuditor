"""
API tests (Diego). TestClient over api.main.app -- no Ollama, no cloud
key, no network: anything that would reach a model is monkeypatched.
"""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from agent import ollama_client
from api.main import app
from api.state import load_case_files_from_disk, save_case_file, state
from shared.schemas import AccusationClaim, CaseFile, GraphEdge, GraphExport, GraphNode


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture(autouse=True)
def clean_state():
    state.estate = None
    state.graph = None
    state.case_files = {}
    state.investigation_running = False
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
    spinning, no message. Victor's UI needs the error event to render."""
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
