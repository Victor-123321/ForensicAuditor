"""
Loads a generated DataEstate -- the same object data/generator/ produces
for DATA_SOURCE=local -- and the real SAT blacklist into the Snowflake
tables graph/sql_detectors.py queries. Only used when DATA_SOURCE=snowflake;
see docs/snowflake-integracion.md.
"""
from __future__ import annotations

import csv

from data.generator.download_sat_blacklist import CACHE_PATH
from data.snowflake_client import execute_sql, sql_string_literal
from shared.schemas import DataEstate

#: Rows per INSERT statement. ~150 invoices/payments at our demo scale
#: means one or two round-trips per table, not hundreds.
_INSERT_BATCH_SIZE = 200


def ensure_schema() -> None:
    """Creates the 4 tables this integration needs if they don't already
    exist. Idempotent -- safe to call before every load."""
    execute_sql("""
        CREATE TABLE IF NOT EXISTS SUPPLIERS (
            supplier_id STRING, name STRING, rfc STRING, phone STRING,
            address STRING, bank_account STRING, incorporation_date DATE
        )
    """)
    execute_sql("""
        CREATE TABLE IF NOT EXISTS INVOICES (
            invoice_id STRING, supplier_id STRING, issue_date DATE,
            amount FLOAT, concepto STRING, uuid_cfdi STRING
        )
    """)
    execute_sql("""
        CREATE TABLE IF NOT EXISTS PAYMENTS (
            payment_id STRING, invoice_id STRING, supplier_id STRING,
            payment_date DATE, amount FLOAT, bank_account STRING
        )
    """)
    execute_sql("""
        CREATE TABLE IF NOT EXISTS SAT_BLACKLIST (
            rfc STRING, name STRING, status STRING, publication_date STRING
        )
    """)


def _num(value) -> str:
    return "NULL" if value is None else str(value)


def _date(value) -> str:
    return "NULL" if value is None else sql_string_literal(value.isoformat())


def _batch_insert(table: str, columns: list[str], rows: list[tuple[str, ...]],
                   batch_size: int = _INSERT_BATCH_SIZE) -> None:
    if not rows:
        return
    col_list = ", ".join(columns)
    for start in range(0, len(rows), batch_size):
        chunk = rows[start:start + batch_size]
        values_sql = ", ".join(f"({', '.join(row)})" for row in chunk)
        execute_sql(f"INSERT INTO {table} ({col_list}) VALUES {values_sql}")


def _audited_rfc(estate: DataEstate) -> str | None:
    return next((c.rfc for c in estate.companies if c.is_audited_entity), None)


def load_estate(estate: DataEstate) -> None:
    """Truncates and reloads SUPPLIERS/INVOICES/PAYMENTS from `estate`.
    TRUNCATE first, always -- regenerating the estate or injecting a
    fresh scenario (Scenario Injector, Step 6) must never duplicate rows
    on top of the previous load."""
    ensure_schema()
    execute_sql("TRUNCATE TABLE SUPPLIERS")
    execute_sql("TRUNCATE TABLE INVOICES")
    execute_sql("TRUNCATE TABLE PAYMENTS")

    # account_id -> owning RFC, from the "acc-<rfc>" convention
    # graph/builder.py already relies on.
    account_owner = {a.account_id: a.account_id.replace("acc-", "", 1) for a in estate.accounts}
    company_account = {rfc: acc_id for acc_id, rfc in account_owner.items()}

    supplier_rows = [
        (sql_string_literal(c.rfc), sql_string_literal(c.name), sql_string_literal(c.rfc),
         sql_string_literal(c.phone), sql_string_literal(c.address),
         sql_string_literal(company_account.get(c.rfc)), _date(c.incorporation_date))
        for c in estate.companies
    ]
    _batch_insert("SUPPLIERS",
                  ["supplier_id", "name", "rfc", "phone", "address", "bank_account", "incorporation_date"],
                  supplier_rows)

    invoice_rows = [
        (sql_string_literal(inv.uuid), sql_string_literal(inv.emisor_rfc), _date(inv.date),
         _num(inv.amount), sql_string_literal(inv.concepts[0].description if inv.concepts else ""),
         sql_string_literal(inv.uuid))
        for inv in estate.invoices
    ]
    _batch_insert("INVOICES", ["invoice_id", "supplier_id", "issue_date", "amount", "concepto", "uuid_cfdi"],
                  invoice_rows)

    audited_rfc = _audited_rfc(estate)
    payment_rows = []
    for pay in estate.payments:
        to_owner = account_owner.get(pay.to_account)
        from_owner = account_owner.get(pay.from_account)
        # "supplier side" of a payment: whichever end is NOT the audited
        # company's own account -- covers a normal payment TO a supplier
        # and a kickback FROM one supplier-looking account to another.
        supplier_id = to_owner if to_owner and to_owner != audited_rfc else from_owner
        payment_rows.append((
            sql_string_literal(pay.transaction_id), sql_string_literal(pay.reference),
            sql_string_literal(supplier_id), _date(pay.date), _num(pay.amount),
            sql_string_literal(pay.to_account),
        ))
    _batch_insert("PAYMENTS", ["payment_id", "invoice_id", "supplier_id", "payment_date", "amount", "bank_account"],
                  payment_rows)


#: Which "Publicacion pagina SAT ..." column (0-based index in the raw
#: CSV) matches each status, so publication_date reflects the row's
#: actual current status rather than an arbitrary column. Checked in
#: this order because a row can carry dates for more than one historical
#: stage; the current status wins.
_PUBLICATION_COLUMN_BY_STATUS_KEYWORD = (
    ("sentencia favorable", 17),
    ("definitivo", 13),
    ("desvirtuado", 9),
    ("presunto", 5),
)


def _read_raw_blacklist_rows() -> list[dict]:
    """Reads data/raw/sat_69b.csv directly (same cache file and column
    layout data/generator/download_sat_blacklist.py already confirmed --
    skip 3 header rows, RFC in column 1, status in column 3), but keeps
    the status text exactly as SAT wrote it ("Definitivo", "Presunto",
    "Desvirtuado", "Sentencia Favorable") instead of that module's
    normalized lowercase enum: the SQL detector filters on SAT's own
    wording, not ours."""
    if not CACHE_PATH.exists():
        raise FileNotFoundError(
            f"{CACHE_PATH} not found -- run `python -m data.generator.download_sat_blacklist` first")

    with open(CACHE_PATH, encoding="latin-1", newline="") as f:
        raw_rows = list(csv.reader(f))

    result = []
    for row in raw_rows[3:]:
        if len(row) <= 3 or not row[1].strip():
            continue
        rfc, name, status = row[1].strip().upper(), row[2].strip(), row[3].strip()
        publication_date = ""
        status_lower = status.lower()
        for keyword, col in _PUBLICATION_COLUMN_BY_STATUS_KEYWORD:
            if keyword in status_lower and col < len(row):
                publication_date = row[col].strip()
                break
        result.append({"rfc": rfc, "name": name, "status": status, "publication_date": publication_date})
    return result


def load_sat_blacklist(rows: list[dict] | None = None) -> None:
    """Loads the real SAT 69-B list into SAT_BLACKLIST. `rows` lets
    tests/callers pass a small fixture directly (each dict: rfc, name,
    status, publication_date); without it, reads the cached CSV via
    _read_raw_blacklist_rows(). The status column is NOT filtered here
    -- graph/sql_detectors.py::detect_blacklisted_suppliers_sql() is
    where 'Desvirtuado'/'Sentencia Favorable' get excluded."""
    ensure_schema()
    if rows is None:
        rows = _read_raw_blacklist_rows()

    execute_sql("TRUNCATE TABLE SAT_BLACKLIST")
    blacklist_rows = [
        (sql_string_literal(r["rfc"]), sql_string_literal(r.get("name")),
         sql_string_literal(r.get("status")), sql_string_literal(r.get("publication_date")))
        for r in rows
    ]
    _batch_insert("SAT_BLACKLIST", ["rfc", "name", "status", "publication_date"], blacklist_rows)
