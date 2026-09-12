"""
Cloud-model client (FR-15, NFR-4), backed by Google Gemini. The SRS
reserves cloud calls for two things per investigation -- final case-file
narrative synthesis (agent/loop.py) and the live Q&A follow-up (FR-19,
api/main.py's /ask endpoint) -- capping total cloud usage at <=2
calls/investigation. Every intermediate ReAct step stays on the local
model (agent/loop.py::_call_local_model), so this module is never on the
loop's hot path. api/main.py also registers call_cloud_model as
ollama_client's emergency fallback for when the LAN server dies
mid-demo; that path is off-budget because it only fires when the local
model produced nothing at all.

Uses the `google-genai` SDK (the current one -- the older
`google-generativeai` package is deprecated). CLOUD_LLM_MODEL defaults
to the rolling alias `gemini-flash-latest` rather than a hand-pinned
dated version, so a Google release mid-competition doesn't strand us on
a retired model id. Get a free key at https://aistudio.google.com.

Every failure leaves here as CloudError, which subclasses RuntimeError
so the existing callers' `except RuntimeError` keeps catching it. That
normalization is the point: callers implement NFR-5's local-only
fallback around one exception type instead of knowing what the SDK
raises underneath.
"""
from __future__ import annotations

import os

from google import genai
from google.genai import types

import shared.config  # noqa: F401 -- imported for its load_dotenv() side effect

DEFAULT_CLOUD_LLM_MODEL = "gemini-flash-latest"

# Cached alongside the key it was built with: re-reading the key each
# call is what makes a .env edit (or a test's monkeypatch) take effect
# without restarting the process, and a client built against a stale key
# would silently outlive it.
_client: genai.Client | None = None
_client_key: str | None = None


class CloudError(RuntimeError):
    """Any reason the cloud model did not return usable text: no API key,
    network failure, quota exhausted, safety block, empty response."""


def cloud_available() -> bool:
    """True if a cloud call could even be attempted. Callers use this to
    skip the attempt entirely (and say so honestly) rather than paying a
    timeout to discover there is no key."""
    return bool(os.environ.get("CLOUD_LLM_API_KEY"))


def _get_client() -> genai.Client:
    global _client, _client_key
    api_key = os.environ.get("CLOUD_LLM_API_KEY")
    if not api_key:
        raise CloudError("CLOUD_LLM_API_KEY is not set -- cannot reach Gemini. "
                         "Get a free key at https://aistudio.google.com")
    if _client is None or _client_key != api_key:
        _client = genai.Client(api_key=api_key)
        _client_key = api_key
    return _client


def call_cloud_model(prompt: str, temperature: float = 0.2) -> str:
    """Sends one prompt to Gemini, returns its plain-text response.

    Raises CloudError (a RuntimeError) and never anything else, so a
    dead network, an exhausted free-tier quota or a safety block all
    reach the caller as the same single thing to fall back from. This
    function does not swallow failures itself -- silently returning the
    local model's work as if the cloud had produced it would hide a
    broken key for the whole demo.
    """
    client = _get_client()
    model = os.environ.get("CLOUD_LLM_MODEL", DEFAULT_CLOUD_LLM_MODEL)
    try:
        response = client.models.generate_content(
            model=model,
            contents=prompt,
            config=types.GenerateContentConfig(temperature=temperature),
        )
        text = (response.text or "").strip()
    except CloudError:
        raise
    except Exception as exc:  # noqa: BLE001 -- see below
        # Deliberately broad. The SDK raises APIError for quota/auth, but
        # transport failures surface as httpx errors and a safety-blocked
        # response raises on attribute access, none of which share a base
        # class we control. NFR-5 says the demo must survive all of them
        # identically, so they all become one CloudError here.
        raise CloudError(f"Gemini call failed ({model}): {exc}") from exc

    if not text:
        # Usually a safety filter: fraud accusations naming companies can
        # trip it. An empty string would read as a successful call, so
        # make it fall back like any other failure.
        raise CloudError(f"Gemini returned an empty response ({model}); "
                         "the prompt may have been filtered")
    return text
