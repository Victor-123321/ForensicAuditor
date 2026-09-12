from data.generator.estate_generator import AUDITED_RFC, generate, generate_clean_control, inject_pattern
from graph.builder import build_graph
from graph.detectors import run_all


def test_generate_is_reproducible():
    e1 = generate(seed=7, num_suppliers=5, num_blacklisted=0)
    e2 = generate(seed=7, num_suppliers=5, num_blacklisted=0)
    assert [c.rfc for c in e1.companies] == [c.rfc for c in e2.companies]


def test_generate_includes_audited_company():
    e = generate(seed=1, num_suppliers=3, num_blacklisted=0)
    assert any(c.rfc == AUDITED_RFC and c.is_audited_entity for c in e.companies)


def test_inject_kickback_shell_adds_shared_attribute_companies():
    e = generate(seed=1, num_suppliers=3, num_blacklisted=0)
    before = len(e.companies)
    e2 = inject_pattern(e, "kickback_shell", seed=1)
    assert len(e2.companies) == before + 2
    addresses = [c.address for c in e2.companies[-2:]]
    assert addresses[0] == addresses[1]  # the shared-address tell


def test_inject_round_tripping_creates_a_cycle_of_payments():
    e = generate(seed=1, num_suppliers=3, num_blacklisted=0)
    e2 = inject_pattern(e, "round_tripping", seed=1, hops=3)
    new_payments = e2.payments[len(e.payments):]
    assert new_payments[0].from_account == f"acc-{AUDITED_RFC}"
    assert new_payments[-1].to_account == f"acc-{AUDITED_RFC}"


def test_suspicious_but_clean_suppliers_are_never_flagged():
    """SRS section 4.3's false-accusation trap: suppliers that look
    slightly odd (an unusually large invoice, a late-arriving payment)
    but are legitimate must never be surfaced as a lead by any of the
    5 deterministic detectors."""
    e = generate(seed=1, num_suppliers=5, num_blacklisted=0)
    suspicious_rfcs = {c.rfc for c in e.companies if c.name.startswith("Proveedor Atipico")}
    assert len(suspicious_rfcs) == 2

    g = build_graph(e)
    leads = run_all(g)
    flagged_ids = {entity_id for lead in leads for entity_id in lead.entity_ids}
    assert not (suspicious_rfcs & flagged_ids)


def test_clean_control_estate_produces_no_leads():
    """SRS section 10: the mandatory pre-demo Judgment test needs a
    control estate with literally zero fraud signals for any of the 5
    detectors to find -- the agent must accuse zero suppliers on it."""
    e = generate_clean_control(seed=99)
    g = build_graph(e)
    leads = run_all(g)
    assert leads == []
