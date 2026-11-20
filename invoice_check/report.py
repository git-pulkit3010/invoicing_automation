"""Step 4: the payment decision report (Markdown, CSV, JSON)."""

from __future__ import annotations

import csv
import io
import json
from decimal import Decimal

from .common import fmt_money, fmt_num
from .timesheet import Timesheet
from .verify import GO, NO, REVIEW, Decision

ICON = {GO: "MATCH", NO: "MISMATCH", REVIEW: "REVIEW_REQUIRED"}


def markdown(decisions: list[Decision], ts: Timesheet, errors: list[str]) -> str:
    n = {k: sum(1 for d in decisions if d.decision == k) for k in (GO, NO, REVIEW)}
    total = {k: sum(((d.total or Decimal(0)) for d in decisions if d.decision == k), Decimal(0))
             for k in (GO, NO, REVIEW)}
    out = [
        "# Invoice payment check",
        "",
        f"Timesheet period: {ts.period_start} to {ts.period_end} ({ts.path.name})",
        "",
        "| Decision | Invoices | Amount |",
        "| --- | --- | --- |",
        f"| Go ahead with payment | {n[GO]} | {fmt_money(total[GO])} |",
        f"| Do not pay | {n[NO]} | {fmt_money(total[NO])} |",
        f"| Needs a person to check | {n[REVIEW]} | {fmt_money(total[REVIEW])} |",
        "",
    ]
    if errors:
        out += ["## Errors", ""] + [f"- {e}" for e in errors] + [""]
    out += ["## Invoices", "",
            "| Decision | Vendor | Invoice | Consultant | Inv hrs | TS hrs | Rate (inv / WO) | Amount | Expected | WO valid |",
            "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
    for d in decisions:
        for i, c in enumerate(d.consultants or [None]):
            first = i == 0
            out.append("| " + " | ".join([
                f"**{ICON[d.decision]}**" if first else "",
                d.vendor if first else "",
                d.invoice_number if first else "",
                c.name if c else "-",
                fmt_num(c.invoice_hours) if c else "-",
                fmt_num(c.timesheet_hours) if c else "-",
                f"{fmt_num(c.invoice_rate)} / {fmt_num(c.wo_rate)}" if c else "-",
                fmt_money(c.invoice_amount) if c else "-",
                fmt_money(c.expected_amount) if c else "-",
                (c.wo_valid or "-") if c else "-",
            ]) + " |")
    out.append("")
    out += ["## Details", ""]
    for d in decisions:
        out.append(f"### {d.decision}: {d.vendor or d.file} invoice {d.invoice_number or '?'}")
        out.append("")
        out.append(f"**{d.message}.** File: `{d.file}`. Period {d.period}. Total {fmt_money(d.total)}."
                   + (" Values read from an image." if d.source == "image" else ""))
        out.append("")
        for r in d.reasons:
            out.append(f"- {r}")
        if d.decision == NO and (d.untrusted or d.reviews):
            out += [f"- (also needs checking) {r}" for r in d.reviews]
        for w in d.warnings:
            out.append(f"- Note: {w}")
        out.append("")
    return "\n".join(out).rstrip() + "\n"


def to_csv(decisions: list[Decision]) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["decision", "message", "vendor", "invoice_number", "file", "period", "total",
                "consultant", "attuid", "invoice_hours", "timesheet_hours", "invoice_rate", "wo_rate",
                "invoice_amount", "expected_amount", "wo_valid", "reasons", "notes"])
    for d in decisions:
        for c in d.consultants or [None]:
            w.writerow([d.decision, d.message, d.vendor, d.invoice_number, d.file, d.period,
                        d.total if d.total is not None else "",
                        c.name if c else "", (c.attuid or "") if c else "",
                        *[(getattr(c, k) if c and getattr(c, k) is not None else "")
                          for k in ("invoice_hours", "timesheet_hours", "invoice_rate", "wo_rate",
                                    "invoice_amount", "expected_amount", "wo_valid")],
                        " | ".join(d.reasons), " | ".join(d.warnings)])
    return buf.getvalue()


def to_json(decisions: list[Decision]) -> str:
    def conv(o):
        if isinstance(o, Decimal):
            return str(o)
        return o.__dict__
    return json.dumps([{**d.__dict__, "message": d.message, "reasons": d.reasons} for d in decisions],
                      default=conv, indent=2)
