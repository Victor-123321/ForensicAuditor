"""
"¿Conecto con el modelo o no?" -- en un comando, sin abrir la UI.

    python -m scripts.check_ollama                 # la configuración actual
    python -m scripts.check_ollama 192.168.1.50    # un servidor concreto

Sondea el servidor, comprueba que el modelo configurado está ahí y hace
una generación corta de verdad, cronometrada, para que sepas si vas a
sobrevivir la demo. Termina con código 0 si todo está bien, 1 si no.
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

    print(f"Servidor : {settings.url}")
    print(f"Modelo   : {settings.model}")
    print(f"Timeout  : {settings.timeout:.0f}s   keep_alive: {settings.keep_alive}\n")

    ok, models, message = ollama_client.list_models(settings.url)
    print(f"{OK if ok else FAIL} {message}")
    if not ok:
        print("\nGuía de diagnóstico: docs/ollama-red-local.md")
        return 1

    wanted = settings.model if ":" in settings.model else f"{settings.model}:latest"
    if models and wanted not in models and settings.model not in models:
        print(f"{FAIL} '{settings.model}' no está en ese servidor.")
        print(f"     Disponibles: {', '.join(models)}")
        print(f"     Corre `ollama pull {settings.model}` en el servidor, o cambia OLLAMA_MODEL.")
        return 1
    print(f"{OK} '{settings.model}' está disponible.")

    print("\nProbando una generación corta (la primera puede tardar si "
          "el modelo arranca en frío)...")
    started = time.monotonic()
    try:
        result = ollama_client.chat(
            [{"role": "user", "content": "Responde solo con la palabra: listo"}],
            settings=settings, allow_fallback=False,
            on_token=lambda piece: print(piece, end="", flush=True))
    except ollama_client.OllamaError as exc:
        print(f"\n{FAIL} {exc}")
        return 1

    elapsed = time.monotonic() - started
    print(f"\n\n{OK} Respondió en {elapsed:.1f}s "
          f"({result.metrics.eval_count} tokens, "
          f"{result.metrics.tokens_per_second:.1f} tok/s)")
    print("\nPon esto en tu .env:")
    print(f"  OLLAMA_URL={settings.url}")
    print(f"  OLLAMA_MODEL={settings.model}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
