import pytest
import networkx as nx

from graph.detectors import (
    detect_blacklist_matches,
    detect_cycles,
    detect_shared_attribute_clusters,
)


def test_detect_cycles_finds_round_trip():
    g = nx.MultiDiGraph()
    g.add_edge("acc-A", "acc-B", type="EXECUTED_PAYMENT", amount=100)
    g.add_edge("acc-B", "acc-C", type="EXECUTED_PAYMENT", amount=100)
    g.add_edge("acc-C", "acc-A", type="EXECUTED_PAYMENT", amount=100)

    leads = detect_cycles(g)
    assert len(leads) == 1
    assert set(leads[0].entity_ids) == {"acc-A", "acc-B", "acc-C"}


def test_detect_blacklist_matches():
    g = nx.MultiDiGraph()
    g.add_node("RFC1", type="Company", blacklist_status="definitivo")
    g.add_node("RFC2", type="Company", blacklist_status="none")

    leads = detect_blacklist_matches(g)
    assert len(leads) == 1
    assert leads[0].entity_ids == ["RFC1"]


def test_shared_attribute_cluster_needs_at_least_two():
    g = nx.MultiDiGraph()
    g.add_edge("RFC1", "RFC2", type="SHARES_ADDRESS")

    leads = detect_shared_attribute_clusters(g)
    assert len(leads) == 1
    assert set(leads[0].entity_ids) == {"RFC1", "RFC2"}


def test_no_false_positives_on_clean_graph():
    g = nx.MultiDiGraph()
    g.add_node("RFC1", type="Company", blacklist_status="none")
    g.add_node("RFC2", type="Company", blacklist_status="none")
    g.add_edge("acc-RFC1", "acc-RFC2", type="EXECUTED_PAYMENT", amount=100, reference="inv-1")
    g.add_node("inv-1", type="Invoice", amount=100)

    assert detect_blacklist_matches(g) == []
    assert detect_cycles(g) == []
    assert detect_shared_attribute_clusters(g) == []


# ---------------------------------------------------------------------------
# Every injectable scenario must be solvable (FR-4)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("pattern", [
    "fake_billing", "kickback_shell", "round_tripping", "inflated_sales",
])
@pytest.mark.parametrize("seed", [1, 7, 42, 123, 2026])
def test_every_injected_pattern_trips_at_least_one_detector(pattern, seed):
    """A judge picks the scenario, so none of the four may be invisible.
    fake_billing used to trip nothing at all: unique address and phone,
    no cycle, a sink account with zero betweenness, and a payment that
    matched its invoice to the peso and cited its uuid. The agent had no
    way in."""
    from data.generator.estate_generator import generate, inject_pattern
    from graph.builder import build_graph
    from graph.detectors import DETECTORS

    clean = build_graph(generate(seed=seed, num_blacklisted=0))
    before = {name: len(fn(clean)) for name, fn in DETECTORS.items()}

    injected = build_graph(inject_pattern(generate(seed=seed, num_blacklisted=0), pattern))
    after = {name: len(fn(injected)) for name, fn in DETECTORS.items()}

    fired = [name for name in DETECTORS if after[name] > before[name]]
    assert fired, f"{pattern} (seed {seed}) trips no detector: {before} -> {after}"


def test_fake_billing_leaves_a_payment_with_no_invoice_behind_it():
    """The specific tell, so a future edit to the pattern can't quietly
    take it away again: the payment carries no invoice reference, which
    is both what an EFOS payment really looks like and what the
    SYSTEM_PROMPT teaches the agent to look for."""
    from data.generator.estate_generator import AUDITED_RFC, generate, inject_pattern
    from graph.builder import build_graph
    from graph.detectors import DETECTORS

    estate = inject_pattern(generate(seed=42, num_blacklisted=0), "fake_billing")
    injected_payments = [p for p in estate.payments if p.reference is None]
    assert injected_payments, "no unreferenced payment in the estate"
    assert any(p.amount == 250_000.0 for p in injected_payments)

    leads = DETECTORS["invoice_payment_mismatch"](build_graph(estate))
    assert any("no matching payment" in lead.reason for lead in leads)
