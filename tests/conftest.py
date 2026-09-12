"""
Shared fixtures.

The Ollama tests never touch a network or a real `~/.forensic_auditor`:
`requests` is monkeypatched per-test and this fixture keeps the module
level caches (settings, vision capabilities, cloud fallback) from leaking
between tests.
"""
from __future__ import annotations

import pytest

from agent import ollama_client
from shared import config


@pytest.fixture(autouse=True)
def isolated_config(tmp_path, monkeypatch):
    """Points the user config file at a temp dir and clears every
    OLLAMA_* variable, so a developer's own .env can't change results."""
    monkeypatch.setenv(config.CONFIG_ENV_VAR, str(tmp_path / "config.json"))
    for env_var in config.ENV_VARS:
        monkeypatch.delenv(env_var, raising=False)
    config.reset_cache()
    ollama_client.clear_vision_cache()
    ollama_client.register_cloud_fallback(None)
    ollama_client.clear_cancel()
    yield
    config.reset_cache()
    ollama_client.clear_vision_cache()
    ollama_client.register_cloud_fallback(None)
    ollama_client.clear_cancel()
