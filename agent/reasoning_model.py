"""
The model behind every ReAct step (FR-15), chosen with AGENT_LLM.

  ollama   (default) the team's LAN Ollama, agent/ollama_client.py.
  cortex   Snowflake Cortex's REST inference endpoint, with the same
           SNOWFLAKE_ACCOUNT / SNOWFLAKE_PAT that DATA_SOURCE=snowflake
           uses. No laptop on the wifi in the path. Measured on the
           team's trial account with the real system prompt: 2.5s per
           step, against 20-45s for qwen2.5:7b served over the LAN.

Only llama3.1-70b answered on that account (GCP us-central1): llama3.3
needs cross-region inference, and mistral-large2, llama4-maverick,
llama3.1-405b and deepseek-r1 are deprecated or legacy. CORTEX_MODEL
overrides it.

Either way the caller gets an ollama_client.ChatResult back and an
OllamaError on failure -- that class already means "the model hop
failed, phrased for a human" -- so agent/loop.py and agent/qa.py keep a
single code path for both.
"""
from __future__ import annotations

import os

from agent import ollama_client
from agent.ollama_client import ChatResult, NoticeCallback, OllamaError
from data.snowflake_client import (
    DEFAULT_CORTEX_MODEL,
    SnowflakeError,
    cortex_complete,
    snowflake_available,
)
from shared.config import load_settings

#: A ReAct step or a final case file fits well inside this; it bounds
#: what a runaway answer costs in trial credits.
CORTEX_MAX_TOKENS = 2048

#: Steps come back in seconds. A request hung past this should end the
#: run with a message, not hold the judge's progress bar.
CORTEX_TIMEOUT_SECONDS = 90


def provider() -> str:
    """"cortex" or "ollama". Anything unrecognised means ollama, the
    behaviour this project had before the flag existed."""
    return "cortex" if os.environ.get("AGENT_LLM", "").strip().lower() == "cortex" else "ollama"


def model_name() -> str:
    if provider() == "cortex":
        return os.environ.get("CORTEX_MODEL", "").strip() or DEFAULT_CORTEX_MODEL
    return load_settings().model


def label() -> str:
    """How the live log names the model when it fails."""
    return f"Snowflake Cortex ({model_name()})" if provider() == "cortex" else "local model"


def complete_result(prompt: str, *, temperature: float | None = None,
                    on_notice: NoticeCallback | None = None) -> ChatResult:
    if provider() == "ollama":
        return ollama_client.complete_result(prompt, temperature=temperature,
                                             on_notice=on_notice)
    return _complete_with_cortex(prompt, temperature)


def _complete_with_cortex(prompt: str, temperature: float | None) -> ChatResult:
    model = model_name()
    if not snowflake_available():
        raise OllamaError(
            "AGENT_LLM=cortex, pero faltan SNOWFLAKE_ACCOUNT / SNOWFLAKE_PAT en .env. "
            "Ponlos, o vuelve a AGENT_LLM=ollama.", kind="protocol")

    # Same contract as ollama_client.chat: a Detener from the previous
    # run must not cancel this one before it starts.
    ollama_client.CANCEL.clear()
    try:
        content = cortex_complete(prompt, model=model, temperature=temperature,
                                  max_tokens=CORTEX_MAX_TOKENS,
                                  timeout=CORTEX_TIMEOUT_SECONDS)
    except SnowflakeError as exc:
        raise OllamaError(f"Snowflake Cortex no respondió con {model}: {exc}",
                          kind="connection") from exc

    # Not streamed, so there is nothing to interrupt mid-answer: Detener
    # takes effect when this step comes back, seconds later, rather than
    # between tokens as it does with Ollama.
    return ChatResult(content=content, model=model, via="cortex",
                      cancelled=ollama_client.CANCEL.is_set())
