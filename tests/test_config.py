import json

import pytest

from shared import config
from shared.config import OllamaSettings, load_settings, normalize_ollama_url, save_settings


@pytest.mark.parametrize("raw,expected", [
    ("192.168.1.50", "http://192.168.1.50:11434"),
    ("192.168.1.50:11434", "http://192.168.1.50:11434"),
    ("localhost:11434", "http://localhost:11434"),
    ("localhost", "http://localhost:11434"),
    # What the server-side setup doc tells the team to hand around:
    ("http://192.168.1.50:11434/api/generate", "http://192.168.1.50:11434"),
    ("http://192.168.1.50:11434/api/chat", "http://192.168.1.50:11434"),
    ("http://192.168.1.50:11434/", "http://192.168.1.50:11434"),
    ("  HTTP://192.168.1.50:11434///  ", "http://192.168.1.50:11434"),
    ('"192.168.1.50:11434"', "http://192.168.1.50:11434"),
    ("mi-laptop.local", "http://mi-laptop.local:11434"),
    ("http://10.0.0.4:8080", "http://10.0.0.4:8080"),  # non-default port kept
    ("https://ollama.example.com", "https://ollama.example.com:443"),
    ("", "http://localhost:11434"),
    (None, "http://localhost:11434"),
])
def test_normalize_ollama_url(raw, expected):
    assert normalize_ollama_url(raw) == expected


def test_settings_normalize_url_and_build_endpoint():
    settings = OllamaSettings(url="192.168.1.50/api/generate", model="  qwen2.5:7b  ")
    assert settings.url == "http://192.168.1.50:11434"
    assert settings.model == "qwen2.5:7b"
    assert settings.endpoint("/api/chat") == "http://192.168.1.50:11434/api/chat"


def test_defaults_when_nothing_is_configured():
    settings = load_settings(refresh=True)
    assert settings.url == "http://localhost:11434"
    assert settings.keep_alive == "30m"
    assert settings.num_ctx == 8192
    assert settings.timeout == 300
    assert settings.temperature == 0.7


def test_config_file_is_read_and_env_wins_over_it(monkeypatch):
    path = config.config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"ollama": {"url": "192.168.1.77", "model": "from-file",
                                           "num_ctx": 4096}}), encoding="utf-8")

    settings = load_settings(refresh=True)
    assert settings.url == "http://192.168.1.77:11434"
    assert settings.model == "from-file"
    assert settings.num_ctx == 4096

    monkeypatch.setenv("OLLAMA_URL", "http://192.168.1.99:11434/api/generate")
    settings = load_settings(refresh=True)
    assert settings.url == "http://192.168.1.99:11434"   # env overrides the file
    assert settings.model == "from-file"                 # untouched keys still come from it


def test_corrupt_config_file_falls_back_to_defaults():
    path = config.config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{ not json at all", encoding="utf-8")
    assert load_settings(refresh=True).url == "http://localhost:11434"


def test_unparsable_env_value_is_ignored(monkeypatch):
    monkeypatch.setenv("OLLAMA_NUM_CTX", "ocho mil")
    assert load_settings(refresh=True).num_ctx == 8192


def test_save_settings_roundtrip_and_cache_refresh():
    save_settings(OllamaSettings(url="192.168.1.50", model="qwen2.5:7b", timeout=600))
    reloaded = load_settings()
    assert reloaded.url == "http://192.168.1.50:11434"
    assert reloaded.model == "qwen2.5:7b"
    assert reloaded.timeout == 600

    on_disk = json.loads(config.config_path().read_text(encoding="utf-8"))
    assert on_disk["ollama"]["url"] == "http://192.168.1.50:11434"


def test_save_settings_preserves_unrelated_keys():
    path = config.config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"something_else": {"keep": True}}), encoding="utf-8")
    save_settings(OllamaSettings(url="192.168.1.50"))
    on_disk = json.loads(path.read_text(encoding="utf-8"))
    assert on_disk["something_else"] == {"keep": True}


def test_env_overrides_reports_pinned_keys(monkeypatch):
    assert config.env_overrides() == {}
    monkeypatch.setenv("OLLAMA_MODEL", "llama3.1:8b")
    assert config.env_overrides() == {"model": "llama3.1:8b"}
