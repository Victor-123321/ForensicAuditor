"""
SQL/Cortex-backed detectors (docs/snowflake-integracion.md). Each
function returns list[Lead] -- the exact same contract as
graph/detectors.py's local detectors -- by running the equivalent logic
as a query against the Snowflake warehouse instead of NetworkX. Only
used when DATA_SOURCE=snowflake; DATA_SOURCE=local never imports this
module. See api/main.py::_build_graph_for_state() for the wiring.
"""
from __future__ import annotations

import json
import uuid
import warnings

import networkx as nx

from data.snowflake_client import (
    DEFAULT_CORTEX_MODEL,
    SnowflakeError,
    cortex_complete,
    execute_sql,
    sql_string_literal,
)
from graph.builder import build_graph
from shared.schemas import DataEstate, Lead


def detect_blacklisted_suppliers_sql() -> list[Lead]:
    """FR-5's SQL twin.

    FILTRO CRITICO: only 'Definitivo' and 'Presunto' incriminate a
    taxpayer under Art. 69-B. 'Desvirtuado' means the company
    successfully rebutted SAT's presumption -- SAT itself exonerated
    them -- and 'Sentencia Favorable' means they won in court. Treating
    either as guilt would be exactly the false-positive the Judgment
    criterion punishes (this mirrors ACCUSABLE_BLACKLIST_STATUSES in
    shared/schemas.py, which the local detect_blacklist_matches() uses
    for the same reason).
    """
    rows = execute_sql("""
        SELECT s.SUPPLIER_ID, s.RFC, b.STATUS
        FROM SUPPLIERS s
        JOIN SAT_BLACKLIST b ON UPPER(TRIM(s.RFC)) = UPPER(TRIM(b.RFC))
        WHERE UPPER(TRIM(b.STATUS)) IN ('DEFINITIVO', 'PRESUNTO')
    """)
    return [
        Lead(id=str(uuid.uuid4()), detector="blacklist_match_sql",
             entity_ids=[row["SUPPLIER_ID"]],
             reason=f"{row['RFC']} appears on SAT's Article 69-B list with status '{row['STATUS']}'")
        for row in rows
    ]


def detect_invoice_payment_mismatch_sql() -> list[Lead]:
    """FR-6's SQL twin: LEFT JOIN so an invoice with zero payments still
    surfaces (COALESCE(SUM(...), 0)), aggregated per invoice, flagged
    when the absolute gap exceeds a cent."""
    rows = execute_sql("""
        SELECT i.INVOICE_ID, i.SUPPLIER_ID, i.AMOUNT AS INVOICE_AMOUNT,
               COALESCE(SUM(p.AMOUNT), 0) AS PAID
        FROM INVOICES i
        LEFT JOIN PAYMENTS p ON p.INVOICE_ID = i.INVOICE_ID
        GROUP BY i.INVOICE_ID, i.SUPPLIER_ID, i.AMOUNT
        HAVING ABS(i.AMOUNT - COALESCE(SUM(p.AMOUNT), 0)) > 0.01
    """)
    return [
        Lead(id=str(uuid.uuid4()), detector="invoice_payment_mismatch_sql",
             entity_ids=[row["INVOICE_ID"]],
             reason=(f"Invoice {row['INVOICE_ID']} (supplier {row['SUPPLIER_ID']}) amount "
                     f"{row['INVOICE_AMOUNT']} vs. paid {row['PAID']} -- mismatch"))
        for row in rows
    ]


def detect_shared_attributes_sql() -> list[Lead]:
    """FR-8's SQL twin: phone / address / bank_account grouped
    separately (UNION ALL) so a match on one attribute doesn't hide in a
    multi-column GROUP BY, each with its own ARRAY_AGG of supplier_ids.
    Nulls and empty strings are excluded -- two suppliers both missing a
    phone number is not a shared attribute."""
    rows = execute_sql("""
        SELECT 'phone' AS ATTR_TYPE, PHONE AS ATTR_VALUE, ARRAY_AGG(SUPPLIER_ID) AS IDS
        FROM SUPPLIERS WHERE PHONE IS NOT NULL AND PHONE != ''
        GROUP BY PHONE HAVING COUNT(*) > 1
        UNION ALL
        SELECT 'address', ADDRESS, ARRAY_AGG(SUPPLIER_ID)
        FROM SUPPLIERS WHERE ADDRESS IS NOT NULL AND ADDRESS != ''
        GROUP BY ADDRESS HAVING COUNT(*) > 1
        UNION ALL
        SELECT 'bank_account', BANK_ACCOUNT, ARRAY_AGG(SUPPLIER_ID)
        FROM SUPPLIERS WHERE BANK_ACCOUNT IS NOT NULL AND BANK_ACCOUNT != ''
        GROUP BY BANK_ACCOUNT HAVING COUNT(*) > 1
    """)
    leads = []
    for row in rows:
        ids = _parse_array(row.get("IDS"))
        if len(ids) < 2:
            continue
        leads.append(Lead(
            id=str(uuid.uuid4()), detector="shared_attribute_cluster_sql",
            entity_ids=sorted(ids),
            reason=(f"{len(ids)} suppliers share the same {row['ATTR_TYPE']} "
                    f"('{row['ATTR_VALUE']}') -- possible shell-company cluster"),
        ))
    return leads


def _parse_array(value) -> list[str]:
    """ARRAY_AGG comes back through the REST API as a JSON-array string
    (every value from execute_sql() is a string unless the column type
    is fixed/real -- see data/snowflake_client.py::_rows_to_dicts)."""
    if value is None:
        return []
    if isinstance(value, list):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return []


_VAGUE_PROMPT_PREFIX = (
    "Clasifica el siguiente concepto de una factura mexicana como VAGO o "
    "ESPECIFICO. Responde con UNA sola palabra en mayusculas: VAGO o "
    "ESPECIFICO, sin explicacion adicional. Un concepto es VAGO si "
    "describe un servicio generico e inverificable sin entregable "
    "concreto (ejemplos: 'servicios de consultoria', 'asesoria "
    "administrativa diversa', 'servicios profesionales varios'). Es "
    "ESPECIFICO si nombra un bien, cantidad, periodo o entregable "
    "identificable. Concepto: "
)

#: Cortex SQL functions to probe for, newest name first -- an account
#: may expose either depending on when it was provisioned. Never assume.
_CORTEX_SQL_FUNCTIONS = ("AI_COMPLETE", "SNOWFLAKE.CORTEX.COMPLETE")


def _cortex_sql_function_name() -> str | None:
    """Runs a one-token probe query against each candidate function name
    and returns the first that works, or None if the account exposes
    neither (Plan B: per-concept cortex_complete() calls instead)."""
    for fn in _CORTEX_SQL_FUNCTIONS:
        try:
            execute_sql(f"SELECT {fn}('{DEFAULT_CORTEX_MODEL}', 'ping') AS PROBE")
            return fn
        except SnowflakeError:
            continue
    return None


def detect_vague_concepts_cortex() -> list[Lead]:
    """The language detector neither SQL nor the graph can do: classify
    what an invoice's concepto field actually SAYS.

    OPTIMIZATION (required): classifies SELECT DISTINCT concepto, not
    every row -- ~150 invoices but typically 15-25 distinct concepts, so
    this is 15-25 model calls (or one query, in the SQL-function path),
    never 150.
    """
    fn_name = _cortex_sql_function_name()
    labels: dict[str, str] = {}

    if fn_name:
        # Single in-warehouse query: build the full prompt per row with
        # SQL string concatenation so the model never leaves Snowflake.
        rows = execute_sql(f"""
            SELECT CONCEPTO,
                   UPPER(TRIM({fn_name}('{DEFAULT_CORTEX_MODEL}',
                       {sql_string_literal(_VAGUE_PROMPT_PREFIX)} || CONCEPTO))) AS LABEL
            FROM (SELECT DISTINCT CONCEPTO FROM INVOICES
                  WHERE CONCEPTO IS NOT NULL AND CONCEPTO != '')
        """)
        labels = {row["CONCEPTO"]: (row.get("LABEL") or "") for row in rows}
    else:
        # Plan B: neither Cortex SQL function is available on this
        # account -- classify the distinct concepts one at a time via
        # the REST completion endpoint (data/snowflake_client.py).
        concept_rows = execute_sql(
            "SELECT DISTINCT CONCEPTO FROM INVOICES WHERE CONCEPTO IS NOT NULL AND CONCEPTO != ''")
        for row in concept_rows:
            concepto = row["CONCEPTO"]
            try:
                answer = cortex_complete(_VAGUE_PROMPT_PREFIX + concepto)
            except SnowflakeError as exc:
                warnings.warn(f"cortex_complete failed for one concepto, skipping it: {exc}",
                              RuntimeWarning, stacklevel=2)
                continue
            labels[concepto] = answer.strip().upper()

    vague_concepts = [c for c, label in labels.items() if "VAGO" in label]
    if not vague_concepts:
        return []

    in_clause = ", ".join(sql_string_literal(c) for c in vague_concepts)
    rows = execute_sql(f"""
        SELECT SUPPLIER_ID, ARRAY_AGG(INVOICE_ID) AS INVOICE_IDS
        FROM INVOICES
        WHERE CONCEPTO IN ({in_clause})
        GROUP BY SUPPLIER_ID
    """)

    leads = []
    for row in rows:
        invoice_ids = _parse_array(row.get("INVOICE_IDS"))
        if not invoice_ids:
            continue
        leads.append(Lead(
            id=str(uuid.uuid4()), detector="vague_concept_cortex",
            entity_ids=[row["SUPPLIER_ID"]], supporting_edge_ids=[],
            reason=(f"{row['SUPPLIER_ID']} has {len(invoice_ids)} invoice(s) with a vague, "
                    "unverifiable concepto (no concrete deliverable), classified by Cortex"),
        ))
    return leads


def run_all_sql() -> list[Lead]:
    """The Snowflake-side equivalent of graph.detectors.run_all(): every
    SQL/Cortex detector, combined. A Cortex failure (model unavailable
    in this region, etc.) is logged and skipped rather than killing the
    other 3 detectors -- same "never a single point of failure" rule
    that governs the DATA_SOURCE flag one level up."""
    leads = []
    leads.extend(detect_blacklisted_suppliers_sql())
    leads.extend(detect_invoice_payment_mismatch_sql())
    leads.extend(detect_shared_attributes_sql())
    try:
        leads.extend(detect_vague_concepts_cortex())
    except SnowflakeError as exc:
        warnings.warn(f"detect_vague_concepts_cortex failed, skipping it: {exc}",
                      RuntimeWarning, stacklevel=2)
    return leads


def _reduce_to_neighborhood(g: nx.MultiDiGraph, entity_ids: set[str]) -> nx.MultiDiGraph:
    """Keeps only the given nodes plus their direct (1-hop) neighbors in
    either direction, with every edge between any two kept nodes."""
    keep = set(entity_ids) & set(g.nodes)
    for node in list(keep):
        keep.update(g.predecessors(node))
        keep.update(g.successors(node))
    return g.subgraph(keep).copy()


def _ensure_blacklist_loaded() -> None:
    """SAT_BLACKLIST is a real-world reference table (~14k rows) that
    doesn't change per estate -- reloading it on every /estate/generate
    or Scenario Injector call (Step 6) would cost ~100s+ for no reason.
    Loads it once, the first time this account's table is empty, and
    never again after that."""
    from data.snowflake_loader import ensure_schema, load_sat_blacklist  # local import: only needed here

    ensure_schema()  # idempotent -- guarantees SAT_BLACKLIST exists before COUNT(*)
    rows = execute_sql("SELECT COUNT(*) AS N FROM SAT_BLACKLIST")
    if rows and rows[0].get("N", 0) > 0:
        return
    load_sat_blacklist()


def build_reduced_graph_from_snowflake(estate: DataEstate) -> nx.MultiDiGraph:
    """DATA_SOURCE=snowflake's graph-construction path (Step 5,
    docs/snowflake-integracion.md): load `estate` into the warehouse
    (data/snowflake_loader.py), run the 4 SQL/Cortex detectors there,
    and build a NetworkX graph containing only the flagged entities and
    their direct neighbors -- not the whole estate. The graph-native
    detectors (cycles, betweenness centrality -- graph/detectors.py)
    then run over this SAME reduced graph unchanged, exactly as they do
    today, via agent/tools.py::run_detector()."""
    from data.snowflake_loader import load_estate  # local import: only needed on this path

    _ensure_blacklist_loaded()
    load_estate(estate)
    leads = run_all_sql()

    full_graph = build_graph(estate)  # still needed for real node/edge attributes
    suspicious_ids = {eid for lead in leads for eid in lead.entity_ids}
    return _reduce_to_neighborhood(full_graph, suspicious_ids)
