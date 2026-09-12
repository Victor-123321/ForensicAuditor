"""
agent/cloud.py -- the Gemini client behind FR-15 (narrative synthesis)
and FR-19 (/ask). Nothing here touches the network: what matters for the
demo is not that Gemini answers well, it is that every way Gemini can
fail arrives at the callers as one catchable thing, so an exhausted free
tier degrades to the local model instead of ending the run (NFR-5).
"""
import pytest
from google.genai import errors as genai_errors

from agent import cloud


@pytest.fixture(autouse=True)
def no_retry_sleep(monkeypatch):
    """The retry backoff is real seconds in production. Suites run on
    every save, so burn none of them here -- what is under test is which
    calls happen, not the wall clock between them."""
    monkeypatch.setattr(cloud.time, "sleep", lambda _seconds: None)


def _server_error(code=503):
    return genai_errors.ServerError(
        code, {"error": {"message": "high demand", "status": "UNAVAILABLE"}})


def _client_error(code=403):
    return genai_errors.ClientError(
        code, {"error": {"message": "bad key", "status": "PERMISSION_DENIED"}})


class _FakeResponse:
    def __init__(self, text):
        self.text = text


class _FakeModels:
    """Records what generate_content was called with."""
    def __init__(self, result):
        self._result = result
        self.calls = []

    def generate_content(self, *, model, contents, config):
        self.calls.append({"model": model, "contents": contents, "config": config})
        result = self._result
        if callable(result):  # per-call scripting: fn(model, call_index)
            result = result(model, len(self.calls) - 1)
        if isinstance(result, Exception):
            raise result
        return _FakeResponse(result)


class _FakeClient:
    def __init__(self, result):
        self.models = _FakeModels(result)


@pytest.fixture
def fake_cloud(monkeypatch):
    """Swaps in a stub client and returns a factory that arms it with a
    response (or an exception to raise)."""
    def arm(result):
        client = _FakeClient(result)
        monkeypatch.setattr(cloud, "_get_client", lambda: client)
        return client
    return arm


# ---------------------------------------------------------------------------
# Availability
# ---------------------------------------------------------------------------

def test_cloud_available_is_false_without_a_key(monkeypatch):
    monkeypatch.delenv("CLOUD_LLM_API_KEY", raising=False)
    assert cloud.cloud_available() is False


def test_cloud_available_is_false_for_an_empty_key(monkeypatch):
    """.env.example ships CLOUD_LLM_API_KEY= with nothing after it, so
    "set but empty" is the state most machines are actually in."""
    monkeypatch.setenv("CLOUD_LLM_API_KEY", "")
    assert cloud.cloud_available() is False


def test_cloud_available_is_true_with_a_key(monkeypatch):
    monkeypatch.setenv("CLOUD_LLM_API_KEY", "AIza-not-a-real-key")
    assert cloud.cloud_available() is True


# ---------------------------------------------------------------------------
# Failure normalization -- the reason CloudError exists
# ---------------------------------------------------------------------------

def test_cloud_error_is_a_runtime_error():
    """agent/loop.py and agent/qa.py both fall back on `except
    RuntimeError`. If CloudError ever stops subclassing it, both
    fallbacks go silent and the demo crashes instead of degrading."""
    assert issubclass(cloud.CloudError, RuntimeError)


def test_call_without_a_key_raises_cloud_error(monkeypatch):
    monkeypatch.delenv("CLOUD_LLM_API_KEY", raising=False)
    with pytest.raises(cloud.CloudError, match="CLOUD_LLM_API_KEY"):
        cloud.call_cloud_model("anything")


def test_an_sdk_failure_becomes_a_cloud_error(fake_cloud):
    """Quota exhausted mid-demo is the scenario this protects: whatever
    the SDK raises, the caller sees CloudError and keeps going."""
    fake_cloud(Exception("429 RESOURCE_EXHAUSTED: quota exceeded"))
    with pytest.raises(cloud.CloudError, match="RESOURCE_EXHAUSTED"):
        cloud.call_cloud_model("prompt")


def test_an_empty_response_becomes_a_cloud_error(fake_cloud):
    """A safety filter returns a successful call with no text. Passing
    that through would blank the scheme_narrative; failing makes the
    caller keep the local model's version instead."""
    fake_cloud("   ")
    with pytest.raises(cloud.CloudError, match="empty response"):
        cloud.call_cloud_model("name some companies accused of fraud")


def test_a_none_response_becomes_a_cloud_error(fake_cloud):
    fake_cloud(None)
    with pytest.raises(cloud.CloudError):
        cloud.call_cloud_model("prompt")


# ---------------------------------------------------------------------------
# Happy path and model selection
# ---------------------------------------------------------------------------

def test_returns_the_stripped_response_text(fake_cloud):
    fake_cloud("  La red de facturas falsas mueve $2M MXN.  ")
    assert cloud.call_cloud_model("resume esto") == "La red de facturas falsas mueve $2M MXN."


def test_defaults_to_the_rolling_flash_alias(monkeypatch, fake_cloud):
    """Pinning a dated version would 404 the whole demo if Google
    retires it mid-competition."""
    monkeypatch.delenv("CLOUD_LLM_MODEL", raising=False)
    client = fake_cloud("ok")
    cloud.call_cloud_model("prompt")
    assert client.models.calls[0]["model"] == "gemini-flash-latest"
    assert cloud.DEFAULT_CLOUD_LLM_MODEL == "gemini-flash-latest"


def test_cloud_llm_model_env_var_overrides_the_default(monkeypatch, fake_cloud):
    monkeypatch.setenv("CLOUD_LLM_MODEL", "gemini-3.1-pro")
    client = fake_cloud("ok")
    cloud.call_cloud_model("prompt")
    assert client.models.calls[0]["model"] == "gemini-3.1-pro"


def test_passes_the_prompt_and_temperature_through(fake_cloud):
    client = fake_cloud("ok")
    cloud.call_cloud_model("the prompt", temperature=0.7)
    call = client.models.calls[0]
    assert call["contents"] == "the prompt"
    assert call["config"].temperature == 0.7


# ---------------------------------------------------------------------------
# Retry and model fallback -- both added after a live 503 during testing
# ---------------------------------------------------------------------------

def test_a_transient_503_is_retried_and_can_succeed(fake_cloud):
    """Measured against the real API on 2026-09-12: 1 call in 5 came back
    503 "high demand" and the immediate retry worked. Without this the
    spike costs the run its Gemini-polished narrative."""
    client = fake_cloud(lambda model, i: _server_error() if i == 0 else "recuperado")

    assert cloud.call_cloud_model("prompt") == "recuperado"
    assert len(client.models.calls) == 2
    # Still the primary model -- a retry must not silently downgrade.
    assert {c["model"] for c in client.models.calls} == {cloud.DEFAULT_CLOUD_LLM_MODEL}


def test_falls_back_to_the_next_model_when_the_primary_stays_down(monkeypatch, fake_cloud):
    """Staying on SOME Gemini model is the point: dropping to the local
    model leaves the run with no cloud generative-AI call in it at all."""
    monkeypatch.delenv("CLOUD_LLM_MODEL", raising=False)
    client = fake_cloud(
        lambda model, i: "ok desde el respaldo"
        if model != cloud.DEFAULT_CLOUD_LLM_MODEL else _server_error())

    assert cloud.call_cloud_model("prompt") == "ok desde el respaldo"

    tried = [c["model"] for c in client.models.calls]
    # Primary exhausts its retries first, then the first spare answers.
    assert tried[:3] == [cloud.DEFAULT_CLOUD_LLM_MODEL] * 3
    assert tried[3] == cloud.FALLBACK_MODELS[0]


def test_gives_up_with_cloud_error_when_every_model_is_down(fake_cloud):
    client = fake_cloud(lambda model, i: _server_error())
    with pytest.raises(cloud.CloudError, match="high demand"):
        cloud.call_cloud_model("prompt")
    # 3 attempts on each of primary + 2 spares, and no more.
    assert len(client.models.calls) == 9


def test_a_permanent_error_is_not_retried_on_the_same_model(fake_cloud):
    """A bad key fails identically no matter how many times it is asked;
    retrying it just delays the local-model fallback the demo needs."""
    client = fake_cloud(lambda model, i: _client_error(403))
    with pytest.raises(cloud.CloudError):
        cloud.call_cloud_model("prompt")

    tried = [c["model"] for c in client.models.calls]
    # Exactly one attempt per model, no repeats: the spares are still
    # worth trying (a 404 is per-model), but never the same one twice.
    assert len(set(tried)) == len(tried)
    assert len(tried) == 1 + len(cloud.FALLBACK_MODELS)


def test_a_safety_block_is_not_retried(fake_cloud):
    """An empty response is a filter, not a hiccup: the identical prompt
    would be filtered again, on every model."""
    client = fake_cloud("")
    with pytest.raises(cloud.CloudError, match="empty response"):
        cloud.call_cloud_model("acusa a estas empresas de fraude")
    assert len(client.models.calls) == 1


def test_a_configured_model_is_never_duplicated_in_the_chain(monkeypatch, fake_cloud):
    """Setting CLOUD_LLM_MODEL to one of the spares must not make it get
    tried twice while a working spare goes untried."""
    monkeypatch.setenv("CLOUD_LLM_MODEL", cloud.FALLBACK_MODELS[0])
    client = fake_cloud(lambda model, i: _server_error())
    with pytest.raises(cloud.CloudError):
        cloud.call_cloud_model("prompt")

    ordered = list(dict.fromkeys(c["model"] for c in client.models.calls))
    assert ordered == [cloud.FALLBACK_MODELS[0], cloud.FALLBACK_MODELS[1]]
