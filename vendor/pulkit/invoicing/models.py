"""Pydantic schemas shared by the extraction, reconciliation and reporting layers.

The extraction models deliberately keep *evidence* next to every value: the
agent needs to see which line of the PDF produced a number so it can check the
extraction instead of trusting it.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Dict, List, Optional

from pydantic import BaseModel, Field

#: Reconciliation outcomes.
STATUS_MATCH = "MATCH"
STATUS_MISMATCH = "MISMATCH"
STATUS_REVIEW = "REVIEW_REQUIRED"

#: Extraction outcomes.
EXTRACTION_VERIFIED = "verified"
EXTRACTION_NEEDS_REVIEW = "needs_review"
EXTRACTION_FAILED = "failed"


class FieldValue(BaseModel):
    """A single extracted value together with where it came from."""

    value: Optional[str] = None
    evidence: Optional[str] = None
    page: Optional[int] = None
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)

    def present(self) -> bool:
        return self.value is not None and str(self.value).strip() != ""


class LineItem(BaseModel):
    """One billable line on an invoice."""

    description: Optional[str] = None
    detail: Optional[str] = None
    quantity: Optional[Decimal] = None
    unit_rate: Optional[Decimal] = None
    amount: Optional[Decimal] = None
    currency: Optional[str] = None
    page: Optional[int] = None
    #: True when ``quantity * unit_rate`` reproduces ``amount`` exactly.
    arithmetic_consistent: Optional[bool] = None
    #: Human readable reason when the arithmetic does not line up.
    arithmetic_note: Optional[str] = None


class InvoiceExtraction(BaseModel):
    """Structured view of one invoice PDF."""

    source_file: str
    source_sha256: Optional[str] = None
    page_count: int = 1
    vendor_name: FieldValue = Field(default_factory=FieldValue)
    customer_name: FieldValue = Field(default_factory=FieldValue)
    contractor_name: FieldValue = Field(default_factory=FieldValue)
    contractor_id: FieldValue = Field(default_factory=FieldValue)
    invoice_number: FieldValue = Field(default_factory=FieldValue)
    invoice_date: FieldValue = Field(default_factory=FieldValue)
    due_date: FieldValue = Field(default_factory=FieldValue)
    billing_period: FieldValue = Field(default_factory=FieldValue)
    period_year: Optional[int] = None
    period_month: Optional[int] = None
    currency: FieldValue = Field(default_factory=FieldValue)
    subtotal: FieldValue = Field(default_factory=FieldValue)
    tax: FieldValue = Field(default_factory=FieldValue)
    total: FieldValue = Field(default_factory=FieldValue)
    amount_due: FieldValue = Field(default_factory=FieldValue)
    line_items: List[LineItem] = Field(default_factory=list)
    extraction_status: str = EXTRACTION_NEEDS_REVIEW
    #: Problems found while extracting; a non-empty list forces REVIEW_REQUIRED.
    warnings: List[str] = Field(default_factory=list)

    def billed_quantity(self) -> Optional[Decimal]:
        """Total quantity across all lines."""
        values = [i.quantity for i in self.line_items if i.quantity is not None]
        return sum(values, Decimal("0")) if values else None

    def billed_amount(self) -> Optional[Decimal]:
        """Total line amount across all lines."""
        values = [i.amount for i in self.line_items if i.amount is not None]
        return sum(values, Decimal("0")) if values else None


class WorkOrderExtraction(BaseModel):
    """Structured view of one vendor work order PDF."""

    source_file: str
    source_sha256: Optional[str] = None
    page_count: int = 1
    work_order_ref: FieldValue = Field(default_factory=FieldValue)
    vendor_name: FieldValue = Field(default_factory=FieldValue)
    consultant_name: FieldValue = Field(default_factory=FieldValue)
    consultant_id: FieldValue = Field(default_factory=FieldValue)
    billing_rate: FieldValue = Field(default_factory=FieldValue)
    rate_unit: FieldValue = Field(default_factory=FieldValue)
    client_name: FieldValue = Field(default_factory=FieldValue)
    start_date: FieldValue = Field(default_factory=FieldValue)
    end_date: FieldValue = Field(default_factory=FieldValue)
    net_days: FieldValue = Field(default_factory=FieldValue)
    extraction_status: str = EXTRACTION_NEEDS_REVIEW
    warnings: List[str] = Field(default_factory=list)


class ContractorRef(BaseModel):
    """A contractor row from the master timesheet."""

    contractor_id: str
    first_name: str = ""
    last_name: str = ""
    full_name: str = ""
    vendor_name: str = ""

    @property
    def normalized_name(self) -> str:
        from .textutil import norm_name

        return norm_name(self.full_name)


class ReconciliationResult(BaseModel):
    """The outcome of reconciling one invoice against the reference data."""

    invoice_file: str
    invoice_number: Optional[str] = None
    vendor_name: Optional[str] = None
    contractor_id: Optional[str] = None
    contractor_name: Optional[str] = None
    period_label: Optional[str] = None
    period_year: Optional[int] = None
    period_month: Optional[int] = None
    status: str = STATUS_REVIEW
    #: The number the invoice was billed for, in the invoice currency.
    invoice_amount: Optional[Decimal] = None
    #: hours (from the timesheet) x rate (from the work order).
    expected_amount: Optional[Decimal] = None
    #: invoice_amount - expected_amount.
    difference: Optional[Decimal] = None
    expected_hours: Optional[Decimal] = None
    invoice_hours: Optional[Decimal] = None
    hours_difference: Optional[Decimal] = None
    expected_rate: Optional[Decimal] = None
    invoice_rate: Optional[Decimal] = None
    rate_difference: Optional[Decimal] = None
    currency: str = "USD"
    #: Ordered, human readable explanations of the outcome.
    reasons: List[str] = Field(default_factory=list)
    #: Non-fatal observations (duplicates, expired work order, ...).
    warnings: List[str] = Field(default_factory=list)
    #: Everything needed to audit the decision.
    audit: Dict = Field(default_factory=dict)
    created_at: dt.datetime = Field(default_factory=lambda: dt.datetime.now(dt.timezone.utc))


# --- APPEND-POINT-3 ---


class MarkdownLine(BaseModel):
    """One billable row read from the Markdown rendering of a document."""

    description: Optional[str] = None
    quantity: Optional[Decimal] = None
    unit_rate: Optional[Decimal] = None
    amount: Optional[Decimal] = None


class DocumentFacts(BaseModel):
    """What a *second* reading of the document says.

    The Markdown rendering is produced by a different layout analysis than
    the cell geometry the extractor uses.  Comparing the two is a cheap way
    to catch a misread: a number that the extractor read but the Markdown
    does not show -- or shows with a different value -- is a finding, not a
    detail.
    """

    source_file: str = ""
    #: ``subtotal`` / ``total`` / ``tax`` / ``amount_due`` -> amount.
    totals: Dict[str, Decimal] = Field(default_factory=dict)
    #: The billable rows found in the Markdown tables.
    lines: List[MarkdownLine] = Field(default_factory=list)
    #: Every number that appears in the rendering.
    numbers: List[Decimal] = Field(default_factory=list)
    #: True when the document was read as a rendered Markdown file.
    available: bool = False
    #: True when the rendering contained tables that could be read as rows.
    structured: bool = False

    def has_number(self, value: Optional[Decimal]) -> bool:
        """True when ``value`` appears somewhere in the rendering."""
        if value is None:
            return False
        for candidate in self.numbers:
            if abs(candidate - value) <= Decimal("0.005"):
                return True
        return False
