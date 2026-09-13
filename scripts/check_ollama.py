"""
"Can I reach the model or not?" -- in one command, without opening the UI.

    python -m scripts.check_ollama                 # the current configuration
    python -m scripts.check_ollama 192.168.1.50    # a specific server

Probes the server, checks that the configured model is there and runs a
short, real, timed generation, so you know whether the demo will survive.
Exits with code 0 if everything is fine, 1 otherwise.
"""
from __future__ import annotations

import sys
import time

from agent import ollama_client
from shared.config import load_settings, normalize_ollama_url

OK = "[ok]"
FAIL = "[--]"


def main(argv: list[str]) -> int:
    settings = load_settings(refresh=True)
    if len(argv) > 1:
        settings = settings.model_copy(update={"url": normalize_ollama_url(argv[1])})

    print(f"Server   : {settings.url}")
    print(f"Model    : {settings.model}")
    print(f"Timeout  : {settings.timeout:g}s   keep_alive: {settings.keep_alive}\n")

    ok, models, message = ollama_client.list_models(settings.url)
    print(f"{OK if ok else FAIL} {message}")
    if not ok:
        print("\nTroubleshooting guide: docs/ollama-red-local.md")
        return 1

    wanted = settings.model if ":" in settings.model else f"{settings.model}:latest"
    if models and wanted not in models and settings.model not in models:
        print(f"{FAIL} '{settings.model}' is not on that server.")
        print(f"     Available: {', '.join(models)}")
        print(f"     Run `ollama pull {settings.model}` on the server, or change OLLAMA_MODEL.")
        return 1
    print(f"{OK} '{settings.model}' is available.")

    print("\nTrying a short generation (the first one can take a while if "
          "the model is starting cold)...")
    started = time.monotonic()
    try:
        result = ollama_client.chat(
            [{"role": "user", "content": "Reply with just the word: ready"}],
            settings=settings, allow_fallback=False,
            on_token=lambda piece: print(piece, end="", flush=True))
    except ollama_client.OllamaError as exc:
        print(f"\n{FAIL} {exc}")
        return 1

    elapsed = time.monotonic() - started
    print(f"\n\n{OK} Answered in {elapsed:.1f}s "
          f"({result.metrics.eval_count} tokens, "
          f"{result.metrics.tokens_per_second:.1f} tok/s)")
    print("\nPut this in your .env:")
    print(f"  OLLAMA_URL={settings.url}")
    print(f"  OLLAMA_MODEL={settings.model}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
