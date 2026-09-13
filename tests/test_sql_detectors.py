"""
Tests for the Snowflake/Cortex integration (docs/snowflake-integracion.md).

Most of this file only runs when real Snowflake credentials are present
(@pytest.mark.skipif(not snowflake_available())) -- CI and any
teammate's machine without a trial account simply skips it, never
fails. The two tests that matter with NO credentials at all --
DATA_SOURCE=local behaving exactly as before, and DATA_SOURCE=snowflake
falling back cleanly when Snowflake is unreachable -- always run.
"""
from __future__ import annotations

import json
import os

import pytest

from data.generator.estate_generator import generate, inject_pattern
from data.snowflake_client import snowflake_available
from graph.builder import build_graph, to_graph_export
from shared.schemas import Lead

_needs_snowflake = pytest.mark.skipif(
    not snowflake_available(), reason="SNOWFLAKE_ACCOUNT/SNOWFLAKE_PAT not configured")


# ---------------------------------------------------------------------------
# Always run: no credentials required.
# ---------------------------------------------------------------------------

def test_data_source_local_is_unchanged(monkeypatch):
    """The rule that governs this whole integration: with DATA_SOURCE
    unset (or "local"), api/main.py's flag helper must return exactly
    what build_graph() would have returned before Snowflake existed --
    same node/edge counts, regardless of whether Snowflake credentials
    happen to be present on this machine."""
    monkeypatch.delenv("DATA_SOURCE", raising=False)
    from api.main import _build_graph_for_state

    estate = generate(seed=42, num_suppliers=8, num_blacklisted=2)
    estate = inject_pattern(estate, "kickback_shell", seed=7)

    direct = to_graph_export(build_graph(estate))
    via_flag = to_graph_export(_build_graph_for_state(estate))

    assert len(via_flag.nodes) == len(direct.nodes)
    assert len(via_flag.edges) == len(direct.edges)


def test_data_source_snowflake_falls_back_without_breaking(monkeypatch):
    """DATA_SOURCE=snowflake with no credentials must never raise -- it
    falls back to the full local graph with a warning (the doc's
    non-negotiable rule: this integration is never a single point of
    failure for the demo)."""
    monkeypatch.setenv("DATA_SOURCE", "snowflake")
    monkeypatch.delenv("SNOWFLAKE_ACCOUNT", raising=False)
    monkeypatch.delenv("SNOWFLAKE_PAT", raising=False)
    from api.main import _build_graph_for_state

    estate = generate(seed=42, num_suppliers=8, num_blacklisted=2)
    with pytest.warns(RuntimeWarning, match="DATA_SOURCE=snowflake failed"):
        g = _build_graph_for_state(estate)

    assert g.number_of_nodes() == build_graph(estate).number_of_nodes()


def _payments(g) -> set[tuple[str, str]]:
    return {(u, v) for u, v, d in g.edges(data=True) if d.get("type") == "EXECUTED_PAYMENT"}


def test_reduced_graph_keeps_the_money_trail():
    """A plain 1-hop cut around what the warehouse flagged kept 1 of 80
    payments and dropped the audited company, so in DATA_SOURCE=snowflake
    no accusation could cite a payment. Seeded with what the shared-
    attribute SQL flags for kickback_shell: the supplier and its shell."""
    from graph.sql_detectors import _reduce_to_neighborhood

    estate = inject_pattern(generate(seed=42, num_suppliers=8, num_blacklisted=2),
                            "kickback_shell", seed=7)
    supplier, shell = estate.companies[-2].rfc, estate.companies[-1].rfc
    full = build_graph(estate)
    reduced = _reduce_to_neighborhood(full, {supplier, shell})

    assert reduced.number_of_nodes() < full.number_of_nodes()
    assert "AUD010101XXX" in reduced and "acc-AUD010101XXX" in reduced
    assert ("acc-AUD010101XXX", f"acc-{supplier}") in _payments(reduced)   # paid in full
    assert (f"acc-{supplier}", f"acc-{shell}") in _payments(reduced)        # the cut sent back


def test_an_invoice_lead_brings_the_payment_behind_it():
    """The mismatch detector flags invoices, not companies. One hop from
    an invoice reaches its issuer but not the issuer's account, so the
    payment for that very invoice used to be cut out."""
    from graph.sql_detectors import _reduce_to_neighborhood

    estate = inject_pattern(generate(seed=42, num_suppliers=8, num_blacklisted=2),
                            "kickback_shell", seed=7)
    invoice = estate.invoices[-1]
    reduced = _reduce_to_neighborhood(build_graph(estate), {invoice.uuid})

    assert ("acc-AUD010101XXX", f"acc-{invoice.emisor_rfc}") in _payments(reduced)


def test_vague_concept_leads_cite_edges_that_exist_in_the_graph(monkeypatch):
    """A Cortex lead used to carry only the supplier: nothing the agent
    could open and no edge a claim could cite. Its invoices and their
    ISSUED_INVOICE edges must resolve in the graph the agent walks."""
    from graph import sql_detectors

    estate = inject_pattern(generate(seed=42, num_suppliers=8, num_blacklisted=2), "fake_billing")
    phantom = estate.invoices[-1]

    def fake_sql(statement: str, timeout: int = 60) -> list[dict]:
        if "SELECT CONCEPTO" in statement:
            return [{"CONCEPTO": inv.concepts[0].description,
                     "LABEL": "VAGO" if inv is phantom else "ESPECIFICO"}
                    for inv in estate.invoices]
        return [{"SUPPLIER_ID": phantom.emisor_rfc, "INVOICE_IDS": json.dumps([phantom.uuid])}]

    monkeypatch.setattr(sql_detectors, "_cortex_sql_function_name", lambda: "AI_COMPLETE")
    monkeypatch.setattr(sql_detectors, "execute_sql", fake_sql)

    [lead] = sql_detectors.detect_vague_concepts_cortex()
    edge_ids = {str(key) for _, _, key in build_graph(estate).edges(keys=True)}

    assert lead.entity_ids == [phantom.emisor_rfc, phantom.uuid]
    assert lead.supporting_edge_ids and set(lead.supporting_edge_ids) <= edge_ids


# ---------------------------------------------------------------------------
# Real-account tests: skipped without SNOWFLAKE_ACCOUNT/SNOWFLAKE_PAT.
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def loaded_estate():
    """Loads a small estate + the real SAT blacklist into the warehouse
    once per test module -- each detector call below is a fresh query
    against that same loaded state."""
    from data.snowflake_loader import load_estate, load_sat_blacklist

    estate = generate(seed=42, num_suppliers=5, num_blacklisted=2)
    estate = inject_pattern(estate, "kickback_shell", seed=7)
    load_estate(estate)
    load_sat_blacklist()
    return estate


@_needs_snowflake
def test_sql_detectors_return_well_formed_leads(loaded_estate):
    from graph.sql_detectors import run_all_sql

    leads = run_all_sql()
    assert leads, "expected at least one lead from the kickback_shell scenario"
    for lead in leads:
        assert isinstance(lead, Lead)
        assert lead.entity_ids
        assert lead.reason


@_needs_snowflake
def test_blacklist_sql_matches_a_real_blacklisted_supplier(loaded_estate):
    from graph.sql_detectors import detect_blacklisted_suppliers_sql

    leads = detect_blacklisted_suppliers_sql()
    flagged = {eid for lead in leads for eid in lead.entity_ids}
    blacklisted_rfcs = {c.rfc for c in loaded_estate.companies
                         if c.blacklist_status.value in ("definitivo", "presunto")}
    assert blacklisted_rfcs, "fixture should have sampled at least one real blacklisted RFC"
    assert blacklisted_rfcs & flagged


@_needs_snowflake
def test_desvirtuado_status_is_never_accused(loaded_estate):
    """The exact false-positive the doc calls out: a supplier SAT
    already exonerated ('Desvirtuado') must not come back as a lead,
    even though the RFC genuinely appears in SAT_BLACKLIST."""
    from data.snowflake_client import execute_sql, sql_string_literal
    from graph.sql_detectors import detect_blacklisted_suppliers_sql

    test_rfc = "TESTDESV000XYZ"
    execute_sql(
        f"INSERT INTO SUPPLIERS (supplier_id, name, rfc) VALUES "
        f"({sql_string_literal(test_rfc)}, {sql_string_literal('Exonerada SA')}, "
        f"{sql_string_literal(test_rfc)})")
    execute_sql(
        f"INSERT INTO SAT_BLACKLIST (rfc, name, status) VALUES "
        f"({sql_string_literal(test_rfc)}, {sql_string_literal('Exonerada SA')}, "
        f"{sql_string_literal('Desvirtuado')})")

    leads = detect_blacklisted_suppliers_sql()
    flagged = {eid for lead in leads for eid in lead.entity_ids}
    assert test_rfc not in flagged
