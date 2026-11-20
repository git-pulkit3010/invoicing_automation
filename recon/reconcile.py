"""Reconciliation engine.

Brings the three independent sources together and evaluates a fixed set of
assertions per invoice line:

    invoice line item      hours, rate and amount as billed
    ATT Time Report        hours actually worked
    vendor Work Order      the authorised hourly rate and engagement window

The headline assertion is the one the business cares about:

    hours (time report) x rate (work order) == amount (invoice)

Everything else exists to explain *why* that fails when it does.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Optional

from .config import ReconConfig
from .extract.invoice import parse_invoice
from .extract.timesheet import TimesheetReport, load_timesheet
from .extract.workorder import parse_work_order
from .models import Check, ContractorHours, Invoice, Reconciliation, Status, WorkOrder
from .names import NameRoster, similarity


@dataclass
class RunResult:
    """Everything one reconciliation run produced."""

    report: Optional[TimesheetReport] = None
    reconciliations: list[Reconciliation] = field(default_factory=list)
    invoices: list[Invoice] = field(default_factory=list)
    work_orders: list[WorkOrder] = field(default_factory=list)
    unlinked_work_orders: list[WorkOrder] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def status(self) -> Status:
        if self.errors:
            return Status.FAIL
        if not self.reconciliations:
            return Status.WARN
        return min((r.status for r in self.reconciliations), key=lambda s: s.rank)

    @property
    def passed(self) -> int:
        return sum(1 for r in self.reconciliations if r.status is Status.PASS)

    @property
    def failed(self) -> int:
        return sum(1 for r in self.reconciliations if r.status is Status.FAIL)

    @property
    def warned(self) -> int:
        """Number of advisory (WARN) findings across all invoice lines."""
        return sum(len(r.warnings) for r in self.reconciliations)


class Reconciler:
    """Runs the end-to-end reconciliation for a set of invoices."""

    def __init__(self, config: ReconConfig) -> None:
        self.config = config
        self._report: Optional[TimesheetReport] = None

    # -- loading ---------------------------------------------------------

    def load(self) -> tuple[TimesheetReport, list[WorkOrder]]:
        report = load_timesheet(
            self.config.excel,
            include_out_of_period_rows=self.config.include_out_of_period_rows,
        )
        work_orders = []
        for path in sorted(self.config.work_orders_dir.glob("*.pdf")):
            work_orders.append(parse_work_order(path))
        return report, work_orders

    # -- matching --------------------------------------------------------

    @staticmethod
    def build_roster(report: TimesheetReport) -> NameRoster:
        roster = NameRoster()
        for contractor in report.contractors.values():
            roster.add(contractor.full_name, contractor.attuid)
        return roster

    @staticmethod
    def match_work_order(work_orders: list[WorkOrder], name: Optional[str]) -> Optional[WorkOrder]:
        """Find the work order whose consultant matches the name exactly.

        Token-set equality keeps "Chaitanya Motupalli" away from
        "Chaitanya Kumar Nara".
        """
        if not name:
            return None
        roster = NameRoster()
        for work_order in work_orders:
            if work_order.consultant_name:
                roster.add(work_order.consultant_name, str(work_order.path))
        key = roster.match(name)
        if key is None:
            return None
        return next((w for w in work_orders if str(w.path) == key), None)

    # -- running ---------------------------------------------------------

    def run(self) -> RunResult:
        result = RunResult()
        try:
            report, work_orders = self.load()
        except Exception as exc:  # pragma: no cover - surfaced to the CLI
            result.errors.append(f"could not load timesheet: {exc}")
            return result

        self._report = report
        result.report = report
        result.work_orders = work_orders
        roster = self.build_roster(report)

        used: set[Path] = set()
        for path in sorted(self.config.invoices_dir.glob("*.pdf")):
            try:
                invoice = parse_invoice(path, roster=roster)
            except Exception as exc:
                result.errors.append(f"{path.name}: could not parse invoice ({exc})")
                continue
            result.invoices.append(invoice)
            if not invoice.lines:
                result.errors.append(
                    f"{path.name}: no billable line item could be identified"
                )
                continue

            for line in invoice.lines:
                contractor = self._resolve_contractor(roster, line.contractor_name)
                work_order = self.match_work_order(work_orders, line.contractor_name)
                vendor_fallback = False
                if work_order is None:
                    work_order = self._match_work_order_by_vendor(work_orders, invoice)
                    vendor_fallback = work_order is not None
                if work_order is not None:
                    used.add(work_order.path)

                reconciliation = Reconciliation(
                    invoice=invoice, line=line, contractor=contractor, work_order=work_order
                )
                reconciliation.checks = self._evaluate(invoice, line, contractor, work_order)
                if vendor_fallback:
                    reconciliation.checks.append(Check(
                        key="work_order_vendor_fallback",
                        label="Work order matched by vendor only",
                        status=Status.WARN,
                        expected="consultant name match",
                        actual=work_order.path.name,
                        detail="confirm this work order belongs to the invoiced consultant",
                    ))
                result.reconciliations.append(reconciliation)

        result.unlinked_work_orders = [w for w in work_orders if w.path not in used]
        if not result.reconciliations:
            result.errors.append("no invoice lines were reconciled")
        return result

    # -- helpers ---------------------------------------------------------

    def _resolve_contractor(
        self, roster: NameRoster, name: Optional[str]
    ) -> Optional[ContractorHours]:
        if not name or self._report is None:
            return None
        attuid = roster.match(name)
        if attuid is None:
            return None
        return self._report.contractors.get(attuid)

    def _match_work_order_by_vendor(
        self, work_orders: list[WorkOrder], invoice: Invoice
    ) -> Optional[WorkOrder]:
        """Fall back to matching a work order by client or vendor name.

        The file name carries the client ('Edge Centric'); the work order
        carries the legal vendor ('Edge Centric LLC'). Token overlap links
        the two.
        """
        client = invoice.filename_client or invoice.vendor_name
        if not client:
            return None
        candidates: list[WorkOrder] = []
        for work_order in work_orders:
            # A named order for someone else cannot authorize this invoice.
            if work_order.consultant_name:
                continue
            for label in (work_order.vendor_name, work_order.client_name):
                if not label:
                    continue
                score = similarity(client, label)
                if score >= 0.5:
                    candidates.append(work_order)
                    break
        return candidates[0] if len(candidates) == 1 else None

    # -- the checks ------------------------------------------------------

    def _evaluate(
        self,
        invoice: Invoice,
        line,
        contractor: Optional[ContractorHours],
        work_order: Optional[WorkOrder],
    ) -> list[Check]:
        checks: list[Check] = []
        cfg = self.config

        # 1. The invoice must be internally consistent.
        computed = line.quantity * line.rate
        if self._close(computed, line.amount, cfg.amount_tolerance):
            checks.append(Check(
                key="invoice_math",
                label="Invoice internal math (quantity x rate = amount)",
                status=Status.PASS,
                expected=_num(line.amount),
                actual=_num(computed),
                detail=f"{_num(line.quantity)} x {_num(line.rate)} = {_num(computed)}",
            ))
        else:
            checks.append(Check(
                key="invoice_math",
                label="Invoice internal math (quantity x rate = amount)",
                status=Status.FAIL,
                expected=_num(line.amount),
                actual=_num(computed),
                detail=(
                    f"quantity {_num(line.quantity)} x rate {_num(line.rate)} "
                    f"= {_num(computed)}, invoice bills {_num(line.amount)}"
                ),
            ))

        # 2. The billed hours must match the time report.
        if contractor is None:
            checks.append(Check(
                key="hours_match",
                label="Billed hours match the ATT Time Report",
                status=Status.FAIL,
                expected="a contractor in the time report",
                actual=line.contractor_name or "<unnamed>",
                detail="the invoiced person could not be found in the time report",
            ))
        else:
            difference = line.quantity - contractor.total_hours
            if abs(difference) <= cfg.hours_tolerance:
                checks.append(Check(
                    key="hours_match",
                    label="Billed hours match the ATT Time Report",
                    status=Status.PASS,
                    expected=f"{_num(contractor.total_hours)} hrs",
                    actual=f"{_num(line.quantity)} hrs",
                    detail=(
                        f"{contractor.full_name} ({contractor.attuid}) recorded "
                        f"{contractor.days_recorded} day(s)"
                    ),
                ))
            else:
                checks.append(Check(
                    key="hours_match",
                    label="Billed hours match the ATT Time Report",
                    status=Status.FAIL,
                    expected=f"{_num(contractor.total_hours)} hrs",
                    actual=f"{_num(line.quantity)} hrs",
                    detail=(
                        f"invoice bills {_num(line.quantity)} hrs but the time report shows "
                        f"{_num(contractor.total_hours)} hrs "
                        f"(difference {_num(difference)} hrs)"
                    ),
                ))

            if contractor.missing_weekdays:
                checks.append(Check(
                    key="missing_weekdays",
                    label="Time report coverage",
                    status=Status.WARN,
                    expected=f"{len(contractor.missing_weekdays)} missing weekday(s)",
                    actual=", ".join(f"Oct {d}" for d in contractor.missing_weekdays),
                    detail=(
                        f"{contractor.full_name} has no recorded time on "
                        f"{len(contractor.missing_weekdays)} weekday(s) of the period"
                    ),
                ))

        # 3. The billed rate must match the work order.
        if work_order is None:
            status = Status.FAIL if cfg.missing_work_order_is_failure else Status.SKIP
            checks.append(Check(
                key="work_order_found",
                label="A work order exists for this consultant",
                status=status,
                expected="a work order",
                actual="none found",
                detail=(
                    f"no work order could be matched to '{line.contractor_name}'; "
                    "the billing rate cannot be verified"
                ),
            ))
        else:
            checks.append(Check(
                key="work_order_found",
                label="A work order exists for this consultant",
                status=Status.PASS,
                expected="a work order",
                actual=work_order.path.name,
                detail=(
                    f"{work_order.vendor_name} | ref {work_order.work_order_ref or 'n/a'} "
                    f"| status {work_order.status or 'unknown'}"
                ),
            ))

            if work_order.rate is None:
                checks.append(Check(
                    key="rate_match",
                    label="Billed rate matches the work order",
                    status=Status.FAIL,
                    expected="a work order rate",
                    actual=f"{_num(line.rate)}",
                    detail="the work order does not state a billing rate",
                ))
            elif self._close(work_order.rate, line.rate, cfg.rate_tolerance):
                checks.append(Check(
                    key="rate_match",
                    label="Billed rate matches the work order",
                    status=Status.PASS,
                    expected=f"{_num(work_order.rate)} {work_order.currency or ''}".strip(),
                    actual=f"{_num(line.rate)} {line.currency}".strip(),
                    detail=f"rate basis: {work_order.rate_basis or 'unspecified'}",
                ))
            else:
                checks.append(Check(
                    key="rate_match",
                    label="Billed rate matches the work order",
                    status=Status.FAIL,
                    expected=f"{_num(work_order.rate)} {work_order.currency or ''}".strip(),
                    actual=f"{_num(line.rate)} {line.currency}".strip(),
                    detail="the invoice rate differs from the contracted rate",
                ))

            # 4. The headline assertion.
            if contractor is not None and work_order.rate is not None:
                expected = contractor.total_hours * work_order.rate
                if self._close(expected, line.amount, cfg.amount_tolerance):
                    checks.append(Check(
                        key="amount_match",
                        label="Hours x work order rate = invoice amount",
                        status=Status.PASS,
                        expected=f"{_num(expected)} {line.currency}",
                        actual=f"{_num(line.amount)} {line.currency}",
                        detail=(
                            f"{_num(contractor.total_hours)} hrs x {_num(work_order.rate)} "
                            f"= {_num(expected)}"
                        ),
                    ))
                else:
                    checks.append(Check(
                        key="amount_match",
                        label="Hours x work order rate = invoice amount",
                        status=Status.FAIL,
                        expected=f"{_num(expected)} {line.currency}",
                        actual=f"{_num(line.amount)} {line.currency}",
                        detail=(
                            f"{_num(contractor.total_hours)} hrs x {_num(work_order.rate)} "
                            f"= {_num(expected)}, invoice bills {_num(line.amount)} "
                            f"(difference {_num(line.amount - expected)})"
                        ),
                    ))

            # 5. The invoice period must sit inside the authorised window.
            window = self._period_check(invoice, work_order)
            if window is not None:
                checks.append(window)

            if work_order.status and work_order.status.lower() != "verified":
                checks.append(Check(
                    key="work_order_status",
                    label="Work order verification status",
                    status=Status.WARN,
                    expected="Verified",
                    actual=work_order.status,
                    detail=f"work order {work_order.work_order_ref or work_order.path.name} "
                           f"is '{work_order.status}'",
                ))

        # 6. The invoice period must match the report period.
        period = self._period_matches_report(invoice)
        if period is not None:
            checks.append(period)

        return checks

    def _period_check(self, invoice: Invoice, work_order: WorkOrder) -> Optional[Check]:
        if not invoice.period_start or not invoice.period_end:
            return None
        if not work_order.auth_start or not work_order.est_end:
            return Check(
                key="period_in_work_order",
                label="Invoice period is inside the work order window",
                status=Status.SKIP,
                expected=f"{work_order.auth_start} to {work_order.est_end}",
                actual=f"{invoice.period_start} to {invoice.period_end}",
                detail="the work order window could not be read",
            )
        if work_order.auth_start <= invoice.period_start and invoice.period_end <= work_order.est_end:
            return Check(
                key="period_in_work_order",
                label="Invoice period is inside the work order window",
                status=Status.PASS,
                expected=f"{work_order.auth_start} to {work_order.est_end}",
                actual=f"{invoice.period_start} to {invoice.period_end}",
                detail="the engagement was authorised for the whole invoiced period",
            )
        if invoice.period_start > work_order.est_end:
            detail = (
                f"the work order expired on {work_order.est_end}, "
                f"{(invoice.period_start - work_order.est_end).days} day(s) before the "
                "invoiced period starts; a renewal or extension is needed"
            )
        else:
            detail = (
                f"the work order does not start until {work_order.auth_start}, "
                "after the invoiced period begins"
            )
        return Check(
            key="period_in_work_order",
            label="Invoice period is inside the work order window",
            status=Status.FAIL,
            expected=f"{work_order.auth_start} to {work_order.est_end}",
            actual=f"{invoice.period_start} to {invoice.period_end}",
            detail=detail,
        )

    def _period_matches_report(self, invoice: Invoice) -> Optional[Check]:
        report = self._report
        if not invoice.period_start or not invoice.period_end:
            return None
        tolerance = self.config.period_tolerance_days
        same = (
            abs((invoice.period_start - report.period_start).days) <= tolerance
            and abs((invoice.period_end - report.period_end).days) <= tolerance
        )
        return Check(
            key="period_matches_report",
            label="Invoice period matches the time report period",
            status=Status.PASS if same else Status.WARN,
            expected=f"{report.period_start} to {report.period_end}",
            actual=f"{invoice.period_start} to {invoice.period_end}",
            detail="the invoice covers the same period as the time report"
            if same else "the invoice period differs from the time report period",
        )

    @staticmethod
    def _close(a: Decimal, b: Decimal, tolerance: Decimal) -> bool:
        return abs(a - b) <= tolerance


def _num(value: Decimal) -> str:
    """Render a Decimal without trailing noise (160 not 160.00)."""
    normalized = value.normalize()
    if normalized == normalized.to_integral_value():
        return str(normalized.quantize(Decimal(1)))
    return str(normalized)
