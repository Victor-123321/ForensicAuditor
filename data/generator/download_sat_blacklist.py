"""
Downloads and caches SAT's official Article 69-B blacklist (EFOS).

Run this once before working on the estate generator -- everything else
in data/generator/ reads from the cached CSV, not the network, so the
demo has no live dependency on SAT's servers.

    python -m data.generator.download_sat_blacklist
"""
from __future__ import annotations

import csv
import sys
from pathlib import Path

import requests

SAT_URL = "http://omawww.sat.gob.mx/cifras_sat/Documents/Listado_Completo_69-B.csv"
CACHE_PATH = Path(__file__).resolve().parents[2] / "data" / "raw" / "sat_69b.csv"


def download(force: bool = False) -> Path:
    if CACHE_PATH.exists() and not force:
        print(f"Already cached at {CACHE_PATH} (use --force to re-download)")
        return CACHE_PATH

    print(f"Downloading {SAT_URL} ...")
    resp = requests.get(SAT_URL, timeout=30)
    resp.raise_for_status()

    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    CACHE_PATH.write_bytes(resp.content)
    print(f"Saved {len(resp.content):,} bytes to {CACHE_PATH}")
    return CACHE_PATH


def load_blacklist(skip_rows: int = 3) -> dict[str, str]:
    """Returns {rfc: status}, e.g. {'ABC010101AAA': 'definitivo'}.

    CONFIRMED against the real downloaded file (2026-09): the CSV opens
    with a one-row disclosure notice, then a one-row title, then the
    real column header row ("No", "RFC", "Nombre del Contribuyente",
    "Situacion del contribuyente", ...) -- so 3 rows must be skipped,
    not 2. Column 0 is a sequential row number, NOT the RFC; the RFC is
    column 1, and the status ("Situacion del contribuyente") is column
    3 -- the last column is a mostly-empty publication-date field, not
    the status.
    """
    if not CACHE_PATH.exists():
        raise FileNotFoundError(
            f"{CACHE_PATH} not found -- run `python -m data.generator.download_sat_blacklist` first"
        )

    result: dict[str, str] = {}
    with open(CACHE_PATH, encoding="latin-1", newline="") as f:
        rows = list(csv.reader(f))

    for row in rows[skip_rows:]:
        if len(row) <= 3 or not row[1].strip():
            continue
        rfc = row[1].strip().upper()
        status_raw = row[3].strip().lower()
        result[rfc] = _normalize_status(status_raw)
    return result


def _normalize_status(raw: str) -> str:
    if "definitivo" in raw:
        return "definitivo"
    if "presunto" in raw:
        return "presunto"
    if "desvirtuado" in raw:
        return "desvirtuado"
    if "favorable" in raw:
        return "sentencia_favorable"
    return "presunto"  # conservative default -- confirm against the real file


if __name__ == "__main__":
    force = "--force" in sys.argv
    path = download(force=force)
    try:
        bl = load_blacklist()
        print(f"Parsed {len(bl):,} RFCs from the blacklist")
    except Exception as e:  # noqa: BLE001 -- deliberately broad for a CLI script
        print(f"Downloaded but could not parse yet ({e}) -- "
              f"inspect {CACHE_PATH} and adjust load_blacklist()")
