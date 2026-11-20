"""Generators for invented test documents: timesheets, work orders, invoices.

Every name, vendor, rate and amount here is made up.
"""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

from openpyxl import Workbook
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas

HEADER = ["Period Year", "Period Month", "Period Week", "Period Day", "Week Start Date", "Week End Date",
          "Vendor ID", "Vendor Name", "Supervisor ATTUID", "Segment ID", "Segment Name", "Contractor ATTUID",
          "Contractor First Name", "Contractor Last Name", "Correcting Hours Indicator", "Regular Hours by Day",
          "OT Hours by Day", "Period Week Regular Hours", "Period Week OT Hours"]


def weekdays(year: int, month: int) -> list[date]:
    d, out = date(year, month, 1), []
    while d.month == month:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def make_timesheet(path: Path, year: int, month: int, people: list[dict], banner: str | None = "auto",
                   extra_rows: list[list] = ()) -> Path:
    """people: dicts with attuid, first, last, days (list of dates) and optional hours / ot per day."""
    wb = Workbook(); ws = wb.active; ws.title = "ATT Time Report"
    ws.append(["CMPM ATT Time Report"])
    last = (date(year, month % 12 + 1, 1) if month < 12 else date(year + 1, 1, 1)) - timedelta(days=1)
    if banner == "auto":
        banner = f"HOURS: {month}/1/{year} - {month}/{last.day}/{year}"
    ws.append([banner or "Report"])
    ws.append(HEADER)
    for p in people:
        for d in p["days"]:
            ws.append([d.year, d.month, "1", d.day, None, None, "V1", "CompNova LLC", "sup1", 1, "Seg",
                       p["attuid"], p["first"], p["last"], "N", p.get("hours", 8), p.get("ot", 0), 40, 0])
    for r in extra_rows:
        ws.append(r)
    wb.save(path)
    return path


def make_work_order(path: Path, vendor: str, consultant: str, rate: str, start: str, end: str,
                    status: str | None = "Verified") -> Path:
    """CompNova-style form: 'Label : value' rows, value drawn slightly above the label."""
    c = canvas.Canvas(str(path), pagesize=letter)
    c.setFont("Helvetica-Bold", 13); c.drawString(55, 740, "Vendor Work Order")
    y = 700
    for label, value in [("Name of Vendor", vendor), ("Name of Consultant", consultant),
                         ("Billing Rate", f"USD {rate} hourly"),
                         ("Client Name where services to be provided", "AT&T"),
                         ("Auth. Start Date", start), ("Est. End Date", end)]:
        c.setFont("Helvetica", 10); c.drawString(55, y, label); c.drawString(307, y, ":")
        c.drawString(327, y + 3, value)
        y -= 26
    c.showPage()
    if status:
        c.setFont("Helvetica", 10); c.drawString(55, 700, f"Status: {status}")
    c.save()
    return path


def make_invoice(path: Path, vendor: str, number: str, lines: list[tuple], total: str,
                 period: str = "10/01/2026 - 10/31/2026", extra: list[tuple] = (),
                 columns: tuple = ("Description", "Quantity", "Rate", "Amount")) -> Path:
    """lines: (consultant, hours, rate, amount) as printed strings. extra: (label, amount) rows."""
    c = canvas.Canvas(str(path), pagesize=letter)
    c.setFont("Helvetica-Bold", 14); c.drawString(50, 740, vendor)
    c.setFont("Helvetica", 10)
    c.drawString(50, 722, "100 Example Road, Springfield, NJ 07000")
    c.drawString(400, 740, "INVOICE")
    c.drawString(400, 722, f"Invoice #: {number}")
    c.drawString(400, 706, "Invoice Date: 11/05/2026")
    c.drawString(50, 690, "Bill To: CompNova, LLC")
    c.drawString(50, 672, f"Billing Period: {period}")
    xs = {"Description": 50, "Quantity": 300, "Rate": 380, "Amount": 470}
    order = list(columns)
    pos = [50, 300, 380, 470]
    c.setFont("Helvetica-Bold", 10)
    for x, h in zip(pos, order):
        c.drawString(x, 640, h)
    c.line(45, 634, 560, 634)
    c.setFont("Helvetica", 10)
    y = 618
    for name, hours, rate, amount in lines:
        vals = {"Description": name, "Quantity": hours, "Rate": rate, "Amount": amount}
        for x, h in zip(pos, order):
            c.drawString(x, y, vals[h])
        y -= 18
    for label, amount in extra:
        c.drawString(50, y, label); c.drawString(470, y, amount)
        y -= 18
    c.line(45, y + 8, 560, y + 8)
    c.drawString(380, y - 8, "Subtotal"); c.drawString(470, y - 8, total)
    c.setFont("Helvetica-Bold", 10)
    c.drawString(380, y - 26, "Total"); c.drawString(470, y - 26, total)
    c.save()
    return path


def make_scan(path: Path) -> Path:
    """A page with no text layer at all."""
    c = canvas.Canvas(str(path), pagesize=letter)
    c.rect(50, 400, 300, 200, fill=1)
    c.save()
    return path
