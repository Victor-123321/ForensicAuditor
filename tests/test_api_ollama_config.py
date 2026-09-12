"""
The connection-settings endpoints the UI's "Modelo local" panel drives.

Reuses the fake transport from test_ollama_client -- still no network.
"""
from __future__ import annotations

import pytest
import requests
from fastapi.testclient import TestClient

from agent import ollama_client
from api.main import app
from shared import config
from tests.test_ollama_client import FakeResponse, fake_transport


@pytest.fixture
def client():
    return TestClient(app)


def test_get_config_reports_defaults_and_config_file(client):
    body = client.get("/config/ollama").json()
    assert body["settings"]["url"] == "http://localhost:11434"
    assert body["settings"]["model"] == "qwen2.5:7b"
    assert body["env_overrides"] == {}
    assert body["config_file"] == str(config.config_path())


def test_put_config_normalizes_and_persists(client):
    # Exactly what the server-side setup hands the team, pasted as-is.
    payload = {"url": "http://192.168.1.50:11434/api/generate", "model": "qwen2.5:7b",
               "keep_alive": "30m", "num_ctx": 8192, "timeout": 300, "temperature": 0.7}
    saved = client.put("/config/ollama", json=payload).json()
    assert saved["settings"]["url"] == "http://192.168.1.50:11434"

    # ...and it survives into the next request, from the config file.
    assert client.get("/config/ollama").json()["settings"]["url"] == "http://192.168.1.50:11434"
    assert config.config_path().exists()


def test_put_config_flags_env_pinned_keys(client, monkeypatch):
    monkeypatch.setenv("OLLAMA_URL", "http://10.0.0.9:11434")
    body = client.put("/config/ollama", json={"url": "192.168.1.50"}).json()
    # The save happened, but the environment still wins -- say so rather
    # than letting the user think their edit took effect.
    assert body["env_overrides"] == {"url": "http://10.0.0.9:11434"}
    assert body["settings"]["url"] == "http://10.0.0.9:11434"


def test_probe_endpoint_lists_models(client, monkeypatch):
    fake_transport(monkeypatch, get=FakeResponse(
        json_body={"models": [{"name": "qwen2.5:7b"}, {"name": "nomic-embed-text:latest"}]}))
    body = client.get("/config/ollama/models", params={"url": "192.168.1.50"}).json()
    assert body["ok"] is True
    assert "qwen2.5:7b" in body["models"]


def test_probe_endpoint_reports_a_dead_server_as_200(client, monkeypatch):
    """A laptop that's asleep is an answer for the panel to render, not
    a 500 that makes the UI look broken."""
    fake_transport(monkeypatch, get=requests.exceptions.ConnectionError("refused"))
    resp = client.get("/config/ollama/models", params={"url": "192.168.1.50"})
    assert resp.status_code == 200
    assert resp.json()["ok"] is False
    assert "ollama serve" in resp.json()["message"]


def test_health_ollama_confirms_the_configured_model_is_there(client, monkeypatch):
    monkeypatch.setenv("OLLAMA_URL", "192.168.1.50")
    monkeypatch.setenv("OLLAMA_MODEL", "qwen2.5:7b")
    fake_transport(monkeypatch, get=FakeResponse(json_body={"models": [{"name": "qwen2.5:7b"}]}))
    body = client.get("/health/ollama").json()
    assert body["ok"] is True
    assert body["url"] == "http://192.168.1.50:11434"
    assert body["model_available"] is True


def test_health_ollama_says_when_the_model_is_missing(client, monkeypatch):
    monkeypatch.setenv("OLLAMA_MODEL", "qwen2.5:7b")
    fake_transport(monkeypatch, get=FakeResponse(json_body={"models": [{"name": "llama3.1:8b"}]}))
    body = client.get("/health/ollama").json()
    assert body["ok"] is True
    assert body["model_available"] is False
    assert "no tiene 'qwen2.5:7b'" in body["message"]
    assert "llama3.1:8b" in body["message"]


def test_cancel_endpoint_raises_the_flag(client):
    assert ollama_client.CANCEL.is_set() is False
    body = client.post("/investigate/cancel").json()
    assert body["cancelled"] is True
    assert ollama_client.CANCEL.is_set() is True
