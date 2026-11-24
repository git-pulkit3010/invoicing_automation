"""Step 4: the payment decision report (Markdown, CSV, JSON)."""

from __future__ import annotations

import csv
import io
import json
from decimal import Decimal

from .common import fmt_money, fmt_num, tidy_name
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
RESULT_COLUMNS = [  # (header, width, kind)
    ("Verdict", 19, "text"), ("Pay?", 7, "text"), ("Message", 78, "wrap"), ("Vendor", 30, "text"),
    ("Invoice #", 15, "text"), ("Consultant", 26, "text"), ("Timesheet hours", 11, "hours"),
    ("Rate ($/hr)", 10, "money"), ("Expected ($)", 13, "money"), ("Invoiced ($)", 13, "money"),
    ("Difference ($)", 13, "money"),
]
DETAIL_COLUMNS = [
    ("Invoice #", 15), ("Vendor", 30), ("Invoice file", 46), ("Consultant (as on invoice)", 26), ("ATT ID", 9),
    ("Billing period", 25), ("Invoice hours", 10), ("Invoice rate ($/hr)", 11), ("Invoice subtotal ($)", 13),
    ("Invoice total ($)", 13), ("Work order valid", 25), ("Reasons", 80), ("Notes", 80),
]
FILLS = {GO: "C6EFCE", NO: "FFC7CE", REVIEW: "FFEB9C"}
MONEY = '#,##0.00;-#,##0.00;"-"'


def _num(v):
    return float(v) if isinstance(v, Decimal) else v


def to_xlsx(decisions: list[Decision], ts: Timesheet, errors: list[str], path) -> None:
    """A fresh workbook: Results (one clean row per invoice), Summary, Details (audit columns)."""
    from pathlib import Path

    from openpyxl import Workbook
    from openpyxl.comments import Comment
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    from openpyxl.utils import get_column_letter

    font, bold = Font(name="Arial", size=10), Font(name="Arial", size=10, bold=True)
    head_fill = PatternFill("solid", start_color="1F3864")
    head_font = Font(name="Arial", size=10, bold=True, color="FFFFFF")
    line = Border(bottom=Side(style="thin", color="D9D9D9"))

    def header(ws, cols):
        for i, (h, w, *_) in enumerate(cols, start=1):
            c = ws.cell(row=1, column=i, value=h)
            c.font, c.fill = head_font, head_fill
            c.alignment = Alignment(vertical="center", wrap_text=True)
            ws.column_dimensions[get_column_letter(i)].width = w
        ws.row_dimensions[1].height = 30
        ws.freeze_panes = "A2"

    wb = Workbook()

    # ---- Results: one row per invoice -------------------------------------------------
    ws = wb.active
    ws.title = "Results"
    header(ws, RESULT_COLUMNS)
    L = {h: get_column_letter(i) for i, (h, *_) in enumerate(RESULT_COLUMNS, start=1)}
    for r, d in enumerate(decisions, start=2):
        people = [c for c in d.consultants]
        ts_hours = [c.timesheet_hours for c in people if c.timesheet_hours is not None]
        rates = {c.wo_rate for c in people if c.wo_rate is not None}
        invoiced = sum((c.invoice_amount for c in people if c.invoice_amount is not None), Decimal(0)) \
            if any(c.invoice_amount is not None for c in people) else None
        values = {
            "Verdict": d.decision, "Pay?": d.pay, "Message": d.summary,
            "Vendor": tidy_name(d.vendor) if d.vendor.isupper() else d.vendor,
            "Invoice #": d.invoice_number, "Consultant": ", ".join(c.name for c in people) or None,
            "Timesheet hours": sum(ts_hours, Decimal(0)) if len(ts_hours) == len(people) and people else None,
            "Rate ($/hr)": next(iter(rates)) if len(rates) == 1 else None,
            "Invoiced ($)": invoiced,
        }
        for h, w, kind in RESULT_COLUMNS:
            cell = ws[f"{L[h]}{r}"]
            if h in values:
                cell.value = _num(values[h])
            cell.font, cell.border = font, line
            cell.alignment = Alignment(vertical="top", wrap_text=kind == "wrap")
            if kind == "money":
                cell.number_format = MONEY
        t, rate, inv = L["Timesheet hours"], L["Rate ($/hr)"], L["Invoiced ($)"]
        exp = L["Expected ($)"]
        if len(rates) <= 1:
            ws[f"{exp}{r}"] = f'=IF(AND(ISNUMBER({t}{r}),ISNUMBER({rate}{r})),{t}{r}*{rate}{r},"")'
        else:                                   # several consultants at different rates: sum of each
            ws[f"{exp}{r}"] = _num(sum((c.expected_amount for c in people if c.expected_amount is not None),
                                       Decimal(0)))
            ws[f"{exp}{r}"].comment = Comment("Several consultants at different rates: sum of each "
                                              "consultant's timesheet hours x rate (see Details).", "invoice-check")
        ws[f"{L['Difference ($)']}{r}"] = f'=IF(AND(ISNUMBER({inv}{r}),ISNUMBER({exp}{r})),{inv}{r}-{exp}{r},"")'
        fill = PatternFill("solid", start_color=FILLS[d.decision])
        for h in ("Verdict", "Pay?"):
            ws[f"{L[h]}{r}"].fill = fill
        ws[f"{L['Verdict']}{r}"].font = bold
        ws[f"{L['Pay?']}{r}"].font = bold
    last = max(len(decisions) + 1, 2)
    ws.auto_filter.ref = f"A1:{get_column_letter(len(RESULT_COLUMNS))}{last}"

    # ---- Summary ------------------------------------------------------------------------
    sm = wb.create_sheet("Summary")
    for col_, w in zip("ABCD", (26, 26, 12, 16)):
        sm.column_dimensions[col_].width = w
    sm["A1"] = "Invoice payment check"
    sm["A1"].font = Font(name="Arial", size=13, bold=True)
    sm["A2"] = f"Timesheet: {ts.period_start} to {ts.period_end} ({ts.path.name})"
    sm["A2"].font = Font(name="Arial", size=10, color="595959")
    for j, h in enumerate(["Verdict", "Meaning", "Invoices", "Amount ($)"], start=1):
        c = sm.cell(row=4, column=j, value=h)
        c.font, c.fill = head_font, head_fill
    v, a = L["Verdict"], L["Invoiced ($)"]
    for i, (verdict, meaning) in enumerate([(GO, "Go ahead with payment"), (NO, "Do not pay"),
                                            (REVIEW, "Needs a person to check")], start=5):
        sm.cell(row=i, column=1, value=verdict).font = bold
        sm.cell(row=i, column=1).fill = PatternFill("solid", start_color=FILLS[verdict])
        sm.cell(row=i, column=2, value=meaning).font = font
        sm.cell(row=i, column=3, value=f"=COUNTIF(Results!${v}$2:${v}${last},A{i})").font = font
        sm.cell(row=i, column=4, value=f"=SUMIFS(Results!${a}$2:${a}${last},Results!${v}$2:${v}${last},A{i})").font = font
        sm.cell(row=i, column=4).number_format = MONEY
    sm["A8"], sm["C8"], sm["D8"] = "Total", "=SUM(C5:C7)", "=SUM(D5:D7)"
    for c_ in ("A8", "C8", "D8"):
        sm[c_].font = bold
        sm[c_].border = Border(top=Side(style="thin"))
    sm["D8"].number_format = MONEY
    notes = ["Where the numbers come from",
             f"Timesheet hours: ATT Time Report for {ts.period_start:%B %Y}, regular + overtime, by ATT ID.",
             "Rate: the work order in force for the billing period.",
             "Expected = Timesheet hours x Rate (formula). Difference = Invoiced - Expected (formula).",
             "Invoiced: the hours lines on the invoice, verified to appear on it.",
             "Full reasons, file names and work order dates are on the Details sheet."]
    for k, text in enumerate(notes, start=10):
        sm.cell(row=k, column=1, value=text).font = bold if k == 10 else Font(name="Arial", size=10, color="595959")
    if errors:
        sm.cell(row=17, column=1, value="Not checked").font = bold
        for k, e in enumerate(errors, start=18):
            sm.cell(row=k, column=1, value=e).font = font

    # ---- Details: audit columns -----------------------------------------------------------
    dt = wb.create_sheet("Details")
    header(dt, DETAIL_COLUMNS)
    r = 2
    for d in decisions:
        for c in d.consultants or [None]:
            row = [d.invoice_number, d.vendor, d.file, c.invoice_name if c else None, c.attuid if c else None, d.period,
                   c.invoice_hours if c else None, c.invoice_rate if c else None, d.subtotal, d.total,
                   c.wo_valid if c else None, "\n".join(d.reasons) or None, "\n".join(d.warnings) or None]
            for j, val in enumerate(row, start=1):
                cell = dt.cell(row=r, column=j, value=_num(val))
                cell.font, cell.border = font, line
                cell.alignment = Alignment(vertical="top", wrap_text=j >= 12)
                if j in (8, 9, 10):
                    cell.number_format = MONEY
            r += 1

    wb.calculation.fullCalcOnLoad = True          # Excel computes the formulas when the file opens
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    wb.save(path)
