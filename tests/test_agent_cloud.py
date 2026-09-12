"""
agent/cloud.py -- the Gemini client behind FR-15 (narrative synthesis)
and FR-19 (/ask). Nothing here touches the network: what matters for the
demo is not that Gemini answers well, it is that every way Gemini can
fail arrives at the callers as one catchable thing, so an exhausted free
tier degrades to the local model instead of ending the run (NFR-5).
"""
import pytest

from agent import cloud


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
        if isinstance(self._result, Exception):
            raise self._result
        return _FakeResponse(self._result)


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
