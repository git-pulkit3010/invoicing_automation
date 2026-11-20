"""Domain models shared across extractors, the reconciler and the reporter."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Optional


class Status(str, Enum):
    """Outcome of a single reconciliation check."""

    PASS = "PASS"
    FAIL = "FAIL"
    WARN = "WARN"
    INFO = "INFO"
    SKIP = "SKIP"

    @property
    def rank(self) -> int:
        return {"FAIL": 0, "WARN": 1, "PASS": 2, "INFO": 3, "SKIP": 4}[self.value]


@dataclass
class WeekBucket:
    """One Sun-Sat pay week of recorded time."""

    week_start: date
    week_end: date
    days: int
    hours: Decimal


@dataclass
class ContractorHours:
    """Aggregated timesheet hours for one contractor in the report period."""

    attuid: str
    first_name: str
    last_name: str
    full_name: str
    vendor_name: str
    supervisor_attuid: str
    segment_id: str
    segment_name: str
    period_start: date
    period_end: date
    regular_hours: Decimal
    ot_hours: Decimal
    days_recorded: int
    weeks: list[WeekBucket] = field(default_factory=list)
    missing_weekdays: list[int] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)

    @property
    def total_hours(self) -> Decimal:
        return self.regular_hours + self.ot_hours

    @property
    def full_month_benchmark(self) -> Decimal:
        """Hours a contractor would log working every weekday of the period."""
        weekdays = 0
        d = self.period_start
        while d <= self.period_end:
            if d.weekday() < 5:
                weekdays += 1
            d = d.fromordinal(d.toordinal() + 1)
        return Decimal(weekdays * 8)


@dataclass
class InvoiceLine:
    """A single billed line on an invoice."""

    description: str
    contractor_name: Optional[str]
    quantity: Decimal
    rate: Decimal
    amount: Decimal
    currency: str
    source_row: str
    notes: list[str] = field(default_factory=list)


@dataclass
class Invoice:
    """Parsed invoice document."""

    path: Path
    vendor_name: Optional[str] = None
    invoice_number: Optional[str] = None
    invoice_date: Optional[date] = None
    due_date: Optional[date] = None
    terms: Optional[str] = None
    period_start: Optional[date] = None
    period_end: Optional[date] = None
    lines: list[InvoiceLine] = field(default_factory=list)
    total_amount: Optional[Decimal] = None
    flags: list[str] = field(default_factory=list)
    #: Client / contractor / period encoded in the file name convention.
    filename_client: Optional[str] = None
    filename_person: Optional[str] = None
    filename_period_start: Optional[date] = None
    filename_period_end: Optional[date] = None


@dataclass
class WorkOrder:
    """Parsed vendor work order (authorised engagement + billing rate)."""

    path: Path
    vendor_name: Optional[str] = None
    consultant_name: Optional[str] = None
    currency: Optional[str] = None
    rate: Optional[Decimal] = None
    rate_basis: Optional[str] = None
    client_name: Optional[str] = None
    auth_start: Optional[date] = None
    est_end: Optional[date] = None
    work_order_number: Optional[str] = None
    work_order_ref: Optional[str] = None
    status: Optional[str] = None
    created: Optional[date] = None
    flags: list[str] = field(default_factory=list)


@dataclass
class Check:
    """One assertion evaluated during reconciliation."""

    key: str
    label: str
    status: Status
    expected: str = ""
    actual: str = ""
    detail: str = ""


@dataclass
class Reconciliation:
    """The reconciled result for one invoice line."""

    invoice: Invoice
    line: InvoiceLine
    contractor: Optional[ContractorHours] = None
    work_order: Optional[WorkOrder] = None
    checks: list[Check] = field(default_factory=list)

    @property
    def status(self) -> Status:
        """Overall outcome.

        Only a FAIL downgrades the reconciliation: WARN checks are
        advisories (a gap in the timesheet, a work order that was never
        marked 'Verified') rather than a billing discrepancy.
        """
        if any(c.status is Status.FAIL for c in self.checks):
            return Status.FAIL
        return Status.PASS

    @property
    def failed(self) -> list[Check]:
        return [c for c in self.checks if c.status is Status.FAIL]

    @property
    def warnings(self) -> list[Check]:
        return [c for c in self.checks if c.status is Status.WARN]
