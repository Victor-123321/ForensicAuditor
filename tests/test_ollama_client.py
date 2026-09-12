"""
Ollama client tests -- a fake server, no network.

Every test monkeypatches `requests` inside agent.ollama_client, so the
suite runs identically on a laptop with no Ollama, on a plane, and in CI.
"""
from __future__ import annotations

import copy
import json

import pytest
import requests

from agent import ollama_client
from agent.ollama_client import CancelToken, OllamaError, chat, list_models, supports_vision
from shared.config import OllamaSettings

SETTINGS = OllamaSettings(url="192.168.1.50", model="qwen2.5:7b", timeout=42, num_ctx=8192)


# ---------------------------------------------------------------------------
# Fake server plumbing
# ---------------------------------------------------------------------------

class FakeResponse:
    def __init__(self, status_code=200, json_body=None, lines=None, text=None):
        self.status_code = status_code
        self._json = json_body
        self._lines = lines or []
        self.text = text if text is not None else json.dumps(json_body or {})

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def json(self):
        if self._json is None:
            raise ValueError("no JSON in this response")
        return self._json

    def iter_lines(self):
        for line in self._lines:
            yield line if isinstance(line, bytes) else json.dumps(line).encode()


def chunk(content: str) -> dict:
    return {"message": {"role": "assistant", "content": content}, "done": False}


def done_chunk(**metrics) -> dict:
    payload = {"message": {"role": "assistant", "content": ""}, "done": True}
    payload.update(metrics)
    return payload


def fake_transport(monkeypatch, *, get=None, post=None):
    """Installs fake requests.get/post and returns the recorded calls."""
    calls: list[dict] = []

    def record(method, handler):
        def _call(url, **kwargs):
            # Deep-copy the body: `requests` serializes it at send time,
            # so a later retry mutating the same dicts must not rewrite
            # the record of what the first attempt actually sent.
            recorded = {**kwargs}
            if "json" in recorded:
                recorded["json"] = copy.deepcopy(recorded["json"])
            calls.append({"method": method, "url": url, **recorded})
            if isinstance(handler, Exception):
                raise handler
            if callable(handler):
                return handler(url, **kwargs)
            return handler
        return _call

    if get is not None:
        monkeypatch.setattr(ollama_client.requests, "get", record("GET", get))
    if post is not None:
        monkeypatch.setattr(ollama_client.requests, "post", record("POST", post))
    return calls


# ---------------------------------------------------------------------------
# Probe: list_models
# ---------------------------------------------------------------------------

def test_list_models_returns_sorted_names(monkeypatch):
    body = {"models": [{"name": "qwen2.5:7b"}, {"name": "llama3.1:8b"}]}
    calls = fake_transport(monkeypatch, get=FakeResponse(json_body=body))

    ok, models, message = list_models("192.168.1.50")

    assert ok is True
    assert models == ["llama3.1:8b", "qwen2.5:7b"]
    assert "2 modelo" in message
    # The bare IP the user typed was normalized before being hit.
    assert calls[0]["url"] == "http://192.168.1.50:11434/api/tags"
    assert calls[0]["timeout"] == ollama_client.PROBE_TIMEOUT


def test_list_models_connected_but_empty_suggests_pull(monkeypatch):
    fake_transport(monkeypatch, get=FakeResponse(json_body={"models": []}))
    result = list_models("192.168.1.50")
    assert result.ok is True
    assert result.models == []
    assert "ollama pull" in result.message


def test_list_models_connection_refused_is_not_an_exception(monkeypatch):
    fake_transport(monkeypatch, get=requests.exceptions.ConnectionError("refused"))
    result = list_models("192.168.1.50")
    assert result.ok is False
    assert "No hay nadie escuchando" in result.message
    assert "ollama serve" in result.message


def test_list_models_timeout_mentions_the_timeout(monkeypatch):
    fake_transport(monkeypatch, get=requests.exceptions.Timeout("slow"))
    result = list_models("192.168.1.50", timeout=6)
    assert result.ok is False
    assert "6s" in result.message


def test_list_models_on_a_non_ollama_port(monkeypatch):
    fake_transport(monkeypatch, get=FakeResponse(status_code=502))
    result = list_models("192.168.1.50")
    assert result.ok is False
    assert "HTTP 502" in result.message


def test_list_models_on_something_that_is_not_json(monkeypatch):
    fake_transport(monkeypatch, get=FakeResponse(status_code=200, json_body=None,
                                                 text="<html>router login</html>"))
    result = list_models("192.168.1.50")
    assert result.ok is False
    assert "no es JSON" in result.message


# ---------------------------------------------------------------------------
# Probe: supports_vision
# ---------------------------------------------------------------------------

def test_supports_vision_reads_capabilities(monkeypatch):
    fake_transport(monkeypatch, post=FakeResponse(
        json_body={"capabilities": ["completion", "vision"]}))
    assert supports_vision("192.168.1.50", "llava:7b") is True


def test_supports_vision_false_for_text_only_model(monkeypatch):
    fake_transport(monkeypatch, post=FakeResponse(
        json_body={"capabilities": ["completion", "tools"]}))
    assert supports_vision("192.168.1.50", "qwen2.5:7b") is False


def test_supports_vision_legacy_server_uses_families(monkeypatch):
    fake_transport(monkeypatch, post=FakeResponse(
        json_body={"details": {"family": "llama", "families": ["llama", "clip"]}}))
    assert supports_vision("192.168.1.50", "llava:7b") is True


def test_supports_vision_unknown_when_probe_fails(monkeypatch):
    fake_transport(monkeypatch, post=requests.exceptions.ConnectionError("down"))
    assert supports_vision("192.168.1.50", "qwen2.5:7b") is None


def test_supports_vision_is_cached(monkeypatch):
    calls = fake_transport(monkeypatch, post=FakeResponse(json_body={"capabilities": ["vision"]}))
    assert supports_vision("192.168.1.50", "llava:7b") is True
    assert supports_vision("192.168.1.50", "llava:7b") is True
    assert len(calls) == 1


# ---------------------------------------------------------------------------
# Chat streaming
# ---------------------------------------------------------------------------

def test_chat_streams_tokens_and_collects_metrics(monkeypatch):
    stream = [chunk("El proveedor "), chunk("RFC123 "), chunk("está en la lista negra."),
              done_chunk(prompt_eval_count=120, eval_count=64, eval_duration=2_000_000_000,
                         total_duration=3_000_000_000)]
    calls = fake_transport(monkeypatch, post=FakeResponse(lines=stream))

    seen: list[str] = []
    result = chat([{"role": "user", "content": "quién es RFC123"}],
                  settings=SETTINGS, on_token=seen.append)

    assert result.content == "El proveedor RFC123 está en la lista negra."
    assert seen == ["El proveedor ", "RFC123 ", "está en la lista negra."]
    assert result.cancelled is False
    assert result.via == "ollama"
    assert result.metrics.prompt_eval_count == 120
    assert result.metrics.eval_count == 64
    assert result.metrics.tokens_per_second == pytest.approx(32.0)

    sent = calls[0]
    assert sent["url"] == "http://192.168.1.50:11434/api/chat"
    assert sent["stream"] is True
    assert sent["timeout"] == (ollama_client.CONNECT_TIMEOUT, 42)
    body = sent["json"]
    assert body["model"] == "qwen2.5:7b"
    assert body["stream"] is True
    assert body["keep_alive"] == "30m"
    assert body["options"] == {"num_ctx": 8192, "temperature": 0.7}


def test_chat_temperature_override(monkeypatch):
    calls = fake_transport(monkeypatch, post=FakeResponse(lines=[done_chunk()]))
    chat([{"role": "user", "content": "hola"}], settings=SETTINGS, temperature=0.2)
    assert calls[0]["json"]["options"]["temperature"] == 0.2


def test_chat_ignores_unparsable_lines(monkeypatch):
    stream = [b"", b"not json", chunk("ok"), done_chunk()]
    fake_transport(monkeypatch, post=FakeResponse(lines=stream))
    assert chat([{"role": "user", "content": "x"}], settings=SETTINGS).content == "ok"


def test_chat_raises_on_error_inside_the_stream(monkeypatch):
    stream = [chunk("empiezo"), {"error": "context window exceeded"}]
    fake_transport(monkeypatch, post=FakeResponse(lines=stream))
    with pytest.raises(OllamaError) as excinfo:
        chat([{"role": "user", "content": "x"}], settings=SETTINGS)
    assert "context window exceeded" in str(excinfo.value)
    assert excinfo.value.partial == "empiezo"


# ---------------------------------------------------------------------------
# Cancellation
# ---------------------------------------------------------------------------

def test_cancel_stops_mid_stream_and_keeps_partial_text(monkeypatch):
    token = CancelToken()

    def interrupted_stream():
        yield json.dumps(chunk("primera parte")).encode()
        token.cancel()          # the user hits "Detener" right here
        yield json.dumps(chunk("esto ya no debería llegar")).encode()
        yield json.dumps(done_chunk()).encode()

    response = FakeResponse()
    response.iter_lines = interrupted_stream
    fake_transport(monkeypatch, post=response)

    result = chat([{"role": "user", "content": "x"}], settings=SETTINGS, cancel=token)

    assert result.cancelled is True
    assert result.content == "primera parte"


def test_cancel_flag_is_reset_before_each_send(monkeypatch):
    """A cancel left over from the previous generation must not kill the
    next one before it starts."""
    token = CancelToken()
    token.cancel()
    fake_transport(monkeypatch, post=FakeResponse(lines=[chunk("respuesta"), done_chunk()]))

    result = chat([{"role": "user", "content": "x"}], settings=SETTINGS, cancel=token)

    assert result.cancelled is False
    assert result.content == "respuesta"


def test_module_level_cancel_helpers(monkeypatch):
    ollama_client.request_cancel()
    assert ollama_client.CANCEL.is_set() is True
    fake_transport(monkeypatch, post=FakeResponse(lines=[chunk("hola"), done_chunk()]))
    # chat() uses the module token when none is passed, and resets it.
    assert chat([{"role": "user", "content": "x"}], settings=SETTINGS).content == "hola"


# ---------------------------------------------------------------------------
# Error translation
# ---------------------------------------------------------------------------

def test_connection_error_says_what_to_check(monkeypatch):
    fake_transport(monkeypatch, post=requests.exceptions.ConnectionError("refused"))
    with pytest.raises(OllamaError) as excinfo:
        chat([{"role": "user", "content": "x"}], settings=SETTINGS)
    error = excinfo.value
    assert error.kind == "connection"
    assert "No pude conectar con http://192.168.1.50:11434" in str(error)
    assert "firewall" in str(error)


def test_read_timeout_suggests_raising_the_timeout(monkeypatch):
    fake_transport(monkeypatch, post=requests.exceptions.ReadTimeout("slow"))
    with pytest.raises(OllamaError) as excinfo:
        chat([{"role": "user", "content": "x"}], settings=SETTINGS)
    assert excinfo.value.kind == "timeout"
    assert "42s" in str(excinfo.value)
    assert "frío" in str(excinfo.value)


def test_http_error_surfaces_ollamas_own_message(monkeypatch):
    fake_transport(monkeypatch, post=FakeResponse(
        status_code=404, json_body={"error": "model 'qwen2.5:7b' not found"}))
    with pytest.raises(OllamaError) as excinfo:
        chat([{"role": "user", "content": "x"}], settings=SETTINGS)
    assert "not found" in str(excinfo.value)
    assert "ollama pull qwen2.5:7b" in str(excinfo.value)


def test_missing_model_name_is_caught_before_any_request(monkeypatch):
    calls = fake_transport(monkeypatch, post=FakeResponse(lines=[done_chunk()]))
    with pytest.raises(OllamaError) as excinfo:
        chat([{"role": "user", "content": "x"}],
             settings=OllamaSettings(url="192.168.1.50", model=""))
    assert "OLLAMA_MODEL" in str(excinfo.value)
    assert calls == []


# ---------------------------------------------------------------------------
# Images on a text-only model
# ---------------------------------------------------------------------------

def test_images_are_dropped_when_the_model_has_no_vision(monkeypatch):
    def handler(url, **kwargs):
        if url.endswith("/api/show"):
            return FakeResponse(json_body={"capabilities": ["completion"]})
        return FakeResponse(lines=[chunk("solo texto"), done_chunk()])

    calls = fake_transport(monkeypatch, post=handler)
    notices: list[str] = []
    result = chat([{"role": "user", "content": "mira esto"}], settings=SETTINGS,
                  images=["BASE64DATA"], on_notice=notices.append)

    chat_call = [c for c in calls if c["url"].endswith("/api/chat")][0]
    assert "images" not in chat_call["json"]["messages"][0]
    assert result.content == "solo texto"
    assert any("no acepta imágenes" in n for n in notices)


def test_http_400_degrades_to_text_and_retries_once(monkeypatch):
    """If /api/show lied (or wasn't available), a 400 is the other way to
    find out -- resend without images instead of losing the turn."""
    attempts: list[dict] = []

    def handler(url, **kwargs):
        if url.endswith("/api/show"):
            raise requests.exceptions.ConnectionError("no show endpoint")
        attempts.append(copy.deepcopy(kwargs["json"]))
        if len(attempts) == 1:
            return FakeResponse(status_code=400,
                                json_body={"error": "this model does not support images"})
        return FakeResponse(lines=[chunk("texto plano"), done_chunk()])

    fake_transport(monkeypatch, post=handler)
    notices: list[str] = []
    result = chat([{"role": "user", "content": "mira esto"}], settings=SETTINGS,
                  images=["BASE64DATA"], on_notice=notices.append)

    assert len(attempts) == 2
    assert "images" in attempts[0]["messages"][0]
    assert "images" not in attempts[1]["messages"][0]
    assert result.content == "texto plano"
    assert any("HTTP 400" in n for n in notices)


def test_images_attach_to_the_last_user_message(monkeypatch):
    def handler(url, **kwargs):
        if url.endswith("/api/show"):
            return FakeResponse(json_body={"capabilities": ["vision"]})
        return FakeResponse(lines=[done_chunk()])

    calls = fake_transport(monkeypatch, post=handler)
    chat([{"role": "system", "content": "eres un auditor"},
          {"role": "user", "content": "primera"},
          {"role": "assistant", "content": "ok"},
          {"role": "user", "content": "mira esta factura"}],
         settings=SETTINGS, images=["B64"])

    messages = [c for c in calls if c["url"].endswith("/api/chat")][0]["json"]["messages"]
    assert messages[-1]["images"] == ["B64"]
    assert all("images" not in m for m in messages[:-1])


# ---------------------------------------------------------------------------
# Cloud fallback
# ---------------------------------------------------------------------------

def test_falls_back_to_the_cloud_once_and_says_so(monkeypatch):
    fake_transport(monkeypatch, post=requests.exceptions.ConnectionError("laptop asleep"))
    cloud_calls: list[list[dict]] = []

    def cloud(messages):
        cloud_calls.append(messages)
        return "respuesta desde la nube"

    ollama_client.register_cloud_fallback(cloud)
    notices: list[str] = []
    result = chat([{"role": "user", "content": "x"}], settings=SETTINGS,
                  on_notice=notices.append)

    assert result.via == "cloud"
    assert result.content == "respuesta desde la nube"
    assert len(cloud_calls) == 1          # exactly once, no retry storm
    assert notices and "nube" in notices[0]


def test_no_fallback_registered_means_a_loud_failure(monkeypatch):
    fake_transport(monkeypatch, post=requests.exceptions.ConnectionError("down"))
    with pytest.raises(OllamaError):
        chat([{"role": "user", "content": "x"}], settings=SETTINGS)


def test_fallback_is_skipped_for_model_errors(monkeypatch):
    """A 404 from the server is not a network problem -- burning the
    cloud budget on it would hide a fixable mistake."""
    fake_transport(monkeypatch, post=FakeResponse(status_code=404,
                                                  json_body={"error": "model not found"}))
    ollama_client.register_cloud_fallback(lambda messages: "nube")
    with pytest.raises(OllamaError):
        chat([{"role": "user", "content": "x"}], settings=SETTINGS)


def test_fallback_failure_reports_both_problems(monkeypatch):
    fake_transport(monkeypatch, post=requests.exceptions.ConnectionError("down"))

    def broken_cloud(messages):
        raise RuntimeError("sin API key")

    ollama_client.register_cloud_fallback(broken_cloud)
    with pytest.raises(OllamaError) as excinfo:
        chat([{"role": "user", "content": "x"}], settings=SETTINGS)
    assert "No pude conectar" in str(excinfo.value)
    assert "sin API key" in str(excinfo.value)
