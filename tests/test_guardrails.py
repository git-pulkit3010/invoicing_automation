"""Self-contained regression tests for monthly reconciliation guardrails."""

from datetime import date
from decimal import Decimal
from unittest.mock import patch

from recon.cli import build_parser, cmd_reconcile
from recon.config import ReconConfig
from recon.extract.timesheet import TimesheetReport
from recon.models import ContractorHours, Invoice, InvoiceLine, Status, WorkOrder
from recon.reconcile import Reconciler
from recon.report import render_csv, render_json


START = date(2026, 10, 1)
END = date(2026, 10, 31)


def _sources(tmp_path, invoice_lines=True):
    (tmp_path / "invoices").mkdir()
    (tmp_path / "work_orders").mkdir()
    invoice_path = tmp_path / "invoices" / "invoice.pdf"
    invoice_path.touch()
    contractor = ContractorHours(
        "alice", "Alice", "Smith", "Alice Smith", "Acme", "boss", "", "",
        START, END, Decimal(8), Decimal(0), 1,
    )
    report = TimesheetReport(tmp_path / "report.xlsx", START, END, {"alice": contractor})
    invoice = Invoice(
        invoice_path, vendor_name="Acme", period_start=START, period_end=END,
        lines=[InvoiceLine("service", "Alice Smith", Decimal(8), Decimal(100),
                           Decimal(800), "USD", "service")]
        if invoice_lines else [],
    )
    return report, invoice


def _run(tmp_path, report, invoice, work_orders):
    config = ReconConfig.from_root(tmp_path)
    with patch.object(Reconciler, "load", return_value=(report, work_orders)), \
         patch("recon.reconcile.parse_invoice", return_value=invoice):
        return Reconciler(config).run()


def test_other_consultants_work_order_cannot_pass(tmp_path):
    report, invoice = _sources(tmp_path)
    bob = WorkOrder(
        tmp_path / "work_orders" / "bob.pdf", vendor_name="Acme",
        consultant_name="Bob Jones", rate=Decimal(100),
        auth_start=START, est_end=END, status="Verified",
    )
    result = _run(tmp_path, report, invoice, [bob])
    row = result.reconciliations[0]
    assert row.work_order is None
    assert row.status is Status.FAIL
    assert bob in result.unlinked_work_orders


def test_unique_unnamed_vendor_fallback_requires_review(tmp_path):
    report, invoice = _sources(tmp_path)
    unnamed = WorkOrder(
        tmp_path / "work_orders" / "unnamed.pdf", vendor_name="Acme",
        rate=Decimal(100), auth_start=START, est_end=END, status="Verified",
    )
    result = _run(tmp_path, report, invoice, [unnamed])
    row = result.reconciliations[0]
    assert row.status is Status.PASS
    assert any(c.key == "work_order_vendor_fallback" and c.status is Status.WARN
               for c in row.checks)
    assert len(row.warnings) == 1


def test_ambiguous_vendor_fallback_cannot_pass(tmp_path):
    report, invoice = _sources(tmp_path)
    orders = [WorkOrder(tmp_path / "work_orders" / f"{i}.pdf", vendor_name="Acme")
              for i in range(2)]
    result = _run(tmp_path, report, invoice, orders)
    assert result.reconciliations[0].work_order is None
    assert result.reconciliations[0].status is Status.FAIL


def test_unrecognized_invoice_is_an_error_and_other_invoices_continue(tmp_path):
    report, invoice = _sources(tmp_path, invoice_lines=False)
    invoice.flags.append("no billable line item could be identified")
    good_path = tmp_path / "invoices" / "valid.pdf"
    good_path.touch()
    valid = Invoice(
        good_path, vendor_name="Acme", period_start=START, period_end=END,
        lines=[InvoiceLine("service", "Alice Smith", Decimal(8), Decimal(100),
                           Decimal(800), "USD", "service")],
    )
    with patch.object(Reconciler, "load", return_value=(report, [])), \
         patch("recon.reconcile.parse_invoice", side_effect=[invoice, valid]):
        result = Reconciler(ReconConfig.from_root(tmp_path)).run()
    assert result.status is Status.FAIL
    assert len(result.reconciliations) == 1
    assert any("invoice.pdf" in error for error in result.errors)
    assert "reconciled_lines" in render_json(result)


def test_empty_batch_is_an_error(tmp_path):
    report, _ = _sources(tmp_path)
    (tmp_path / "invoices" / "invoice.pdf").unlink()
    with patch.object(Reconciler, "load", return_value=(report, [])):
        result = Reconciler(ReconConfig.from_root(tmp_path)).run()
    assert result.status is Status.FAIL
    assert result.errors == ["no invoice lines were reconciled"]
    args = build_parser().parse_args(["--root", str(tmp_path), "reconcile", "--format", "csv"])
    with patch("recon.cli.Reconciler.run", return_value=result):
        assert cmd_reconcile(args) != 0


def test_csv_contains_warnings_errors_and_missing_bills(tmp_path):
    report, invoice = _sources(tmp_path)
    unnamed = WorkOrder(
        tmp_path / "work_orders" / "unnamed.pdf", vendor_name="Acme",
        rate=Decimal(100), auth_start=START, est_end=END, status="Verified",
    )
    result = _run(tmp_path, report, invoice, [unnamed])
    result.errors.append("broken.pdf: could not parse invoice")
    result.unlinked_work_orders.append(
        WorkOrder(tmp_path / "work_orders" / "missing.pdf", consultant_name="Bob Jones")
    )
    import csv
    import io
    rows = list(csv.DictReader(io.StringIO(render_csv(result))))
    assert [row["record_type"] for row in rows] == [
        "reconciliation", "error", "unlinked_work_order",
    ]
    assert "confirm" in rows[0]["warnings"]
    assert rows[1]["invoice_file"] == "broken.pdf"
    assert "missing.pdf" in rows[2]["detail"]
    assert all(None not in row for row in rows)
