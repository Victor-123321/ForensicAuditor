"""
Cloud-model client (FR-15, NFR-4), backed by Google Gemini. The SRS
reserves cloud calls for two things per investigation -- final case-file
narrative synthesis (agent/loop.py) and the live Q&A follow-up (FR-19,
api/main.py's /ask endpoint) -- capping total cloud usage at <=2
calls/investigation. Every intermediate ReAct step stays on the local
model (agent/loop.py::_call_reasoning_model), so this module is never on the
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
import time

from google import genai
from google.genai import errors as genai_errors
from google.genai import types

import shared.config  # noqa: F401 -- imported for its load_dotenv() side effect

DEFAULT_CLOUD_LLM_MODEL = "gemini-flash-latest"

#: Statuses worth trying again. 503 is the one that actually bit us:
#: during the hackathon everyone hammers the free tier, and Gemini
#: answers "This model is currently experiencing high demand". Measured
#: on 2026-09-12: 1 call in 5 got a 503, and the immediate retry
#: succeeded in 0.9s. Without a retry that spike silently costs the run
#: its polished narrative.
RETRYABLE_STATUS = frozenset({408, 429, 500, 502, 503, 504})

#: Backoff between attempts; len() + 1 is the attempt count. Kept short
#: and bounded on purpose -- a judge is watching a progress bar, so
#: failing over to the local model fast beats retrying for a minute.
RETRY_BACKOFF_SECONDS = (1.0, 2.0)

#: Tried in order when the configured model stays unavailable after its
#: retries. Measured on 2026-09-12 with a free-tier key, 3 calls each:
#:   gemini-flash-latest       2/3 ok, 0.8s   <- the default, 503s under load
#:   gemini-3.5-flash          3/3 ok, 1.0s
#:   gemini-flash-lite-latest  3/3 ok, 0.6s
#: (gemini-pro-latest was 0/3, 429 -- the free tier does not cover Pro,
#: so it is deliberately not in this chain.)
#: Staying on SOME Gemini model matters beyond uptime: falling through to
#: the local model means the run has no generative-AI cloud call in it at
#: all, which is the thing the Gen AI prize is actually judged on.
FALLBACK_MODELS = ("gemini-3.5-flash", "gemini-flash-lite-latest")

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


def _is_retryable(exc: Exception) -> bool:
    """A 503 spike is worth another shot; a bad API key never is.
    Retrying a permanent failure just delays the fallback the demo
    depends on."""
    if isinstance(exc, genai_errors.APIError):
        return getattr(exc, "code", None) in RETRYABLE_STATUS
    # Not an API error at all -- a transport/DNS/timeout failure. Those
    # are the classic transient ones.
    return True


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
    primary = os.environ.get("CLOUD_LLM_MODEL", DEFAULT_CLOUD_LLM_MODEL)
    config = types.GenerateContentConfig(
        temperature=temperature,
        # We never hand Gemini any tools -- all tool use is the local
        # model's job (agent/tools.py). Leaving automatic function
        # calling on makes the SDK print a warning on every call, which
        # would be scrolling past during the demo for a code path we
        # never take.
        automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
    )

    # The configured model first, then the spares -- never a spare that
    # merely repeats the primary.
    models = [primary] + [m for m in FALLBACK_MODELS if m != primary]
    last_exc: Exception | None = None

    for model in models:
        for attempt in range(len(RETRY_BACKOFF_SECONDS) + 1):
            try:
                response = client.models.generate_content(
                    model=model, contents=prompt, config=config)
                text = (response.text or "").strip()
            except Exception as exc:  # noqa: BLE001 -- deliberately broad
                # The SDK raises APIError for quota/auth, but transport
                # failures surface as httpx errors and a safety-blocked
                # response raises on attribute access; none share a base
                # class we control. NFR-5 says the demo survives all of
                # them identically, so they all become one CloudError.
                last_exc = exc
                if not _is_retryable(exc):
                    # Permanent for this model: a bad key fails the same
                    # way everywhere, but a 404 only means THIS model id
                    # is not available to this key, so the spares are
                    # still worth a shot.
                    break
                if attempt < len(RETRY_BACKOFF_SECONDS):
                    time.sleep(RETRY_BACKOFF_SECONDS[attempt])
                continue

            if text:
                return text

            # Empty means a safety filter, not a hiccup: fraud
            # accusations naming real companies can trip it, and the
            # identical prompt would trip it again on any model. Fall
            # back to the local narrative instead of burning the judge's
            # time. An empty string would read as a successful call.
            raise CloudError(f"Gemini returned an empty response ({model}); "
                             "the prompt may have been filtered")

    raise CloudError(f"Gemini call failed on {', '.join(models)}: {last_exc}")
