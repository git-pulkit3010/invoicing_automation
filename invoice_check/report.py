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
        out.append(f"**{d.summary}**")
        out.append("")
        out.append(f"File: `{d.file}`. Period {d.period}. Total {fmt_money(d.total)}."
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
    w.writerow(["decision", "pay", "summary", "message", "vendor", "invoice_number", "file", "period", "total",
                "consultant", "attuid", "invoice_hours", "timesheet_hours", "invoice_rate", "wo_rate",
                "invoice_amount", "expected_amount", "wo_valid", "reasons", "notes"])
    for d in decisions:
        for c in d.consultants or [None]:
            w.writerow([d.decision, d.pay, d.summary, d.message, d.vendor, d.invoice_number, d.file, d.period,
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
    return json.dumps([{**d.__dict__, "message": d.message, "pay": d.pay, "summary": d.summary,
                        "reasons": d.reasons} for d in decisions],
                      default=conv, indent=2)


# ------------------------------------------------------------------- Excel --
XLSX_COLUMNS = [  # (header, width)
    ("Verdict", 20), ("Pay?", 7), ("Message", 70), ("Vendor", 34), ("Invoice #", 15), ("Invoice file", 40),
    ("Consultant", 24), ("ATT ID", 9), ("Billing period", 24), ("Invoice hours", 9), ("Timesheet hours", 10),
    ("Invoice rate ($/hr)", 10), ("Work order rate ($/hr)", 11), ("Invoice amount ($)", 13),
    ("Expected amount ($)", 13), ("Difference ($)", 12), ("Invoice subtotal ($)", 13), ("Invoice total ($)", 13),
    ("Work order valid", 24), ("All reasons", 60), ("Notes", 60), ("Invoice row", 8),
]
FILLS = {GO: "C6EFCE", NO: "FFC7CE", REVIEW: "FFEB9C"}


def to_xlsx(decisions: list[Decision], ts: Timesheet, errors: list[str], path) -> None:
    """A fresh workbook with every verdict, its message and the cross-checked figures."""
    from datetime import datetime
    from pathlib import Path

    from openpyxl import Workbook
    from openpyxl.comments import Comment
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    base, bold = Font(name="Arial", size=10), Font(name="Arial", size=10, bold=True)
    head_fill = PatternFill("solid", start_color="D9E1F2")
    money, hours = '#,##0.00;(#,##0.00);"-"', '#,##0.##'
    col = {h: get_column_letter(i) for i, (h, _) in enumerate(XLSX_COLUMNS, start=1)}

    wb = Workbook()
    ws = wb.active
    ws.title = "Results"
    for i, (h, width) in enumerate(XLSX_COLUMNS, start=1):
        c = ws.cell(row=1, column=i, value=h)
        c.font, c.fill = bold, head_fill
        c.alignment = Alignment(wrap_text=True, vertical="top")
        ws.column_dimensions[get_column_letter(i)].width = width
    ws.row_dimensions[1].height = 42

    r = 2
    for d in decisions:
        for k, c in enumerate(d.consultants or [None]):
            row = {
                "Verdict": d.decision, "Pay?": d.pay, "Message": d.summary, "Vendor": d.vendor,
                "Invoice #": d.invoice_number, "Invoice file": d.file, "Consultant": c.name if c else None,
                "ATT ID": c.attuid if c else None, "Billing period": d.period,
                "Invoice hours": c.invoice_hours if c else None, "Timesheet hours": c.timesheet_hours if c else None,
                "Invoice rate ($/hr)": c.invoice_rate if c else None,
                "Work order rate ($/hr)": c.wo_rate if c else None,
                "Invoice amount ($)": c.invoice_amount if c else None,
                "Invoice subtotal ($)": d.subtotal, "Invoice total ($)": d.total,
                "Work order valid": c.wo_valid if c else None,
                "All reasons": " | ".join(d.reasons) or None, "Notes": " | ".join(d.warnings) or None,
                "Invoice row": 1 if k == 0 else 0,                  # counts each invoice once in Summary
            }
            for i, (h, _) in enumerate(XLSX_COLUMNS, start=1):
                v = row.get(h)
                cell = ws.cell(row=r, column=i, value=float(v) if isinstance(v, Decimal) else v)
                cell.font = base
                cell.alignment = Alignment(vertical="top", wrap_text=h in ("Message", "All reasons", "Notes"))
            ts_h, wo_r, inv_a = col["Timesheet hours"], col["Work order rate ($/hr)"], col["Invoice amount ($)"]
            exp = col["Expected amount ($)"]
            ws[f"{exp}{r}"] = f'=IF(AND(ISNUMBER({ts_h}{r}),ISNUMBER({wo_r}{r})),{ts_h}{r}*{wo_r}{r},"")'
            ws[f"{col['Difference ($)']}{r}"] = f'=IF(AND(ISNUMBER({inv_a}{r}),ISNUMBER({exp}{r})),{inv_a}{r}-{exp}{r},"")'
            for h in ("Invoice rate ($/hr)", "Work order rate ($/hr)", "Invoice amount ($)", "Expected amount ($)",
                      "Difference ($)", "Invoice subtotal ($)", "Invoice total ($)"):
                ws[f"{col[h]}{r}"].number_format = money
                ws[f"{col[h]}{r}"].font = base
            for h in ("Invoice hours", "Timesheet hours"):
                ws[f"{col[h]}{r}"].number_format = hours
            fill = PatternFill("solid", start_color=FILLS[d.decision])
            ws[f"{col['Verdict']}{r}"].fill = fill
            ws[f"{col['Pay?']}{r}"].fill = fill
            ws[f"{col['Verdict']}{r}"].font = bold
            r += 1
    last = r - 1
    ws.freeze_panes = "D2"
    ws.auto_filter.ref = f"A1:{get_column_letter(len(XLSX_COLUMNS))}{max(last, 1)}"
    ws[f"{col['Expected amount ($)']}1"].comment = Comment(
        "Formula: Timesheet hours x Work order rate. Timesheet hours come from the ATT Time Report for the "
        "month checked; the rate from the work order in force for the billing period.", "invoice-check")
    ws[f"{col['Invoice row']}1"].comment = Comment(
        "1 on the first row of each invoice, 0 on extra consultant rows. Used to count invoices on the "
        "Summary sheet.", "invoice-check")

    # ---- Summary
    sm = wb.create_sheet("Summary")
    sm.column_dimensions["A"].width = 34
    for c_ in "BCD":
        sm.column_dimensions[c_].width = 18
    info = [("Invoice payment check", None), (None, None),
            ("Timesheet month", f"{ts.period_start} to {ts.period_end}"),
            ("Timesheet file", ts.path.name),
            ("Report created", datetime.now().strftime("%Y-%m-%d %H:%M"))]
    for i, (k, v) in enumerate(info, start=1):
        sm.cell(row=i, column=1, value=k).font = Font(name="Arial", size=12 if i == 1 else 10, bold=i == 1)
        sm.cell(row=i, column=2, value=v).font = base
    hdr = 7
    for j, h in enumerate(["Verdict", "Meaning", "Invoices", "Invoice amount ($)"], start=1):
        c = sm.cell(row=hdr, column=j, value=h)
        c.font, c.fill = bold, head_fill
    rng = f"$2:${max(last, 2)}"
    vcol, rowcol, amtcol = col["Verdict"], col["Invoice row"], col["Invoice amount ($)"]
    for i, (v, meaning) in enumerate([(GO, "Go ahead with payment"), (NO, "Do not pay"),
                                      (REVIEW, "Needs a person to check")], start=hdr + 1):
        sm.cell(row=i, column=1, value=v).font = bold
        sm.cell(row=i, column=1).fill = PatternFill("solid", start_color=FILLS[v])
        sm.cell(row=i, column=2, value=meaning).font = base
        sm.cell(row=i, column=3, value=(f"=SUMIFS(Results!${rowcol}{rng.split(':')[0][1:]}:${rowcol}{rng.split(':')[1][1:]},"
                                        f"Results!${vcol}${2}:${vcol}${max(last, 2)},A{i})")).font = base
        sm.cell(row=i, column=4, value=(f"=SUMIFS(Results!${amtcol}$2:${amtcol}${max(last, 2)},"
                                        f"Results!${vcol}$2:${vcol}${max(last, 2)},A{i})")).font = base
        sm.cell(row=i, column=4).number_format = money
    tot = hdr + 4
    sm.cell(row=tot, column=1, value="Total").font = bold
    sm.cell(row=tot, column=3, value=f"=SUM(C{hdr + 1}:C{hdr + 3})").font = bold
    sm.cell(row=tot, column=4, value=f"=SUM(D{hdr + 1}:D{hdr + 3})").font = bold
    sm.cell(row=tot, column=4).number_format = money
    if errors:
        sm.cell(row=tot + 2, column=1, value="Not checked").font = bold
        for i, e in enumerate(errors, start=tot + 3):
            sm.cell(row=i, column=1, value=e).font = base

    # ---- Notes (where each figure comes from)
    nt = wb.create_sheet("Notes")
    nt.column_dimensions["A"].width = 26
    nt.column_dimensions["B"].width = 100
    notes = [
        ("Column", "Source"),
        ("Verdict / Pay? / Message", "Decided by code (invoice_check). MATCH = Yes; MISMATCH and REVIEW_REQUIRED = No."),
        ("Invoice hours, rate, amount, subtotal, total", "Read from the invoice and verified to appear on it."),
        ("Timesheet hours", f"ATT Time Report {ts.path.name}, {ts.period_start} to {ts.period_end}: "
                            "regular + overtime hours for the consultant's ATT ID."),
        ("Work order rate, Work order valid", "Work order database: the work order in force for the billing period."),
        ("Expected amount", "Formula: Timesheet hours x Work order rate."),
        ("Difference", "Formula: Invoice amount - Expected amount (0 when they agree)."),
        ("Invoice row", "Helper: 1 on the first row of each invoice, used to count invoices on the Summary sheet."),
    ]
    for i, (a_, b_) in enumerate(notes, start=1):
        nt.cell(row=i, column=1, value=a_).font = bold if i == 1 else base
        nt.cell(row=i, column=2, value=b_).font = bold if i == 1 else base
        nt.cell(row=i, column=2).alignment = Alignment(wrap_text=True, vertical="top")

    wb.calculation.fullCalcOnLoad = True          # Excel computes the formulas when the file opens
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    wb.save(path)
