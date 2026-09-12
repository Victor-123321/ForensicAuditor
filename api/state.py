"""In-memory application state, plus the on-disk case-file snapshots.

Nothing here needs to survive a restart for correctness (SRS section
2.4) -- except the case files. An investigation costs minutes of model
time, and SRS section 9 asks for "a pre-generated, pre-run rehearsal
scenario with a cached case file kept as an offline fallback": if the
process dies mid-hackathon, or the LAN model becomes unreachable in
front of the judges, we reload a run we already did instead of
improvising. So case files are written to `case_files/` as they are
produced (SRS section 11) and read back on startup.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import networkx as nx

from shared.schemas import CaseFile, DataEstate

#: Where case-file snapshots live. Overridable so tests never touch the
#: repo's own directory.
CASE_FILES_DIR = Path(
    os.environ.get("FORENSIC_CASE_FILES_DIR",
                   Path(__file__).resolve().parents[1] / "case_files"))


class AppState:
    def __init__(self) -> None:
        self.estate: DataEstate | None = None
        self.graph: nx.MultiDiGraph | None = None
        self.case_files: dict[str, CaseFile] = {}
        # Guards /investigate against a double click starting two
        # threads over the same graph (the cancel flag is process-wide).
        self.investigation_running: bool = False


state = AppState()


def case_files_dir() -> Path:
    """Read through the env var on every call so tests can repoint it."""
    return Path(os.environ.get("FORENSIC_CASE_FILES_DIR", CASE_FILES_DIR))


def save_case_file(case_file: CaseFile) -> Path | None:
    """Writes one case file to `case_files/{investigation_id}.json`.

    Returns the path, or None if it could not be written. Never raises:
    this runs inside the /investigate worker thread, and losing the
    snapshot must not lose the investigation the user just paid minutes
    of model time for.
    """
    directory = case_files_dir()
    try:
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{case_file.investigation_id}.json"
        # Write to a temporary file and replace, so a crash (or a judge
        # closing the laptop) can't leave a half-written snapshot that
        # then fails to load on the next startup.
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(case_file.model_dump_json(indent=2), encoding="utf-8")
        tmp.replace(path)
        return path
    except OSError:
        return None


def load_case_files_from_disk() -> int:
    """Loads every snapshot in `case_files/` into `state.case_files`.

    Returns how many were loaded. A missing directory is normal on a
    fresh clone; a corrupt or foreign .json is skipped rather than
    taking the API down at startup.
    """
    directory = case_files_dir()
    if not directory.is_dir():
        return 0

    loaded = 0
    for path in sorted(directory.glob("*.json")):
        try:
            case_file = CaseFile.model_validate_json(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            # ValueError covers pydantic's ValidationError and bad JSON.
            continue
        state.case_files[case_file.investigation_id] = case_file
        loaded += 1
    return loaded
