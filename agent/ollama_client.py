"""
Client for an Ollama server that usually is NOT this machine (FR-15).

During the hackathon one laptop runs `ollama serve` bound to 0.0.0.0 and
the rest of the team points `OLLAMA_URL` at its LAN address, so every
call here crosses a wifi network that can drop, sleep or block a port.
That shapes the whole module:

* Nothing is hardcoded -- server, model and knobs come from
  `shared.config` (env / user config file / defaults, in that order).
* A cheap `list_models()` probe (6s) answers "is anybody home?" without
  paying the long chat timeout, and never raises.
* The chat timeout is deliberately long (minutes): loading a 7B model
  cold is slow, and `keep_alive` keeps it resident between messages.
* Every failure is translated into a sentence a teammate can act on --
  never a bare stack trace, never a silent fallback.

Server-side setup (OLLAMA_HOST, firewall, sleep):
`docs/ollama-red-local.md`.
"""
from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Iterator, Sequence

import requests

from shared.config import OllamaSettings, load_settings, normalize_ollama_url

# Probing is a "yes/no" question -- fail fast so the UI stays responsive.
PROBE_TIMEOUT = 6.0
# Reaching the host is fast or never; only the *reading* deserves patience.
CONNECT_TIMEOUT = 10.0

_VISION_TTL = 300.0          # a model's capabilities don't change often
_VISION_UNKNOWN_TTL = 30.0   # but don't cache "couldn't tell" for long

TokenCallback = Callable[[str], None]
NoticeCallback = Callable[[str], None]
CloudFallback = Callable[[list[dict]], str]


class OllamaError(RuntimeError):
    """A failure on the model hop, already phrased for a human.

    `kind` is one of: connection | timeout | http | protocol |
    vision_unsupported. `partial` carries whatever text had already been
    streamed when the connection died, so a caller can salvage it.
    """

    def __init__(self, message: str, *, kind: str = "unknown",
                 url: str | None = None, partial: str = "") -> None:
        super().__init__(message)
        self.kind = kind
        self.url = url
        self.partial = partial


# ---------------------------------------------------------------------------
# Cancellation
# ---------------------------------------------------------------------------

class CancelToken:
    """Cooperative cancel flag, checked once per streamed line.

    A `threading.Event` rather than a bool because the API streams an
    investigation from a background thread (api/main.py) while the cancel
    arrives on the request thread.
    """

    def __init__(self) -> None:
        self._event = threading.Event()

    def cancel(self) -> None:
        self._event.set()

    set = cancel  # alias: reads better at some call sites

    def clear(self) -> None:
        self._event.clear()

    def is_set(self) -> bool:
        return self._event.is_set()


#: Process-wide token used when a caller doesn't pass its own.
CANCEL = CancelToken()


def request_cancel() -> None:
    """Stops the in-flight generation on the default token."""
    CANCEL.cancel()


def clear_cancel() -> None:
    CANCEL.clear()


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------

@dataclass
class ProbeResult:
    """Answer to "is there an Ollama at this URL, and what does it have?"

    Unpacks as `ok, models, message` to match how it's usually read.
    """

    ok: bool
    models: list[str] = field(default_factory=list)
    message: str = ""

    def __iter__(self) -> Iterator:
        return iter((self.ok, self.models, self.message))


@dataclass
class ChatMetrics:
    prompt_eval_count: int = 0
    eval_count: int = 0
    eval_duration_ns: int = 0
    total_duration_ns: int = 0

    @property
    def tokens_per_second(self) -> float:
        if self.eval_duration_ns <= 0:
            return 0.0
        return self.eval_count / (self.eval_duration_ns / 1e9)


@dataclass
class ChatResult:
    content: str
    model: str
    metrics: ChatMetrics = field(default_factory=ChatMetrics)
    cancelled: bool = False
    via: str = "ollama"  # "ollama" | "cloud"
    notices: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Probe: /api/tags and /api/show
# ---------------------------------------------------------------------------

def list_models(url: str | None = None, timeout: float = PROBE_TIMEOUT) -> ProbeResult:
    """GET {url}/api/tags. Never raises -- the failure *is* the answer."""
    base = normalize_ollama_url(url) if url is not None else load_settings().url
    try:
        resp = requests.get(f"{base}/api/tags", timeout=timeout)
    # Order matters and is not obvious: ConnectTimeout inherits from BOTH
    # ConnectionError and Timeout, so catching Timeout first would label a
    # host that never answers as "slow" -- the opposite diagnosis, and the
    # most common case on campus wifi. Most specific first.
    except requests.exceptions.ConnectTimeout:
        return ProbeResult(False, [], f"{base} no contestó al intentar conectar "
                                      f"({timeout:g}s). El equipo está apagado o suspendido, "
                                      "no está en esta red, o su firewall descarta el 11434 "
                                      "en silencio.")
    except requests.exceptions.ReadTimeout:
        return ProbeResult(False, [], f"{base} aceptó la conexión pero no respondió en "
                                      f"{timeout:g}s. Está encendido pero tarda demasiado: "
                                      "revisa si está saturado.")
    except requests.exceptions.ConnectionError:
        return ProbeResult(False, [], f"{base} rechazó la conexión: hay un equipo ahí, pero "
                                      "nada escuchando en ese puerto. ¿Está corriendo "
                                      "`ollama serve` con OLLAMA_HOST=0.0.0.0, y el 11434 "
                                      "abierto en su firewall?")
    except requests.exceptions.Timeout:
        return ProbeResult(False, [], f"{base} no respondió en {timeout:g}s.")
    except requests.RequestException as exc:
        return ProbeResult(False, [], f"No pude sondear {base}: {exc}")

    if resp.status_code >= 400:
        return ProbeResult(False, [], f"{base} respondió HTTP {resp.status_code}. "
                                      "¿Seguro que es un servidor Ollama y no otra cosa en ese puerto?")
    try:
        payload = resp.json()
    except ValueError:
        return ProbeResult(False, [], f"{base} respondió algo que no es JSON. "
                                      "Probablemente hay otro servicio en ese puerto.")

    models = [m.get("name", "") for m in payload.get("models", []) if m.get("name")]
    if not models:
        return ProbeResult(True, [], f"Conecté con {base}, pero no tiene ningún modelo "
                                     "descargado. Corre `ollama pull qwen2.5:7b` en ese equipo.")
    return ProbeResult(True, sorted(models),
                       f"Conectado a {base} - {len(models)} modelo(s) disponible(s).")


_vision_cache: dict[tuple[str, str], tuple[float, bool | None]] = {}
_vision_lock = threading.Lock()


def supports_vision(url: str | None = None, model: str | None = None,
                    timeout: float = PROBE_TIMEOUT) -> bool | None:
    """True/False if the model takes images, None if we couldn't tell.

    Asking beforehand is cheaper than discovering it as an HTTP 400 in
    the middle of a demo. Cached for a few minutes per (server, model).
    """
    settings = load_settings()
    base = normalize_ollama_url(url) if url is not None else settings.url
    name = (model or settings.model).strip()
    if not name:
        return None

    key = (base, name)
    now = time.monotonic()
    with _vision_lock:
        hit = _vision_cache.get(key)
        if hit and now < hit[0]:
            return hit[1]

    answer: bool | None
    try:
        resp = requests.post(f"{base}/api/show", json={"model": name}, timeout=timeout)
        if resp.status_code >= 400:
            answer = None
        else:
            info = resp.json()
            caps = info.get("capabilities")
            if isinstance(caps, list):
                answer = "vision" in [str(c).lower() for c in caps]
            else:
                # Older Ollama has no `capabilities`: multimodal models
                # carry a clip/vision projector in their families instead.
                details = info.get("details") or {}
                families = [str(f).lower() for f in (details.get("families") or [])]
                family = str(details.get("family", "")).lower()
                blob = " ".join(families + [family])
                if "clip" in blob or "vision" in blob:
                    answer = True
                elif families or family:
                    answer = False  # families listed, none of them multimodal
                else:
                    answer = None
    except (requests.RequestException, ValueError):
        answer = None

    ttl = _VISION_TTL if answer is not None else _VISION_UNKNOWN_TTL
    with _vision_lock:
        _vision_cache[key] = (now + ttl, answer)
    return answer


def clear_vision_cache() -> None:
    with _vision_lock:
        _vision_cache.clear()


# ---------------------------------------------------------------------------
# Cloud fallback (FR-15 reserves cloud calls; this is the emergency one)
# ---------------------------------------------------------------------------

_cloud_fallback: CloudFallback | None = None


def register_cloud_fallback(fn: CloudFallback | None) -> None:
    """Registers `fn(messages) -> str`, used ONLY when the LAN server is
    unreachable, once per call, and always announced through `on_notice`."""
    global _cloud_fallback
    _cloud_fallback = fn


def has_cloud_fallback() -> bool:
    return _cloud_fallback is not None


# ---------------------------------------------------------------------------
# Chat
# ---------------------------------------------------------------------------

def chat(
    messages: Sequence[dict],
    *,
    settings: OllamaSettings | None = None,
    model: str | None = None,
    temperature: float | None = None,
    images: Sequence[str] | None = None,
    on_token: TokenCallback | None = None,
    on_notice: NoticeCallback | None = None,
    cancel: CancelToken | None = None,
    allow_fallback: bool = True,
) -> ChatResult:
    """Streams a chat completion from the configured Ollama server.

    `on_token` is called with each fragment as it arrives (that's what
    makes the UI feel alive); `images` is a list of base64 strings
    attached to the last user message, and is dropped with a notice if
    the model has no vision. Raises `OllamaError` with a message meant
    for a human when the hop fails and no cloud fallback is registered.
    """
    cfg = settings or load_settings()
    name = (model or cfg.model).strip()
    # `is not None`, not `or`: a caller's own token is almost always in
    # the "not cancelled" state, and must still be the one we watch.
    token = cancel if cancel is not None else CANCEL
    # Reset before every send: a cancel from the previous generation must
    # not kill this one before it starts.
    token.clear()

    notices: list[str] = []

    def notify(text: str) -> None:
        notices.append(text)
        if on_notice:
            on_notice(text)

    if not name:
        raise OllamaError(
            "No hay modelo configurado. Pon OLLAMA_MODEL en tu .env (p. ej. "
            "qwen2.5:7b) o elígelo con el botón 'Buscar modelos'.",
            kind="protocol", url=cfg.url)

    payload_messages = [dict(m) for m in messages]
    if images:
        vision = supports_vision(cfg.url, name)
        if vision is False:
            notify(f"El modelo '{name}' no acepta imágenes; envío solo el texto.")
            images = None
        elif vision is None:
            notify(f"No pude confirmar si '{name}' tiene visión; lo intento con imágenes.")
    if images:
        for msg in reversed(payload_messages):
            if msg.get("role") == "user":
                msg["images"] = list(images)
                break

    try:
        return _stream_chat(cfg, name, payload_messages, temperature, on_token, token, notices)
    except OllamaError as exc:
        if exc.kind == "vision_unsupported" and images:
            # The /api/show probe was wrong (or unavailable): degrade to
            # text rather than losing the turn.
            notify(f"'{name}' rechazó las imágenes (HTTP 400); reenvío solo el texto.")
            for msg in payload_messages:
                msg.pop("images", None)
            return _stream_chat(cfg, name, payload_messages, temperature, on_token, token, notices)

        if exc.kind in ("connection", "timeout") and allow_fallback and _cloud_fallback is not None:
            notify(f"{exc} - Tiro una sola vez del modelo en la nube para no dejarte colgado.")
            try:
                answer = _cloud_fallback(payload_messages)
            except Exception as cloud_exc:  # noqa: BLE001
                raise OllamaError(
                    f"{exc} Además, el respaldo en la nube también falló: {cloud_exc}",
                    kind=exc.kind, url=cfg.url, partial=exc.partial) from cloud_exc
            if on_token and answer:
                on_token(answer)
            return ChatResult(content=answer, model="cloud-fallback", via="cloud",
                              notices=notices)
        raise


def _stream_chat(cfg: OllamaSettings, model: str, messages: list[dict],
                 temperature: float | None, on_token: TokenCallback | None,
                 token: CancelToken, notices: list[str]) -> ChatResult:
    url = cfg.endpoint("/api/chat")
    payload = {
        "model": model,
        "messages": messages,
        "stream": True,
        # Long keep_alive so the server doesn't evict the model between
        # our messages and pay the cold start again.
        "keep_alive": cfg.keep_alive,
        "options": {
            "num_ctx": cfg.num_ctx,
            "temperature": cfg.temperature if temperature is None else temperature,
        },
    }
    has_images = any("images" in m for m in messages)
    chunks: list[str] = []
    metrics = ChatMetrics()
    cancelled = False

    try:
        # (connect, read): give up fast on an unreachable host, but wait
        # out a cold model load between bytes.
        with requests.post(url, json=payload, stream=True,
                           timeout=(CONNECT_TIMEOUT, cfg.timeout)) as resp:
            if resp.status_code >= 400:
                raise _http_error(resp, cfg, model, has_images)

            for line in resp.iter_lines():
                if token.is_set():
                    cancelled = True
                    break
                if not line:
                    continue
                try:
                    chunk = json.loads(line)
                except ValueError:
                    continue  # keep-alive noise / partial frame
                if chunk.get("error"):
                    raise OllamaError(str(chunk["error"]), kind="http", url=cfg.url,
                                      partial="".join(chunks))
                piece = (chunk.get("message") or {}).get("content", "")
                if piece:
                    chunks.append(piece)
                    if on_token:
                        on_token(piece)
                if chunk.get("done"):
                    metrics = ChatMetrics(
                        prompt_eval_count=int(chunk.get("prompt_eval_count", 0) or 0),
                        eval_count=int(chunk.get("eval_count", 0) or 0),
                        eval_duration_ns=int(chunk.get("eval_duration", 0) or 0),
                        total_duration_ns=int(chunk.get("total_duration", 0) or 0),
                    )
                    break
    except requests.exceptions.ReadTimeout as exc:
        raise OllamaError(
            f"El modelo '{model}' no respondió en {cfg.timeout:g}s. Si es un modelo "
            "grande arrancando en frío puede tardar varios minutos: sube `timeout` "
            "(OLLAMA_TIMEOUT) o mantenlo caliente con un keep_alive más largo.",
            kind="timeout", url=cfg.url, partial="".join(chunks)) from exc
    except requests.exceptions.ConnectionError as exc:
        raise OllamaError(
            f"No pude conectar con {cfg.url}. Revisa que Ollama esté escuchando en "
            "0.0.0.0 en ese equipo, que el 11434/tcp esté abierto en su firewall y que "
            "ambas máquinas sigan en la misma red.",
            kind="connection", url=cfg.url, partial="".join(chunks)) from exc
    except requests.exceptions.Timeout as exc:
        raise OllamaError(
            f"{cfg.url} no respondió a tiempo ({cfg.timeout:g}s).",
            kind="timeout", url=cfg.url, partial="".join(chunks)) from exc
    except requests.RequestException as exc:
        raise OllamaError(f"Falló la llamada a {cfg.url}: {exc}", kind="protocol",
                          url=cfg.url, partial="".join(chunks)) from exc

    return ChatResult(content="".join(chunks), model=model, metrics=metrics,
                      cancelled=cancelled, via="ollama", notices=notices)


def _http_error(resp: requests.Response, cfg: OllamaSettings, model: str,
                has_images: bool) -> OllamaError:
    """Turns Ollama's own error body into the message we show."""
    detail = ""
    try:
        body = resp.json()
        if isinstance(body, dict):
            detail = str(body.get("error", "")).strip()
    except ValueError:
        detail = (resp.text or "").strip()[:300]

    if resp.status_code == 400 and has_images:
        return OllamaError(detail or "el modelo no acepta imágenes",
                           kind="vision_unsupported", url=cfg.url)
    if resp.status_code == 404 and "not found" in detail.lower():
        return OllamaError(
            f"{detail}. Corre `ollama pull {model}` en el servidor, o elige otro "
            "modelo con 'Buscar modelos'.", kind="http", url=cfg.url)
    return OllamaError(
        detail or f"{cfg.url} respondió HTTP {resp.status_code}.",
        kind="http", url=cfg.url)


def complete_result(prompt: str, *, system: str | None = None, **kwargs) -> ChatResult:
    """One-shot convenience wrapper: a bare prompt in, a full result out.

    The ReAct loop uses this one because it needs `cancelled` and the
    token metrics, not only the text.
    """
    messages: list[dict] = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})
    return chat(messages, **kwargs)


def complete(prompt: str, **kwargs) -> str:
    """Same, when the caller only wants the text."""
    return complete_result(prompt, **kwargs).content
