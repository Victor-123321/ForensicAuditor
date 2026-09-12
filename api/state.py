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
        #: Identifies the run that currently owns the slot. A cancelled
        #: worker keeps dying in the background; without this its
        #: finally would release the slot of whatever started after it.
        self.current_run_id: int = 0


state = AppState()


def case_files_dir() -> Path:
    """Read through the env var on every call so tests can repoint it."""
    return Path(os.environ.get("FORENSIC_CASE_FILES_DIR", CASE_FILES_DIR))


def is_worth_keeping(case_file: CaseFile) -> bool:
    """Is this case file useful as an offline fallback?

    A run that died because the model was unreachable produces "No fraud
    could be proven" with an empty trail. Keeping those buries the one
    snapshot worth showing a judge: the directory had 13 files and 12 of
    them were that.
    """
    return bool(case_file.implicated_suppliers or case_file.evidence_trail.edges)


def save_case_file(case_file: CaseFile) -> Path | None:
    """Writes one case file to `case_files/{investigation_id}.json`.

    Returns the path, or None if it was not worth keeping or could not
    be written. Never raises: this runs inside the /investigate worker
    thread, and losing the snapshot must not lose the investigation the
    user just paid minutes of model time for.
    """
    if not is_worth_keeping(case_file):
        return None

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
    skipped: list[str] = []
    for path in sorted(directory.glob("*.json")):
        try:
            case_file = CaseFile.model_validate_json(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            # ValueError covers pydantic's ValidationError and bad JSON.
            # Name it: a silent skip means "reloaded 4" when there are 7
            # files, and adding a required field to CaseFile would
            # invalidate every existing snapshot without a word.
            skipped.append(f"{path.name}: {type(exc).__name__}")
            continue
        state.case_files[case_file.investigation_id] = case_file
        loaded += 1

    for problem in skipped:
        print(f"[api] skipped unreadable case file -- {problem}")
    return loaded


def steps_path(investigation_id: str) -> Path:
    return case_files_dir() / f"{investigation_id}.steps.json"


def save_steps(investigation_id: str, steps: list[dict],
               graph: dict | None = None) -> Path | None:
    """Stores the step stream -- and the graph it ran over -- next to its
    case file.

    The case file is the verdict; this is the reasoning that produced it.
    Keeping it lets the UI replay a real investigation without paying
    for the model again -- the rehearsal SRS section 9 asks for, and the
    only way to exercise the live animations without waiting a minute.

    The graph has to travel with it. Regenerating the estate from the
    same seed is NOT enough: invoices and payments get uuid4 ids, and
    measured, only 23 of 261 edges survive a regeneration. A replay over
    the graph on screen would light up almost nothing.
    """
    if not steps:
        return None
    try:
        path = steps_path(investigation_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps({"steps": steps, "graph": graph}, ensure_ascii=False),
                       encoding="utf-8")
        tmp.replace(path)
        return path
    except OSError:
        return None


def load_recording(investigation_id: str) -> dict:
    """{steps, graph} for a recorded run. Older recordings saved a bare
    list of steps and no graph; they still load, graph=None."""
    try:
        raw = json.loads(steps_path(investigation_id).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"steps": [], "graph": None}
    if isinstance(raw, list):
        return {"steps": raw, "graph": None}
    if isinstance(raw, dict):
        return {"steps": raw.get("steps") or [], "graph": raw.get("graph")}
    return {"steps": [], "graph": None}


def load_steps(investigation_id: str) -> list[dict]:
    """The saved step stream, or [] if this run has none."""
    return load_recording(investigation_id)["steps"]
