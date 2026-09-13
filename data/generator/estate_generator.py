"""
Synthetic data-estate generator (SRS section 4 / FR-1, FR-2).

Produces a DataEstate: one audited company, N supplier companies (a few
genuinely SAT-blacklisted, the rest clean -- so a naive "flag anything
odd" approach produces false accusations, which is exactly the trap the
brief warns about), invoices, and payments -- reproducible from a seed.

    python -m data.generator.estate_generator
"""
from __future__ import annotations

import random
import uuid
import warnings
from datetime import date, timedelta

from data.generator import patterns
from data.generator.download_sat_blacklist import load_blacklist
from shared.schemas import ACCUSABLE_BLACKLIST_STATUSES, BankAccount, BlacklistStatus, Company, DataEstate, Invoice, InvoiceConcept, Payment

AUDITED_RFC = "AUD010101XXX"

# What a legitimate CFDI concepto looks like: a good, a quantity, an
# asset, a deliverable. These used to be "Insumos varios" and "Servicios
# profesionales", which are exactly the generic, unverifiable wording of
# a 69-B phantom invoice -- Cortex (graph/sql_detectors.py) rightly
# called 22 of 24 suppliers VAGO and the language signal meant nothing.
# Picked by position, never by rng: drawing from rng would shift every
# RFC, amount and date that follows for a given seed.
ORDINARY_CONCEPTS = (
    "Lamina de acero rolada en frio cal. 16, 2,000 kg",
    "Tornilleria hexagonal grado 5, 1/2 x 2 in, 500 piezas",
    "Tarimas de madera 1.20 x 1.00 m, 150 piezas",
    "Flete Monterrey-Saltillo, caja seca 53 ft, 3 viajes",
    "Pelicula stretch 18 in x 1,500 ft, 40 rollos",
    "Mantenimiento preventivo compresor Atlas Copco GA30, orden 4471",
    "Guantes de nitrilo talla M, 60 cajas",
    "Calibracion de 12 basculas industriales con reporte de servicio",
)
# The false-accusation trap's traits are size and timing, not wording.
TRAP_CONCEPTS = (
    "Auditoria de seguridad industrial planta Apodaca, informe final",
    "Desarrollo del modulo de inventarios SAP, entregable fase 2",
)


def _rand_rfc(rng: random.Random) -> str:
    letters = "".join(rng.choices("ABCDEFGHIJKLMNOPQRSTUVWXYZ", k=3))
    digits = "".join(rng.choices("0123456789", k=6))
    suffix = "".join(rng.choices("ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789", k=3))
    return f"{letters}{digits}{suffix}"


def _rand_clabe(rng: random.Random) -> str:
    return "".join(rng.choices("0123456789", k=18))


def _clean_supplier(rng: random.Random, base_date: date) -> Company:
    rfc = _rand_rfc(rng)
    return Company(
        rfc=rfc, name=f"Proveedor {rfc[:4]} SA de CV",
        address=f"Calle {rng.randint(1, 999)}, Monterrey, NL",
        phone=f"81{rng.randint(10000000, 99999999)}",
        industry=rng.choice(["Manufactura", "Servicios", "Comercio", "Logistica"]),
        incorporation_date=base_date - timedelta(days=rng.randint(200, 3000)),
        blacklist_status=BlacklistStatus.NONE,
    )


def _add_suspicious_but_clean_suppliers(rng: random.Random, base_date: date, accounts_by_rfc: dict[str, str],
                                         count: int = 2) -> dict:
    """SRS section 4.3's false-accusation trap: suppliers with a trait
    that LOOKS odd to a human skimming the data (an unusually large
    invoice, a payment that lands well outside the usual window) but
    resolves cleanly on inspection -- amount paid always matches the
    invoice exactly, so no detector in graph/detectors.py should ever
    surface these as a lead. Exists so the agent (and this test suite)
    can be checked for false positives, not just true positives.
    """
    companies, accounts, invoices, payments = [], [], [], []

    for i in range(count):
        rfc = _rand_rfc(rng)
        company = Company(
            rfc=rfc, name=f"Proveedor Atipico {rfc[:4]} SA de CV",
            address=f"Torre Sospechosa {rng.randint(1, 999)}, San Pedro, NL",
            phone=f"81{rng.randint(10000000, 99999999)}",
            industry=rng.choice(["Consultoria", "Servicios"]),
            incorporation_date=base_date - timedelta(days=rng.randint(400, 2000)),
            blacklist_status=BlacklistStatus.NONE,
        )
        acc = BankAccount(account_id=f"acc-{rfc}", clabe=_rand_clabe(rng), bank_name=rng.choice(["Banorte", "BBVA"]))
        accounts_by_rfc[rfc] = acc.account_id
        inv_date = base_date + timedelta(days=rng.randint(0, 200))

        if i % 2 == 0:
            # Trait: an unusually large invoice -- but paid in full,
            # exactly, so the invoice/payment mismatch detector (FR-6)
            # has nothing to flag; size alone isn't a rule it checks.
            amount = round(rng.uniform(300_000, 500_000), 2)
            pay_date = inv_date + timedelta(days=rng.randint(10, 20))
        else:
            # Trait: the payment lands well after the usual 5-30 day
            # window -- but the amount still matches exactly, and the
            # mismatch detector never looks at dates, so it's clean.
            amount = round(rng.uniform(10_000, 60_000), 2)
            pay_date = inv_date + timedelta(days=rng.randint(45, 60))

        inv = Invoice(
            uuid=str(uuid.uuid4()), folio=f"F-{rng.randint(1000, 9999)}",
            emisor_rfc=rfc, receptor_rfc=AUDITED_RFC, amount=amount,
            date=inv_date, uso_cfdi="G03", forma_pago="03", metodo_pago="PUE",
            concepts=[InvoiceConcept(description=TRAP_CONCEPTS[i % len(TRAP_CONCEPTS)], amount=amount)],
        )
        pay = Payment(
            transaction_id=str(uuid.uuid4()), from_account=accounts_by_rfc[AUDITED_RFC],
            to_account=acc.account_id, amount=amount, date=pay_date, reference=inv.uuid,
        )
        companies.append(company)
        accounts.append(acc)
        invoices.append(inv)
        payments.append(pay)

    return {"companies": companies, "accounts": accounts, "invoices": invoices, "payments": payments}


def generate(seed: int = 42, num_suppliers: int = 20, num_blacklisted: int = 3) -> DataEstate:
    rng = random.Random(seed)
    base_date = date(2026, 1, 1)

    audited = Company(
        rfc=AUDITED_RFC, name="Grupo Industrial Ejemplo SA de CV",
        address="Av. Constitucion 400, Monterrey, NL", phone="8112223333",
        industry="Manufactura", incorporation_date=date(2005, 3, 1),
        is_audited_entity=True,
    )
    audited_account = BankAccount(account_id=f"acc-{AUDITED_RFC}", clabe=_rand_clabe(rng), bank_name="Banorte")

    companies = [audited]
    accounts = [audited_account]
    invoices: list[Invoice] = []
    payments: list[Payment] = []
    accounts_by_rfc = {AUDITED_RFC: audited_account.account_id}

    # Blend in real SAT-blacklisted RFCs among the synthetic suppliers
    # (FR-2). Falls back gracefully if the CSV hasn't been downloaded
    # yet, so this generator is runnable before Dev 1 finishes the SAT
    # ingestion task.
    try:
        blacklist = load_blacklist()
        # Sample only from statuses that incriminate: a uniform sample
        # over the whole file hands the demo a company SAT EXONERATED
        # (13.9% of the real list) and paints it red like the guilty ones.
        guilty = [rfc for rfc, status in blacklist.items()
                  if status in ACCUSABLE_BLACKLIST_STATUSES]
        real_blacklisted_rfcs = rng.sample(guilty, k=min(num_blacklisted, len(guilty)))
    except FileNotFoundError:
        blacklist, real_blacklisted_rfcs = {}, []
        if num_blacklisted:
            # Used to pass silently, so the star detector found nothing
            # and nobody knew why. Say it out loud.
            warnings.warn(
                f"num_blacklisted={num_blacklisted} ignored: the SAT list is not on "
                "disk. Run `python -m data.generator.download_sat_blacklist` or the "
                "blacklist_match detector will have nothing to find.",
                RuntimeWarning, stacklevel=2)

    for i in range(num_suppliers):
        if i < len(real_blacklisted_rfcs):
            rfc = real_blacklisted_rfcs[i]
            status = BlacklistStatus(blacklist.get(rfc, "presunto"))
            supplier = Company(
                rfc=rfc, name=f"Proveedor Real Listado {i}",
                address=f"Calle {rng.randint(1, 999)}, Monterrey, NL",
                phone=f"81{rng.randint(10000000, 99999999)}",
                industry=rng.choice(["Manufactura", "Servicios", "Comercio"]),
                incorporation_date=base_date - timedelta(days=rng.randint(60, 500)),
                blacklist_status=status,
            )
        else:
            supplier = _clean_supplier(rng, base_date)

        companies.append(supplier)
        acc = BankAccount(account_id=f"acc-{supplier.rfc}", clabe=_rand_clabe(rng),
                           bank_name=rng.choice(["Banorte", "BBVA", "Santander", "Banamex"]))
        accounts.append(acc)
        accounts_by_rfc[supplier.rfc] = acc.account_id

        # a handful of ordinary, unremarkable invoices + matching payments
        for _ in range(rng.randint(2, 5)):
            amount = round(rng.uniform(5_000, 80_000), 2)
            inv_date = base_date + timedelta(days=rng.randint(0, 240))
            inv = Invoice(
                uuid=str(uuid.uuid4()), folio=f"F-{rng.randint(1000, 9999)}",
                emisor_rfc=supplier.rfc, receptor_rfc=AUDITED_RFC, amount=amount,
                date=inv_date, uso_cfdi="G03", forma_pago="03", metodo_pago="PUE",
                concepts=[InvoiceConcept(
                    description=ORDINARY_CONCEPTS[len(invoices) % len(ORDINARY_CONCEPTS)],
                    amount=amount)],
            )
            invoices.append(inv)
            payments.append(Payment(
                transaction_id=str(uuid.uuid4()), from_account=audited_account.account_id,
                to_account=acc.account_id, amount=amount,
                date=inv_date + timedelta(days=rng.randint(5, 30)), reference=inv.uuid,
            ))

    # SRS section 4.3's false-accusation trap: 2 suppliers that look
    # slightly odd but are clean -- see _add_suspicious_but_clean_suppliers.
    suspicious_clean = _add_suspicious_but_clean_suppliers(rng, base_date, accounts_by_rfc, count=2)
    companies.extend(suspicious_clean["companies"])
    accounts.extend(suspicious_clean["accounts"])
    invoices.extend(suspicious_clean["invoices"])
    payments.extend(suspicious_clean["payments"])

    return DataEstate(seed=seed, companies=companies, people=[], accounts=accounts,
                       invoices=invoices, payments=payments)


def generate_clean_control(seed: int = 0) -> DataEstate:
    """SRS section 10's mandatory pre-demo Judgment test: a control
    estate with zero planted fraud schemes AND zero blacklisted
    suppliers, so none of the 5 detectors in graph/detectors.py should
    surface a single lead over it -- the agent is expected to accuse
    nobody. `generate()` never injects a fraud pattern on its own (that
    only happens via a separate `inject_pattern()` call), so this is
    just `generate()` with num_blacklisted forced to 0 for clarity and
    to guarantee the blacklist detector has nothing to match either.
    """
    return generate(seed=seed, num_blacklisted=0)


def inject_pattern(estate: DataEstate, pattern_name: str, seed: int | None = None, **kwargs) -> DataEstate:
    """Implements FR-4's Scenario Injector: adds one fraud-pattern
    instance to an existing estate and returns the merged result."""
    if pattern_name not in patterns.PATTERNS:
        raise ValueError(f"Unknown pattern '{pattern_name}', choose from {list(patterns.PATTERNS)}")

    rng = random.Random(seed if seed is not None else random.randint(0, 10**6))
    accounts_by_rfc = {c.rfc: f"acc-{c.rfc}" for c in estate.companies}
    base_date = date(2026, 6, 1)

    fn = patterns.PATTERNS[pattern_name]
    result = fn(AUDITED_RFC, accounts_by_rfc, rng, base_date, **kwargs)

    return DataEstate(
        seed=estate.seed,
        companies=estate.companies + result["companies"],
        people=estate.people,
        accounts=estate.accounts + result["accounts"],
        invoices=estate.invoices + result["invoices"],
        payments=estate.payments + result["payments"],
    )


if __name__ == "__main__":
    e = generate()
    print(f"Generated estate: {len(e.companies)} companies, {len(e.invoices)} invoices, "
          f"{len(e.payments)} payments")
    e2 = inject_pattern(e, "kickback_shell")
    print(f"After injecting kickback_shell: {len(e2.companies)} companies, "
          f"{len(e2.payments)} payments")
