"""In-memory application state. No database: nothing here needs to
survive a restart beyond one demo session (SRS section 2.4)."""
from __future__ import annotations

import networkx as nx

from shared.schemas import CaseFile, DataEstate


class AppState:
    def __init__(self) -> None:
        self.estate: DataEstate | None = None
        self.graph: nx.MultiDiGraph | None = None
        self.case_files: dict[str, CaseFile] = {}


state = AppState()
