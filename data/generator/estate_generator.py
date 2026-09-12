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
from datetime import date, timedelta

from data.generator import patterns
from data.generator.download_sat_blacklist import load_blacklist
from shared.schemas import BankAccount, BlacklistStatus, Company, DataEstate, Invoice, InvoiceConcept, Payment

AUDITED_RFC = "AUD010101XXX"


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
        real_blacklisted_rfcs = rng.sample(list(blacklist.keys()), k=min(num_blacklisted, len(blacklist)))
    except FileNotFoundError:
        blacklist, real_blacklisted_rfcs = {}, []

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
                concepts=[InvoiceConcept(description="Insumos varios", amount=amount)],
            )
            invoices.append(inv)
            payments.append(Payment(
                transaction_id=str(uuid.uuid4()), from_account=audited_account.account_id,
                to_account=acc.account_id, amount=amount,
                date=inv_date + timedelta(days=rng.randint(5, 30)), reference=inv.uuid,
            ))

    return DataEstate(seed=seed, companies=companies, people=[], accounts=accounts,
                       invoices=invoices, payments=payments)


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
