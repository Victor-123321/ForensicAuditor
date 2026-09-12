"""
Fraud topology generators, patterned on AMLSim's documented pattern
types (fan-out, cycle/round-tripping, gather-scatter -- see the SRS,
section 2.4, on why we reimplement the patterns instead of running
AMLSim's own Java pipeline). Each function returns new entities to
merge into a DataEstate -- they don't touch the graph directly, so they
stay easy to unit test.
"""
from __future__ import annotations

import random
import uuid
from datetime import date, timedelta

from shared.schemas import BankAccount, Company, Invoice, InvoiceConcept, Payment


def _rand_rfc(rng: random.Random) -> str:
    letters = "".join(rng.choices("ABCDEFGHIJKLMNOPQRSTUVWXYZ", k=3))
    digits = "".join(rng.choices("0123456789", k=6))
    suffix = "".join(rng.choices("ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789", k=3))
    return f"{letters}{digits}{suffix}"


def _rand_clabe(rng: random.Random) -> str:
    return "".join(rng.choices("0123456789", k=18))


def fake_billing(audited_rfc: str, accounts_by_rfc: dict[str, str], rng: random.Random,
                  base_date: date, amount: float = 250_000.0) -> dict:
    """A supplier bills the audited company for work never done.

    The payment deliberately carries NO invoice reference. Measured
    across seeds 1, 7, 42, 123 and 2026, this pattern used to trip none
    of the five detectors -- unique address and phone, no cycle, a sink
    account with zero betweenness, and a payment that matched its
    invoice to the peso and cited its uuid. A fraud that leaves no trace
    is a scenario the agent cannot solve, and a judge can pick it.

    An unreferenced payment is also the more realistic shape: a payment
    to an EFOS shell is exactly the one with no CFDI behind it. It makes
    invoice_payment_mismatch fire on the invoice ("no matching payment")
    and leaves a 250,000 MXN payment with nothing supporting it, which is
    the tell the SYSTEM_PROMPT already teaches the agent to look for.
    """
    shell_rfc = _rand_rfc(rng)
    shell = Company(
        rfc=shell_rfc, name=f"Servicios Integrales {shell_rfc[:4]} SA de CV",
        address="Av. Ficticia 000, Monterrey, NL", phone="8110000000",
        industry="Consultoria", incorporation_date=base_date - timedelta(days=90),
    )
    shell_account = BankAccount(account_id=f"acc-{shell_rfc}", clabe=_rand_clabe(rng), bank_name="BBVA")
    inv = Invoice(
        uuid=str(uuid.uuid4()), folio=f"F-{rng.randint(1000, 9999)}",
        emisor_rfc=shell_rfc, receptor_rfc=audited_rfc, amount=amount,
        date=base_date, uso_cfdi="G03", forma_pago="99", metodo_pago="PPD",
        concepts=[InvoiceConcept(description="Servicios de consultoria estrategica", amount=amount)],
    )
    pay = Payment(
        transaction_id=str(uuid.uuid4()), from_account=accounts_by_rfc[audited_rfc],
        to_account=shell_account.account_id, amount=amount,
        date=base_date + timedelta(days=15), reference=None,  # see docstring
    )
    return {"companies": [shell], "accounts": [shell_account], "invoices": [inv], "payments": [pay]}


def kickback_shell(audited_rfc: str, accounts_by_rfc: dict[str, str], rng: random.Random,
                    base_date: date, kickback_fraction: float = 0.3, amount: float = 400_000.0) -> dict:
    """A legit-looking supplier gets paid in full, then routes a cut
    back to a shell company that shares its address and phone."""
    supplier_rfc = _rand_rfc(rng)
    shell_rfc = _rand_rfc(rng)
    supplier = Company(
        rfc=supplier_rfc, name=f"Materiales del Norte {supplier_rfc[:4]} SA",
        address="Blvd. Industrial 123, Apodaca, NL", phone="8119990000",
        industry="Manufactura", incorporation_date=base_date - timedelta(days=800),
    )
    shell = Company(
        rfc=shell_rfc, name=f"Grupo {shell_rfc[:4]} Holdings SA de CV",
        address="Blvd. Industrial 123, Apodaca, NL",  # same address -> SHARES_ADDRESS
        phone="8119990000",  # same phone -> SHARES_PHONE
        industry="Servicios", incorporation_date=base_date - timedelta(days=60),
    )
    inv = Invoice(
        uuid=str(uuid.uuid4()), folio=f"F-{rng.randint(1000, 9999)}",
        emisor_rfc=supplier_rfc, receptor_rfc=audited_rfc, amount=amount,
        date=base_date, uso_cfdi="G01", forma_pago="03", metodo_pago="PUE",
        concepts=[InvoiceConcept(description="Materia prima", amount=amount)],
    )
    supplier_account = BankAccount(account_id=f"acc-{supplier_rfc}", clabe=_rand_clabe(rng), bank_name="Banorte")
    shell_account = BankAccount(account_id=f"acc-{shell_rfc}", clabe=_rand_clabe(rng), bank_name="Banorte")
    pay_full = Payment(transaction_id=str(uuid.uuid4()), from_account=accounts_by_rfc[audited_rfc],
                        to_account=supplier_account.account_id, amount=amount,
                        date=base_date + timedelta(days=10), reference=inv.uuid)
    pay_kickback = Payment(transaction_id=str(uuid.uuid4()), from_account=supplier_account.account_id,
                            to_account=shell_account.account_id,
                            amount=round(amount * kickback_fraction, 2),
                            date=base_date + timedelta(days=12), reference=None)
    return {
        "companies": [supplier, shell],
        "accounts": [supplier_account, shell_account],
        "invoices": [inv],
        "payments": [pay_full, pay_kickback],
    }


def round_tripping(audited_rfc: str, accounts_by_rfc: dict[str, str], rng: random.Random,
                    base_date: date, amount: float = 180_000.0, hops: int = 3) -> dict:
    """Money leaves the audited company and returns to it through a
    chain of intermediaries -- a payment cycle in the graph."""
    chain_rfcs = [_rand_rfc(rng) for _ in range(hops)]
    companies, accounts, payments = [], [], []
    for rfc in chain_rfcs:
        companies.append(Company(
            rfc=rfc, name=f"Comercializadora {rfc[:4]} SA de CV",
            address="Periferico 500, Garcia, NL", phone="8118880000",
            industry="Comercio", incorporation_date=base_date - timedelta(days=200),
        ))
        accounts.append(BankAccount(account_id=f"acc-{rfc}", clabe=_rand_clabe(rng),
                                     bank_name=rng.choice(["Banorte", "BBVA"])))

    hop_accounts = [accounts_by_rfc[audited_rfc]] + [a.account_id for a in accounts] + [accounts_by_rfc[audited_rfc]]
    d = base_date
    for i in range(len(hop_accounts) - 1):
        d = d + timedelta(days=5)
        payments.append(Payment(transaction_id=str(uuid.uuid4()), from_account=hop_accounts[i],
                                 to_account=hop_accounts[i + 1], amount=amount, date=d, reference=None))
    return {"companies": companies, "accounts": accounts, "invoices": [], "payments": payments}


def inflated_sales(audited_rfc: str, accounts_by_rfc: dict[str, str], rng: random.Random,
                    base_date: date, amount: float = 300_000.0) -> dict:
    """The audited company books a sale to a customer that never
    actually pays -- inflates revenue with no real cash movement."""
    customer_rfc = _rand_rfc(rng)
    customer = Company(
        rfc=customer_rfc, name=f"Distribuidora {customer_rfc[:4]} de Mexico SA",
        address="Carretera Nacional 700, Monterrey, NL", phone="8117770000",
        industry="Comercio", incorporation_date=base_date - timedelta(days=30),
    )
    inv = Invoice(
        uuid=str(uuid.uuid4()), folio=f"F-{rng.randint(1000, 9999)}",
        emisor_rfc=audited_rfc, receptor_rfc=customer_rfc, amount=amount,
        date=base_date, uso_cfdi="G01", forma_pago="99", metodo_pago="PPD",
        concepts=[InvoiceConcept(description="Venta de producto terminado", amount=amount)],
    )
    # deliberately no matching Payment -- this absence is the tell (FR-6)
    return {"companies": [customer], "accounts": [], "invoices": [inv], "payments": []}


PATTERNS = {
    "fake_billing": fake_billing,
    "kickback_shell": kickback_shell,
    "round_tripping": round_tripping,
    "inflated_sales": inflated_sales,
}
