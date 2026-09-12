"""
Evidence guardrail (FR-16, FR-17): strips any accusation in a draft
case file that doesn't resolve to a real edge in the evidence graph.
This is the concrete mechanism behind "refuse to accuse a supplier it
cannot back up" -- treat it as mandatory, not optional (see the SRS,
section 9, risks table).
"""
from __future__ import annotations

import networkx as nx

from shared.schemas import AccusationClaim, CaseFile


def validate_case_file(g: nx.MultiDiGraph, draft: CaseFile) -> tuple[CaseFile, list[str]]:
    """Returns (cleaned_case_file, list_of_rejection_reasons)."""
    valid_edge_ids = {edge.id for edge in draft.evidence_trail.edges}
    kept: list[AccusationClaim] = []
    rejected_reasons: list[str] = []

    for claim in draft.implicated_suppliers:
        if not claim.evidence_edge_ids:
            rejected_reasons.append(
                f"Dropped accusation against {claim.supplier_rfc}: no evidence edges cited")
            continue

        unresolved = [eid for eid in claim.evidence_edge_ids if eid not in valid_edge_ids]
        if unresolved:
            rejected_reasons.append(
                f"Dropped accusation against {claim.supplier_rfc}: "
                f"edge id(s) {unresolved} do not resolve in the evidence graph")
            continue

        if claim.peso_amount <= 0:
            rejected_reasons.append(
                f"Dropped accusation against {claim.supplier_rfc}: no positive peso amount cited")
            continue

        kept.append(claim)

    cleaned = draft.model_copy(update={"implicated_suppliers": kept})
    return cleaned, rejected_reasons
