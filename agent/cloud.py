"""
Cloud-model client (FR-15, NFR-4). The SRS reserves cloud calls for two
things per investigation -- final case-file narrative synthesis
(agent/loop.py) and the live Q&A follow-up (FR-19, api/main.py's /ask
endpoint) -- capping total cloud usage at <=2 calls/investigation.
Every intermediate ReAct step stays on the local model
(agent/loop.py::_call_local_model), so this module is never on the
loop's hot path.

Uses OpenRouter's OpenAI-compatible chat completions endpoint so the
team can point CLOUD_LLM_MODEL at any of OpenRouter's free-tier models
(e.g. "meta-llama/llama-3.3-70b-instruct:free") without touching code.

Diego: import `call_cloud_model` directly from here for the /ask
endpoint (FR-19) -- no need to touch agent/loop.py or agent/prompts.py
for that, both are Angel's.
"""
from __future__ import annotations

import os

import requests

import shared.config  # noqa: F401 -- imported for its load_dotenv() side effect

DEFAULT_CLOUD_LLM_URL = "https://openrouter.ai/api/v1/chat/completions"
DEFAULT_CLOUD_LLM_MODEL = "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free"

# Read at CALL time, not import time: shared.config is what runs
# load_dotenv(), so reading these at import made the key visible only
# when something happened to import shared.config first. That worked by
# accident of api/main.py's import order and broke for anyone importing
# agent.loop directly.
def _cloud_env() -> tuple[str, str | None, str]:
    return (
        os.environ.get("CLOUD_LLM_URL", DEFAULT_CLOUD_LLM_URL),
        os.environ.get("CLOUD_LLM_API_KEY"),
        os.environ.get("CLOUD_LLM_MODEL", DEFAULT_CLOUD_LLM_MODEL),
    )


def call_cloud_model(prompt: str, temperature: float = 0.2) -> str:
    """Sends one prompt to the cloud model, returns its plain-text
    response. Raises RuntimeError if CLOUD_LLM_API_KEY isn't set, and
    lets requests.RequestException (network) / requests.HTTPError (bad
    response) propagate otherwise -- callers implement NFR-5's
    local-only fallback around this, this function does not swallow
    failures itself."""
    url, api_key, model = _cloud_env()
    if not api_key:
        raise RuntimeError("CLOUD_LLM_API_KEY is not set -- cannot reach the cloud model")

    resp = requests.post(
        url,
        headers={"Authorization": f"Bearer {api_key}"},
        json={
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": temperature,
        },
        timeout=60,
    )
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"]
