from data.generator.estate_generator import AUDITED_RFC, generate, inject_pattern


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
