"""Rendering of reconciliation results as a console table, JSON or CSV."""

from __future__ import annotations

import csv
import io
import json
from dataclasses import asdict, is_dataclass
from datetime import date
from decimal import Decimal
from typing import Any

from .models import Reconciliation, Status
from .reconcile import RunResult, _num

COLUMNS = [
    ("Client", 30, "l"),
    ("Contractor", 22, "l"),
    ("Inv hrs", 9, "r"),
    ("TS hrs", 9, "r"),
    ("Rate", 10, "r"),
    ("Amount", 13, "r"),
    ("Expected", 13, "r"),
    ("Status", 8, "r"),
]


def _money(value: Decimal | None) -> str:
    if value is None:
        return "-"
    return f"{value:,.2f}"


def _clip(text: str, width: int) -> str:
    text = text or ""
    if len(text) <= width:
        return text.ljust(width)
    return text[: width - 1] + "."


def render_console(result: RunResult, verbose: bool = False) -> str:
    out: list[str] = []
    report = result.report

    out.append("CompNova invoice reconciliation")
    out.append("=" * 78)
    if report is not None:
        out.append(f"Time report : {report.path.name}")
        out.append(
            f"Period      : {report.period_start} .. {report.period_end}"
            f"   ({len(report.contractors)} contractors)"
        )
    out.append(f"Invoices    : {len(result.invoices)}")
    out.append(f"Work orders : {len(result.work_orders)}")
    out.append("")

    if result.errors:
        out.append("ERRORS")
        out.append("-" * 78)
        for error in result.errors:
            out.append(f"  ! {error}")
        out.append("")

    out.append("SUMMARY")
    out.append("-" * 78)
    header = "".join(
        name.ljust(width) if align == "l" else name.rjust(width)
        for name, width, align in COLUMNS
    )
    out.append(header)
    out.append("-" * len(header))
    for reconciliation in result.reconciliations:
        out.append(_summary_row(reconciliation))
    out.append("-" * len(header))
    advisories = sum(len(r.warnings) for r in result.reconciliations)
    out.append(
        f"{result.passed} passed, {result.failed} failed of "
        f"{len(result.reconciliations)} invoice line(s); {advisories} advisory finding(s)"
    )
    out.append("")

    out.append("DETAIL")
    out.append("=" * 78)
    for index, reconciliation in enumerate(result.reconciliations, start=1):
        out.extend(_detail_block(index, reconciliation))
        out.append("")

    if result.unlinked_work_orders:
        out.append("WORK ORDERS WITHOUT A MATCHED INVOICE")
        out.append("-" * 78)
        for work_order in result.unlinked_work_orders:
            out.append(
                f"  {work_order.path.name} -> {work_order.consultant_name or 'unknown'}"
                f" ({work_order.vendor_name or 'unknown'})"
            )
        out.append("")

    return "\n".join(out)


def _summary_row(reconciliation: Reconciliation) -> str:
    invoice = reconciliation.invoice
    line = reconciliation.line
    contractor = reconciliation.contractor
    work_order = reconciliation.work_order

    expected = None
    if contractor is not None and work_order is not None and work_order.rate is not None:
        expected = contractor.total_hours * work_order.rate

    cells = [
        _clip(invoice.vendor_name or invoice.filename_client or "-", 30),
        _clip(line.contractor_name or "-", 22),
        _num(line.quantity).rjust(9),
        (_num(contractor.total_hours) if contractor else "-").rjust(9),
        f"{_num(line.rate)}".rjust(10),
        _money(line.amount).rjust(13),
        _money(expected).rjust(13),
        reconciliation.status.value.rjust(8),
    ]
    return "".join(cells)


def _detail_block(index: int, reconciliation: Reconciliation) -> list[str]:
    invoice = reconciliation.invoice
    line = reconciliation.line
    contractor = reconciliation.contractor
    work_order = reconciliation.work_order

    title = invoice.filename_client or invoice.vendor_name or invoice.path.stem
    name = line.contractor_name or "<unknown>"
    out = [f"[{index}] {title} - {name}"]

    meta = [f"invoice   {invoice.path.name}"]
    if invoice.invoice_number:
        meta.append(f"invoice # {invoice.invoice_number}")
    if invoice.invoice_date:
        meta.append(f"dated {invoice.invoice_date}")
    if invoice.terms:
        meta.append(f"terms {invoice.terms}")
    if invoice.period_start and invoice.period_end:
        meta.append(f"service {invoice.period_start}..{invoice.period_end}")
    out.append("    " + " | ".join(meta))

    out.append(
        f"    line      {_num(line.quantity)} hrs x {_num(line.rate)} = {_money(line.amount)} {line.currency}"
    )

    if contractor is not None:
        ts_meta = (
            f"{contractor.full_name} ({contractor.attuid}) | "
            f"{contractor.days_recorded} day(s) | supervisor {contractor.supervisor_attuid}"
        )
        out.append(f"    timesheet {ts_meta}")
        weekly = ", ".join(
            f"{bucket.week_start.strftime('%b %d')}:{_num(bucket.hours)}h" for bucket in contractor.weeks
        )
        out.append(f"              weekly {weekly}")
    else:
        out.append("    timesheet <no match in the time report>")

    if work_order is not None:
        wo_meta = (
            f"{work_order.vendor_name or 'unknown'} | ref {work_order.work_order_ref or 'n/a'}"
            f" | {work_order.work_order_number or 'n/a'}"
        )
        if work_order.rate is not None:
            wo_meta += f" | rate {work_order.currency or ''} {_num(work_order.rate)} {work_order.rate_basis or ''}".rstrip()
        out.append(f"    workorder {wo_meta}")
        if work_order.auth_start and work_order.est_end:
            out.append(
                f"              window {work_order.auth_start}..{work_order.est_end}"
                f" | status {work_order.status or 'unknown'}"
            )
    else:
        out.append("    workorder <not found>")

    for check in reconciliation.checks:
        out.append(f"    {check.status.value:<4} {check.label}")
        out.append(f"         expected: {check.expected}")
        out.append(f"         actual  : {check.actual}")
        if check.detail:
            out.append(f"         note    : {check.detail}")

    for flag in invoice.flags:
        out.append(f"    NOTE  {flag}")
    if contractor is not None:
        for flag in contractor.flags:
            out.append(f"    NOTE  {flag}")
    if work_order is not None:
        for flag in work_order.flags:
            out.append(f"    NOTE  {flag}")

    return out


class _Encoder(json.JSONEncoder):
    def default(self, o: Any) -> Any:
        if isinstance(o, Decimal):
            return float(o)
        if isinstance(o, date):
            return o.isoformat()
        if is_dataclass(o) and not isinstance(o, type):
            return asdict(o)
        if isinstance(o, Status):
            return o.value
        return super().default(o)


def render_json(result: RunResult) -> str:
    payload = {
        "status": result.status.value,
        "summary": {
            "passed": result.passed,
            "failed": result.failed,
            "warned": result.warned,
            "invoices": len(result.invoices),
            "work_orders": len(result.work_orders),
        },
        "period": (
            {
                "start": result.report.period_start,
                "end": result.report.period_end,
            }
            if result.report
            else None
        ),
        "errors": result.errors,
        "reconciliations": [
            {
                "invoice_file": r.invoice.path.name,
                "client": r.invoice.vendor_name,
                "invoice_number": r.invoice.invoice_number,
                "invoice_period": (
                    [r.invoice.period_start, r.invoice.period_end]
                    if r.invoice.period_start
                    else None
                ),
                "contractor": {
                    "name": r.line.contractor_name,
                    "attuid": r.contractor.attuid if r.contractor else None,
                    "timesheet_hours": r.contractor.total_hours if r.contractor else None,
                    "days_recorded": r.contractor.days_recorded if r.contractor else None,
                    "missing_weekdays": r.contractor.missing_weekdays if r.contractor else None,
                },
                "work_order": {
                    "file": r.work_order.path.name if r.work_order else None,
                    "consultant": r.work_order.consultant_name if r.work_order else None,
                    "rate": r.work_order.rate if r.work_order else None,
                    "currency": r.work_order.currency if r.work_order else None,
                    "window": (
                        [r.work_order.auth_start, r.work_order.est_end]
                        if r.work_order and r.work_order.auth_start
                        else None
                    ),
                    "status": r.work_order.status if r.work_order else None,
                },
                "invoice_line": {
                    "quantity": r.line.quantity,
                    "rate": r.line.rate,
                    "amount": r.line.amount,
                    "currency": r.line.currency,
                    "description": r.line.description,
                },
                "result": r.status.value,
                "checks": [
                    {
                        "key": c.key,
                        "label": c.label,
                        "status": c.status.value,
                        "expected": c.expected,
                        "actual": c.actual,
                        "detail": c.detail,
                    }
                    for c in r.checks
                ],
            }
            for r in result.reconciliations
        ],
        "unlinked_work_orders": [w.path.name for w in result.unlinked_work_orders],
    }
    return json.dumps(payload, indent=2, cls=_Encoder)


def render_csv(result: RunResult) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow([
        "invoice_file", "client", "invoice_number", "contractor", "attuid",
        "invoice_hours", "timesheet_hours", "days_recorded",
        "invoice_rate", "work_order_rate", "invoice_amount", "expected_amount",
        "difference", "invoice_period", "work_order_window", "result",
    ])
    for reconciliation in result.reconciliations:
        invoice = reconciliation.invoice
        line = reconciliation.line
        contractor = reconciliation.contractor
        work_order = reconciliation.work_order

        expected = None
        if contractor is not None and work_order is not None and work_order.rate is not None:
            expected = contractor.total_hours * work_order.rate

        writer.writerow([
            invoice.path.name,
            invoice.vendor_name or "",
            invoice.invoice_number or "",
            line.contractor_name or "",
            contractor.attuid if contractor else "",
            _num(line.quantity),
            _num(contractor.total_hours) if contractor else "",
            contractor.days_recorded if contractor else "",
            _num(line.rate),
            _num(work_order.rate) if work_order and work_order.rate is not None else "",
            _num(line.amount),
            _num(expected) if expected is not None else "",
            _num(line.amount - expected) if expected is not None else "",
            f"{invoice.period_start}..{invoice.period_end}" if invoice.period_start else "",
            f"{work_order.auth_start}..{work_order.est_end}"
            if work_order and work_order.auth_start
            else "",
            reconciliation.status.value,
        ])
    return buffer.getvalue()
