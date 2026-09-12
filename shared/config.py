"""
Runtime configuration for the local-model hop (Ollama).

The agent does not have to run the model on the same machine as the API:
during the hackathon one laptop serves `qwen2.5:7b` over the LAN and
everybody else points at it. Nothing about that server is hardcoded --
the URL and model name are read from here, in this order (first win):

  1. Environment variables (incl. anything in a `.env` file next to the
     repo root) -- this is how a `.env` handed around the team wins over
     whatever a laptop has saved locally.
  2. The user config file, `~/.forensic_auditor/config.json`, which the
     UI's "Modelo local" panel writes when you pick a server/model.
  3. The defaults in this module.

See `docs/ollama-red-local.md` for how to expose the server side.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import load_dotenv
from pydantic import BaseModel, Field, field_validator

# `.env` never clobbers a variable that is already exported -- a real
# environment variable stays the highest-priority source.
load_dotenv(override=False)

DEFAULT_URL = "http://localhost:11434"
DEFAULT_MODEL = "qwen2.5:7b"
DEFAULT_KEEP_ALIVE = "30m"
DEFAULT_NUM_CTX = 8192
DEFAULT_TIMEOUT = 300.0
DEFAULT_TEMPERATURE = 0.7

OLLAMA_DEFAULT_PORT = 11434

# Endpoint paths we accept (and strip) from a pasted URL. The server-side
# setup doc tells the team to share `http://<ip>:11434/api/generate`, so
# that exact string has to normalize down to the base URL.
_API_SUFFIXES = (
    "/api/generate",
    "/api/chat",
    "/api/tags",
    "/api/show",
    "/api/embeddings",
    "/api/embed",
    "/api/ps",
    "/v1/chat/completions",
    "/v1",
)

CONFIG_ENV_VAR = "FORENSIC_AUDITOR_CONFIG"
DEFAULT_CONFIG_DIR = Path.home() / ".forensic_auditor"


def normalize_ollama_url(raw: str | None) -> str:
    """Turns whatever the user typed into a clean base URL.

    Accepts `192.168.1.50`, `192.168.1.50:11434`, `localhost:11434`,
    `http://host:11434/api/generate`, `HTTP://Host:11434/` ... and always
    returns `scheme://host:port` with no trailing slash and no API path.
    """
    text = (raw or "").strip().strip('"').strip("'")
    if not text:
        return DEFAULT_URL

    if "//" not in text:
        text = f"http://{text}"

    parts = urlsplit(text)
    scheme = (parts.scheme or "http").lower()
    host = (parts.hostname or "").lower()
    if not host:
        return DEFAULT_URL

    path = parts.path.rstrip("/")
    lowered = path.lower()
    for suffix in _API_SUFFIXES:
        if lowered.endswith(suffix):
            path = path[: -len(suffix)]
            break
    path = path.rstrip("/")

    port = parts.port
    if port is None:
        # No explicit port: assume Ollama's, unless this is plainly a
        # reverse-proxied https URL where 443 is meant.
        port = 443 if scheme == "https" else OLLAMA_DEFAULT_PORT

    netloc = f"[{host}]:{port}" if ":" in host else f"{host}:{port}"
    return f"{scheme}://{netloc}{path}"


class OllamaSettings(BaseModel):
    """Everything the client needs to reach one Ollama server."""

    url: str = DEFAULT_URL
    model: str = DEFAULT_MODEL
    keep_alive: str = DEFAULT_KEEP_ALIVE
    num_ctx: int = Field(default=DEFAULT_NUM_CTX, ge=256)
    timeout: float = Field(default=DEFAULT_TIMEOUT, gt=0)
    temperature: float = Field(default=DEFAULT_TEMPERATURE, ge=0, le=2)

    @field_validator("url", mode="before")
    @classmethod
    def _normalize_url(cls, value: object) -> str:
        return normalize_ollama_url(value if isinstance(value, str) else None)

    @field_validator("model", mode="before")
    @classmethod
    def _clean_model(cls, value: object) -> str:
        return value.strip() if isinstance(value, str) else DEFAULT_MODEL

    def endpoint(self, path: str) -> str:
        return f"{self.url}/{path.lstrip('/')}"


def config_path() -> Path:
    """Where the user's saved settings live (override with
    FORENSIC_AUDITOR_CONFIG, which the tests use to stay off $HOME)."""
    override = os.environ.get(CONFIG_ENV_VAR)
    if override:
        return Path(override)
    return DEFAULT_CONFIG_DIR / "config.json"


def _from_file() -> dict:
    path = config_path()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        # Missing or corrupt config is not an error: fall back to
        # defaults. A hackathon laptop should always start.
        return {}
    if not isinstance(raw, dict):
        return {}
    section = raw.get("ollama", raw)
    return section if isinstance(section, dict) else {}


_ENV_KEYS = {
    "url": ("OLLAMA_URL", str),
    "model": ("OLLAMA_MODEL", str),
    "keep_alive": ("OLLAMA_KEEP_ALIVE", str),
    "num_ctx": ("OLLAMA_NUM_CTX", int),
    "timeout": ("OLLAMA_TIMEOUT", float),
    "temperature": ("OLLAMA_TEMPERATURE", float),
}


#: Public list of the variables that can override the config file.
ENV_VARS: tuple[str, ...] = tuple(env_var for env_var, _ in _ENV_KEYS.values())


def _from_env() -> dict:
    values: dict = {}
    for field, (env_var, cast) in _ENV_KEYS.items():
        raw = os.environ.get(env_var)
        if raw is None or raw.strip() == "":
            continue
        try:
            values[field] = cast(raw.strip())
        except ValueError:
            # A typo'd OLLAMA_NUM_CTX shouldn't take the app down.
            continue
    return values


_cache: OllamaSettings | None = None


def load_settings(refresh: bool = False) -> OllamaSettings:
    """Merged settings: defaults <- config file <- environment."""
    global _cache
    if _cache is not None and not refresh:
        return _cache
    merged = {**_from_file(), **_from_env()}
    try:
        _cache = OllamaSettings(**merged)
    except Exception:  # noqa: BLE001 -- bad saved values must not be fatal
        _cache = OllamaSettings()
    return _cache


def save_settings(settings: OllamaSettings) -> Path:
    """Persists settings to the user config file and refreshes the cache.

    Note for the UI: an `OLLAMA_URL`/`OLLAMA_MODEL` in the environment
    still outranks the file, so `load_settings()` can legitimately come
    back different from what was just saved. `env_overrides()` says which
    keys are pinned that way.
    """
    global _cache
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    existing: dict = {}
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            existing = loaded
    except (OSError, ValueError):
        existing = {}
    existing["ollama"] = settings.model_dump()
    path.write_text(json.dumps(existing, indent=2) + "\n", encoding="utf-8")
    _cache = None
    return path


def env_overrides() -> dict[str, str]:
    """Settings currently pinned by the environment, for the UI to show
    as read-only instead of silently ignoring a user's edit."""
    return {field: os.environ[env_var] for field, (env_var, _) in _ENV_KEYS.items()
            if os.environ.get(env_var, "").strip()}


def reset_cache() -> None:
    global _cache
    _cache = None
