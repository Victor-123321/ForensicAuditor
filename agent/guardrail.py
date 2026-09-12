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

# Edge types that can, on their own, substantiate a monetary
# rule-breaking claim (FR-16: "a specific rule broken ... AND a
# specific peso amount"). Decision: relational/clustering edges
# (SHARES_ADDRESS, SHARES_PHONE, SHARES_BANK_ACCOUNT, OWNS_ACCOUNT,
# LEGAL_REP_OF, OWNS_COMPANY) are real, useful signal for the
# shell-company-cluster detector (FR-8), but they don't themselves
# record a rule violation or a peso amount -- "shares a phone number
# with another company" is not, by itself, a chargeable act. A claim
# backed ONLY by edges of these relational types is rejected, even
# though the edge id genuinely resolves in the evidence graph, because
# resolving is necessary but not sufficient: the cited edge must also
# be the kind of thing FR-16 means by "a rule broken". A claim that
# cites a relational edge ALONGSIDE at least one edge from this set is
# fine -- the relational edge is just extra context at that point.
ACCUSATION_WORTHY_EDGE_TYPES = {
    "EXECUTED_PAYMENT", "ISSUED_INVOICE", "RECEIVED_INVOICE", "BLACKLISTED_AS",
}


def validate_case_file(g: nx.MultiDiGraph, draft: CaseFile) -> tuple[CaseFile, list[str]]:
    """Returns (cleaned_case_file, list_of_rejection_reasons)."""
    edges_by_id = {edge.id: edge for edge in draft.evidence_trail.edges}
    kept: list[AccusationClaim] = []
    rejected_reasons: list[str] = []

    for claim in draft.implicated_suppliers:
        if not claim.evidence_edge_ids:
            rejected_reasons.append(
                f"Dropped accusation against {claim.supplier_rfc}: no evidence edges cited")
            continue

        unresolved = [eid for eid in claim.evidence_edge_ids if eid not in edges_by_id]
        if unresolved:
            rejected_reasons.append(
                f"Dropped accusation against {claim.supplier_rfc}: "
                f"edge id(s) {unresolved} do not resolve in the evidence graph")
            continue

        cited_types = {edges_by_id[eid].type for eid in claim.evidence_edge_ids}
        if cited_types.isdisjoint(ACCUSATION_WORTHY_EDGE_TYPES):
            rejected_reasons.append(
                f"Dropped accusation against {claim.supplier_rfc}: cited edge(s) are all "
                f"relational ({sorted(cited_types)}), none establishes a rule violation")
            continue

        if claim.peso_amount <= 0:
            rejected_reasons.append(
                f"Dropped accusation against {claim.supplier_rfc}: no positive peso amount cited")
            continue

        kept.append(claim)

    # The total has to follow the claims that survived. It used to be
    # computed before the guardrail ran and never revisited, so a case
    # file could report "0 acusados / $54,594.75 en riesgo" -- and both
    # frontends print that number in large type.
    cleaned = draft.model_copy(update={
        "implicated_suppliers": kept,
        "total_amount_at_risk": sum(c.peso_amount for c in kept),
    })
    return cleaned, rejected_reasons
