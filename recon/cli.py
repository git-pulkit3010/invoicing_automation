"""Command line interface for the CompNova invoice reconciliation tool."""

from __future__ import annotations

import argparse
import sys
from decimal import Decimal, InvalidOperation
from pathlib import Path

from .config import ReconConfig
from .extract.invoice import parse_invoice
from .extract.timesheet import load_timesheet
from .extract.workorder import parse_work_order
from .models import Status
from .reconcile import Reconciler, _num
from .report import render_csv, render_console, render_json

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_USAGE = 2


def _default_root() -> Path:
    """The directory that holds the Excel report, invoices/ and work_orders/."""
    return Path(__file__).resolve().parent.parent


def _decimal(value: str) -> Decimal:
    try:
        return Decimal(value)
    except InvalidOperation as exc:
        raise argparse.ArgumentTypeError(f"not a number: {value}") from exc


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="recon",
        description=(
            "Reconcile vendor invoices against the CMPM ATT Time Report and the "
            "vendor Work Orders. Verifies that hours x work order rate = invoice amount."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "examples:\n"
            "  recon reconcile                      reconcile every invoice in the workspace\n"
            "  recon reconcile --format json        emit machine readable results\n"
            "  recon reconcile --only Harish        reconcile one contractor\n"
            "  recon timesheet --name Harish        show recorded hours for a contractor\n"
            "  recon workorders                     list the parsed work orders\n"
            "  recon parse invoices/<file>.pdf      show how one document was parsed\n"
        ),
    )
    parser.add_argument(
        "--root", type=Path, default=_default_root(),
        help="workspace root holding the report, invoices/ and work_orders/ (default: %(default)s)",
    )

    subparsers = parser.add_subparsers(dest="command")

    def add_common(sub: argparse.ArgumentParser) -> None:
        sub.add_argument("--excel", type=Path, help="path to the ATT Time Report workbook")
        sub.add_argument("--invoices", type=Path, help="directory containing invoice PDFs")
        sub.add_argument("--work-orders", type=Path, help="directory containing work order PDFs")

    reconcile = subparsers.add_parser(
        "reconcile", help="reconcile invoices against the time report and work orders"
    )
    add_common(reconcile)
    reconcile.add_argument(
        "--format", choices=["table", "json", "csv"], default="table",
        help="output format (default: %(default)s)",
    )
    reconcile.add_argument("--output", type=Path, help="write the report to a file instead of stdout")
    reconcile.add_argument(
        "--amount-tolerance", type=_decimal, default=Decimal("0.01"),
        help="money tolerance for amount comparisons (default: %(default)s)",
    )
    reconcile.add_argument(
        "--rate-tolerance", type=_decimal, default=Decimal("0.01"),
        help="tolerance for rate comparisons (default: %(default)s)",
    )
    reconcile.add_argument(
        "--hours-tolerance", type=_decimal, default=Decimal("0.01"),
        help="tolerance for hour comparisons (default: %(default)s)",
    )
    reconcile.add_argument(
        "--only", metavar="NAME",
        help="reconcile only invoices whose contractor matches this text",
    )
    reconcile.add_argument(
        "--include-out-of-period",
        action="store_true",
        help="include time report rows whose own period falls outside the report period",
    )
    reconcile.add_argument(
        "--allow-missing-work-order", action="store_true",
        help="treat a missing work order as a warning instead of a failure",
    )

    timesheet = subparsers.add_parser("timesheet", help="show recorded hours")
    add_common(timesheet)
    timesheet.add_argument("--name", help="filter by contractor name (case insensitive)")
    timesheet.add_argument("--attuid", help="filter by contractor ATTUID")
    timesheet.add_argument("--missing-only", action="store_true", help="only contractors with gaps")
    timesheet.add_argument("--limit", type=int, default=50, help="maximum rows to show")

    workorders = subparsers.add_parser("workorders", help="list parsed work orders")
    add_common(workorders)
    workorders.add_argument("--format", choices=["table", "json"], default="table")

    parse_cmd = subparsers.add_parser("parse", help="show how a single document was parsed")
    parse_cmd.add_argument("file", type=Path, help="PDF to parse")
    parse_cmd.add_argument(
        "--kind", choices=["auto", "invoice", "workorder"], default="auto",
        help="document type (default: auto-detect)",
    )

    return parser


def _config_from_args(args) -> ReconConfig:
    root = args.root.resolve()
    config = ReconConfig.from_root(
        root,
        excel=args.excel.resolve() if args.excel else None,
        invoices_dir=args.invoices.resolve() if args.invoices else None,
        work_orders_dir=args.work_orders.resolve() if args.work_orders else None,
    )
    return config


def cmd_reconcile(args) -> int:
    config = _config_from_args(args)
    config.amount_tolerance = args.amount_tolerance
    config.rate_tolerance = args.rate_tolerance
    config.hours_tolerance = args.hours_tolerance
    config.include_out_of_period_rows = args.include_out_of_period
    config.missing_work_order_is_failure = not args.allow_missing_work_order

    result = Reconciler(config).run()

    if args.only:
        needle = args.only.lower()
        result.reconciliations = [
            r for r in result.reconciliations
            if needle in (r.line.contractor_name or "").lower()
            or needle in (r.invoice.filename_client or "").lower()
            or needle in r.invoice.path.name.lower()
        ]

    if args.format == "json":
        rendered = render_json(result)
    elif args.format == "csv":
        rendered = render_csv(result)
    else:
        rendered = render_console(result)

    if args.output:
        args.output.write_text(rendered, encoding="utf-8")
        print(f"wrote {args.output}")
    else:
        print(rendered)

    if result.errors:
        return EXIT_USAGE
    return EXIT_FAILED if result.failed else EXIT_OK


def cmd_timesheet(args) -> int:
    config = _config_from_args(args)
    report = load_timesheet(
        config.excel, include_out_of_period_rows=config.include_out_of_period_rows
    )

    rows = []
    for contractor in report.contractors.values():
        if args.name and args.name.lower() not in contractor.full_name.lower():
            continue
        if args.attuid and args.attuid.lower() != contractor.attuid.lower():
            continue
        if args.missing_only and not contractor.missing_weekdays:
            continue
        rows.append(contractor)

    rows.sort(key=lambda c: (c.full_name, c.attuid))
    if args.limit:
        rows = rows[: args.limit]

    print(f"{'ATTUID':<8} {'NAME':<32} {'DAYS':>4} {'REG':>7} {'OT':>4} {'TOTAL':>7}  MISSING")
    print("-" * 92)
    for contractor in rows:
        missing = ",".join(str(d) for d in contractor.missing_weekdays) or "-"
        print(
            f"{contractor.attuid:<8} {contractor.full_name:<32} "
            f"{contractor.days_recorded:>4} {_num(contractor.regular_hours):>7} "
            f"{_num(contractor.ot_hours):>4} {_num(contractor.total_hours):>7}  {missing}"
        )
    print("-" * 92)
    print(f"{len(rows)} contractor(s) shown; report period {report.period_start}..{report.period_end}")
    return EXIT_OK


def cmd_workorders(args) -> int:
    config = _config_from_args(args)
    work_orders = [parse_work_order(p) for p in sorted(config.work_orders_dir.glob("*.pdf"))]

    if args.format == "json":
        import json

        payload = [
            {
                "file": w.path.name,
                "vendor": w.vendor_name,
                "consultant": w.consultant_name,
                "rate": w.rate,
                "currency": w.currency,
                "basis": w.rate_basis,
                "client": w.client_name,
                "window": [w.auth_start, w.est_end],
                "status": w.status,
                "ref": w.work_order_ref,
                "number": w.work_order_number,
                "flags": w.flags,
            }
            for w in work_orders
        ]
        print(json.dumps(payload, indent=2, default=str))
        return EXIT_OK

    print(f"{'CONSULTANT':<26} {'RATE':>10} {'WINDOW':<25} {'STATUS':<10} {'REF':<12} VENDOR")
    print("-" * 120)
    for w in work_orders:
        window = f"{w.auth_start}..{w.est_end}" if w.auth_start and w.est_end else "-"
        rate = f"{w.currency or ''} {_num(w.rate)}".strip() if w.rate is not None else "-"
        print(
            f"{(w.consultant_name or '?')[:25]:<26} {rate:>10} {window:<25} "
            f"{(w.status or '?'):<10} {(w.work_order_ref or '-')[:11]:<12} {w.vendor_name or '-'}"
        )
        for flag in w.flags:
            print(f"{'':<26} note: {flag}")
    print("-" * 120)
    print(f"{len(work_orders)} work order(s)")
    return EXIT_OK


def cmd_parse(args) -> int:
    path = args.file
    if not path.exists():
        print(f"file not found: {path}", file=sys.stderr)
        return EXIT_USAGE

    kind = args.kind
    if kind == "auto":
        kind = "workorder" if "work_order" in path.name.lower() else "invoice"

    if kind == "workorder":
        work_order = parse_work_order(path)
        print(f"file          {path.name}")
        print(f"vendor        {work_order.vendor_name}")
        print(f"consultant    {work_order.consultant_name}")
        print(f"rate          {work_order.currency} {_num(work_order.rate)} {work_order.rate_basis}")
        print(f"client        {work_order.client_name}")
        print(f"window        {work_order.auth_start} .. {work_order.est_end}")
        print(f"wo number     {work_order.work_order_number}")
        print(f"wo ref        {work_order.work_order_ref}")
        print(f"status        {work_order.status}   created {work_order.created}")
        for flag in work_order.flags:
            print(f"note          {flag}")
        return EXIT_OK

    invoice = parse_invoice(path)
    print(f"file          {path.name}")
    print(f"vendor        {invoice.vendor_name}")
    print(f"invoice #     {invoice.invoice_number}")
    print(f"invoice date  {invoice.invoice_date}   due {invoice.due_date}   terms {invoice.terms}")
    print(f"period        {invoice.period_start} .. {invoice.period_end}")
    print(f"total         {_num(invoice.total_amount) if invoice.total_amount is not None else '-'}")
    print(f"filename      client={invoice.filename_client} person={invoice.filename_person}")
    for line in invoice.lines:
        print(
            f"line          {_num(line.quantity)} hrs x {_num(line.rate)} = "
            f"{_num(line.amount)} {line.currency} | {line.contractor_name or '?'}"
        )
        print(f"              {line.description}")
        for note in line.notes:
            print(f"note          {note}")
    for flag in invoice.flags:
        print(f"note          {flag}")
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if not args.command:
        parser.print_help()
        return EXIT_USAGE

    handlers = {
        "reconcile": cmd_reconcile,
        "timesheet": cmd_timesheet,
        "workorders": cmd_workorders,
        "parse": cmd_parse,
    }
    try:
        return handlers[args.command](args)
    except FileNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_USAGE
    except Exception as exc:  # pragma: no cover - last resort guard
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_USAGE


if __name__ == "__main__":
    raise SystemExit(main())
