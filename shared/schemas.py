"""
Shared data contracts for The Forensic Auditor.

These Pydantic models are the single source of truth for the shapes
exchanged between modules (data/graph -> agent -> api -> ui) and over
the HTTP/SSE API described in the SRS, section 7.

Freeze changes here early and loudly -- every other module imports from
this file. If you need to change a field, say so in the team channel
before you do it.
"""
from __future__ import annotations

from datetime import date
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Graph entities (SRS section 4.1)
# ---------------------------------------------------------------------------

class BlacklistStatus(str, Enum):
    NONE = "none"
    PRESUNTO = "presunto"
    DEFINITIVO = "definitivo"
    DESVIRTUADO = "desvirtuado"
    SENTENCIA_FAVORABLE = "sentencia_favorable"


#: Article 69-B statuses that actually incriminate a taxpayer.
#: DESVIRTUADO and SENTENCIA_FAVORABLE mean the company disproved the
#: presumption or won in court -- 13.9% of the real SAT file. Treating
#: those as guilt would be a false accusation.
ACCUSABLE_BLACKLIST_STATUSES = frozenset({
    BlacklistStatus.PRESUNTO.value,
    BlacklistStatus.DEFINITIVO.value,
})


class Company(BaseModel):
    rfc: str
    name: str
    address: str
    phone: str
    industry: str
    incorporation_date: date
    is_audited_entity: bool = False
    blacklist_status: BlacklistStatus = BlacklistStatus.NONE


class Person(BaseModel):
    id: str
    name: str
    role: str  # "owner" | "legal_rep" | "employee"


class BankAccount(BaseModel):
    account_id: str
    clabe: str
    bank_name: str


class InvoiceConcept(BaseModel):
    description: str
    amount: float


class Invoice(BaseModel):
    uuid: str
    folio: str
    emisor_rfc: str
    receptor_rfc: str
    amount: float
    date: date
    uso_cfdi: str
    forma_pago: str
    metodo_pago: str
    concepts: list[InvoiceConcept] = Field(default_factory=list)


class Payment(BaseModel):
    transaction_id: str
    from_account: str
    to_account: str
    amount: float
    date: date
    reference: Optional[str] = None  # may point at an Invoice.uuid


class DataEstate(BaseModel):
    """Everything the data/generator module produces for one seed."""
    seed: int
    companies: list[Company]
    people: list[Person]
    accounts: list[BankAccount]
    invoices: list[Invoice]
    payments: list[Payment]


# ---------------------------------------------------------------------------
# Graph export (for /graph/export and the UI's base render)
# ---------------------------------------------------------------------------

class GraphNode(BaseModel):
    id: str
    type: str  # "Company" | "Person" | "BankAccount" | "Invoice" | "Payment"
    label: str
    attributes: dict = Field(default_factory=dict)


class GraphEdge(BaseModel):
    id: str
    source: str
    target: str
    type: str
    attributes: dict = Field(default_factory=dict)


class GraphExport(BaseModel):
    nodes: list[GraphNode]
    edges: list[GraphEdge]


# ---------------------------------------------------------------------------
# Detectors / leads (FR-5 - FR-10)
# ---------------------------------------------------------------------------

class Lead(BaseModel):
    id: str
    detector: str  # e.g. "blacklist_match", "cycle", "shared_attribute_cluster", ...
    entity_ids: list[str]
    reason: str
    supporting_edge_ids: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Investigation / agent (FR-11 - FR-19)
# ---------------------------------------------------------------------------

class InvestigationStepType(str, Enum):
    THOUGHT = "thought"
    ACTION = "action"
    OBSERVATION = "observation"
    LEAD_DROPPED = "lead_dropped"
    CONCLUSION = "conclusion"


class InvestigationStep(BaseModel):
    investigation_id: str
    step_index: int
    type: InvestigationStepType
    content: str
    referenced_ids: list[str] = Field(default_factory=list)


class AccusationClaim(BaseModel):
    supplier_rfc: str
    rule_broken: str
    peso_amount: float
    evidence_edge_ids: list[str]  # must be non-empty -- enforced by agent/guardrail.py


class DroppedLead(BaseModel):
    entity_ids: list[str]
    reason: str


class CaseFile(BaseModel):
    investigation_id: str
    scheme_narrative: str
    implicated_suppliers: list[AccusationClaim]
    evidence_trail: GraphExport  # the extracted subgraph
    leads_not_pursued: list[DroppedLead]
    total_amount_at_risk: float


# ---------------------------------------------------------------------------
# API request/response payloads (SRS section 7)
# ---------------------------------------------------------------------------

class GenerateEstateRequest(BaseModel):
    seed: int = 42
    num_suppliers: int = 20
    num_blacklisted: int = 3


class InjectScenarioRequest(BaseModel):
    pattern: str  # "fake_billing" | "kickback_shell" | "round_tripping" | "inflated_sales"
    params: dict = Field(default_factory=dict)


class InvestigateRequest(BaseModel):
    hint: str


class AskRequest(BaseModel):
    question: str


class AskResponse(BaseModel):
    answer: str
    referenced_ids: list[str] = Field(default_factory=list)
