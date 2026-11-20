"""PDF handling with generated (invented) documents: work order form, wrapped table cells, scans."""

from datetime import date
from decimal import Decimal

from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas

from invoice_check import pdf_to_markdown
from invoice_check.convert import convert_folder
from invoice_check.workorders import parse_work_order


def _wo_pdf(path, rate_text="USD 8 3 hourly", value_dy=4):
    """Form like the real one: label, colon, value drawn slightly higher than the label."""
    c = canvas.Canvas(str(path), pagesize=letter)
    y = 700
    for label, value in [("Name of Vendor", "Acme LLC"), ("Name of Consultant", "Alice Smith"),
                         ("Billing Rate", rate_text), ("Client Name where services to be provided", "AT&T"),
                         ("Auth. Start Date", "01/05/26"), ("Est. End Date", "12/31/26")]:
        c.setFont("Helvetica", 10); c.drawString(55, y, label); c.drawString(307, y, ":")
        c.drawString(327, y + value_dy, value)
        y -= 28
    c.showPage(); c.drawString(55, 700, "Status: Verified"); c.save()


def test_work_order_form_is_read(tmp_path):
    p = tmp_path / "wo.pdf"
    _wo_pdf(p)
    w = parse_work_order(p)
    assert w["consultant_name"] == "Alice Smith" and w["vendor"] == "Acme LLC"
    assert (w["currency"], w["rate"], w["rate_basis"]) == ("USD", Decimal(83), "hourly")   # '8 3' joined
    assert (w["start"], w["end"], w["status"]) == (date(2026, 1, 5), date(2026, 12, 31), "Verified")


def test_wrapped_table_cells_are_joined(tmp_path):
    """Narrow columns wrap 'Ali-/son' and '$11,26/4.00' onto a second line."""
    p = tmp_path / "inv.pdf"
    c = canvas.Canvas(str(p), pagesize=letter)
    c.setFont("Helvetica", 9)
    heads = [(20, "Resource"), (150, "Start Date"), (350, "Rate"), (415, "Quantity"), (546, "Amount")]
    for x, t in heads:
        c.drawString(x, 450, t)
    for x, t in [(20, "Ali-"), (150, "11/01/2026"), (350, "$64.00"), (415, "176"), (546, "$11,26")]:
        c.drawString(x, 420, t)
    for x, t in [(20, "son"), (546, "4.00")]:
        c.drawString(x, 409, t)
    c.save()
    out = tmp_path / "inv.md"
    pdf_to_markdown.convert(p, out)
    md = out.read_text()
    assert "| Alison | 11/01/2026 | $64.00 | 176 | $11,264.00 |" in md, md


def test_scanned_pdf_is_flagged(tmp_path):
    inv = tmp_path / "invoices"; inv.mkdir()
    c = canvas.Canvas(str(inv / "scan.pdf"), pagesize=letter)
    c.rect(50, 50, 200, 200, fill=1)          # a page with no text at all
    c.save()
    m = convert_folder(inv, tmp_path / "work")
    assert m["scan.pdf"]["has_text"] is False
