"""Decision rules, tested with invented data only (no real invoices in the repo)."""

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from openpyxl import Workbook

from invoice_check import workorders
from invoice_check.timesheet import ContractorTime, Timesheet, TimesheetError, load_timesheet
from invoice_check.verify import GO, NO, REVIEW, decide, record
from invoice_check.workorders import WorkOrder

START, END = date(2026, 11, 1), date(2026, 11, 30)


def ts(**people) -> Timesheet:
    """people: attuid=(name, regular_hours[, ot_hours])"""
    out = {}
    for attuid, spec in people.items():
        name, reg, *ot = spec
        c = ContractorTime(attuid, name, Decimal(reg), Decimal(ot[0] if ot else 0))
        c.days = {d for d in (date(2026, 11, x) for x in range(1, 31)) if d.weekday() < 5}
        out[attuid] = c
    return Timesheet(Path("ts.xlsx"), START, END, out)


def wo(id, attuid, name, vendor, rate, start=date(2026, 1, 1), end=date(2026, 12, 31), **kw) -> WorkOrder:
    return WorkOrder(id=id, attuid=attuid, consultant_name=name, aliases=kw.get("aliases", []), vendor=vendor,
                     rate=Decimal(rate), currency="USD", rate_basis="hourly", start=start, end=end,
                     status=kw.get("status", "Verified"), source_file=f"wo{id}.pdf", link_method="exact",
                     confirmed=True)


def inv(lines, total, vendor="Acme LLC", number="INV-1", period=("2026-11-01", "2026-11-30")):
    return {"file": "x.pdf", "vendor": vendor, "invoice_number": number, "period_start": period[0],
            "period_end": period[1], "currency": "USD", "total": total,
            "lines": [dict(consultant=c, hours=h, rate=r, amount=a) for c, h, r, a in lines]}


def md_for(ext) -> str:
    """Markdown containing exactly the numbers on the invoice."""
    rows = "\n".join(f"| {l['consultant']} | {l['hours']} | ${l['rate']} | ${l['amount']} |" for l in ext["lines"])
    return f"Invoice # {ext['invoice_number']}\n\n| Name | Hours | Rate | Amount |\n|---|---|---|---|\n{rows}\n\nTotal ${ext['total']}\n"


ALICE_TS = ts(as001=("ALICE SMITH", 160))
ALICE_WO = [wo(1, "as001", "Alice Smith", "Acme LLC", "100")]


def run(ext, timesheet=ALICE_TS, orders=ALICE_WO, md=None, has_text=True, con=None, sha="h"):
    return decide(ext, md if md is not None else md_for(ext), has_text, sha, timesheet, orders, con)


def test_clean_invoice_is_go():
    d = run(inv([("Alice Smith", "160", "100.00", "16000.00")], "16000.00"))
    assert d.decision == GO, d.reasons


def test_extra_charge_on_total_is_no():
    ext = inv([("Alice Smith", "160", "100.00", "16000.00")], "18500.00")
    d = run(ext, md=md_for(ext) + "\nAdministration fee $2,500.00\n")
    assert d.decision == NO
    assert any("sum of its hours lines" in r for r in d.reasons)


def test_month_split_over_two_lines_is_go():
    d = run(inv([("Alice Smith", "80", "100.00", "8000.00"), ("Alice Smith", "80", "100.00", "8000.00")], "16000.00"))
    assert d.decision == GO, d.reasons


def test_number_not_on_invoice_is_review():
    ext = inv([("Alice Smith", "160", "100.00", "16000.00")], "16000.00")
    md = md_for(ext).replace("| 160 |", "| 152 |")          # agent reported a value that isn't printed
    d = run(ext, md=md)
    assert d.decision == REVIEW
    assert any("does not appear on the invoice" in r for r in d.reasons)


def test_scanned_invoice_is_always_review():
    d = run(inv([("Alice Smith", "160", "100.00", "16000.00")], "16000.00"), md="<!-- scanned -->", has_text=False)
    assert d.decision == REVIEW


def test_expired_work_order_needs_review_even_when_amount_agrees():
    orders = [wo(1, "as001", "Alice Smith", "Acme LLC", "100", end=date(2026, 3, 31))]
    d = run(inv([("Alice Smith", "160", "100.00", "16000.00")], "16000.00"), orders=orders)
    assert d.decision == REVIEW and "expired on 2026-03-31" in d.reasons[0]


def test_expired_work_order_with_wrong_hours_is_a_mismatch():
    orders = [wo(1, "as001", "Alice Smith", "Acme LLC", "100", end=date(2026, 3, 31))]
    d = run(inv([("Alice Smith", "168", "100.00", "16800.00")], "16800.00"), orders=orders)
    assert d.decision == NO and any("timesheet shows 160" in r for r in d.reasons)


def test_renewed_work_order_is_used():
    orders = [wo(1, "as001", "Alice Smith", "Acme LLC", "90", end=date(2026, 3, 31)),
              wo(2, "as001", "Alice Smith", "Acme LLC", "100", start=date(2026, 4, 1), end=date(2027, 3, 31))]
    d = run(inv([("Alice Smith", "160", "100.00", "16000.00")], "16000.00"), orders=orders)
    assert d.decision == GO, d.reasons


def test_wrong_rate_is_no():
    d = run(inv([("Alice Smith", "160", "110.00", "17600.00")], "17600.00"))
    assert d.decision == NO and any("not the work order rate" in r for r in d.reasons)


def test_wrong_hours_is_no():
    d = run(inv([("Alice Smith", "168", "100.00", "16800.00")], "16800.00"))
    assert d.decision == NO and any("timesheet shows 160" in r for r in d.reasons)


def test_bad_line_arithmetic_is_no():
    d = run(inv([("Alice Smith", "160", "100.00", "16500.00")], "16500.00"))
    assert d.decision == NO and any("but the line says" in r for r in d.reasons)


def test_other_month_needs_review():
    d = run(inv([("Alice Smith", "160", "100.00", "16000.00")], "16000.00", period=("2026-10-01", "2026-10-31")))
    assert d.decision == REVIEW and any("not the timesheet month" in r for r in d.reasons)


def test_no_work_order_needs_review():
    d = run(inv([("Bob Jones", "160", "100.00", "16000.00")], "16000.00"),
            timesheet=ts(as001=("ALICE SMITH", 160), bj002=("BOB JONES", 160)))
    assert d.decision == REVIEW and "no work order on record for Bob Jones" in d.reasons[0]


def test_consultant_not_in_timesheet_needs_review():
    d = run(inv([("Alice Smith", "160", "100.00", "16000.00")], "16000.00"), timesheet=ts(zz9=("ZOE PARK", 160)))
    assert d.decision == REVIEW and "not in the master timesheet" in d.reasons[0]


def test_consultant_with_zero_hours_is_a_mismatch():
    d = run(inv([("Alice Smith", "160", "100.00", "16000.00")], "16000.00"), timesheet=ts(as001=("ALICE SMITH", 0)))
    assert d.decision == NO and "no hours recorded" in d.reasons[0]


def test_tax_on_the_total_is_compared_via_the_subtotal():
    ext = inv([("Alice Smith", "160", "100.00", "16000.00")], "17280.00")
    ext["subtotal"] = "16000.00"
    d = run(ext, md=md_for(ext) + "\nSubtotal $16,000.00\nSales tax 8% $1,280.00\n")
    assert d.decision == GO, d.reasons
    assert any("differs from the subtotal" in w for w in d.warnings)


def test_fee_inside_the_subtotal_is_a_mismatch():
    ext = inv([("Alice Smith", "160", "100.00", "16000.00")], "18500.00")
    ext["subtotal"] = "18500.00"
    d = run(ext, md=md_for(ext) + "\nAdministration fee $2,500.00\nSubtotal $18,500.00\n")
    assert d.decision == NO and any("invoice subtotal 18,500.00 does not equal" in r for r in d.reasons)


def test_duplicate_timesheet_days_are_a_warning():
    sheet = ts(as001=("ALICE SMITH", 160))
    sheet.contractors["as001"].duplicate_days = {date(2026, 11, 3)}
    d = run(inv([("Alice Smith", "160", "100.00", "16000.00")], "16000.00"), timesheet=sheet)
    assert d.decision == GO and any("more than one timesheet row" in w for w in d.warnings)


def test_same_name_resolved_by_vendor():
    sheet = ts(as001=("ALICE SMITH", 160), as999=("ALICE SMITH", 120))
    orders = [wo(1, "as001", "Alice Smith", "Acme LLC", "100"), wo(2, "as999", "Alice Smith", "Gamma Inc", "90")]
    d = run(inv([("Alice Smith", "120", "90.00", "10800.00")], "10800.00", vendor="Gamma Inc"),
            timesheet=sheet, orders=orders)
    assert d.decision == GO, d.reasons
    assert d.consultants[0].attuid == "as999"


def test_same_name_same_vendor_needs_a_person():
    sheet = ts(as001=("ALICE SMITH", 160), as999=("ALICE SMITH", 120))
    orders = [wo(1, "as001", "Alice Smith", "Acme LLC", "100"), wo(2, "as999", "Alice Smith", "Acme LLC", "90")]
    d = run(inv([("Alice Smith", "160", "100.00", "16000.00")], "16000.00"), timesheet=sheet, orders=orders)
    assert d.decision == REVIEW and "more than one consultant" in d.reasons[0]


def test_shorter_name_on_invoice_matches_alias():
    sheet = ts(mr001=("MAYA LAKSHMI RAO KRISHNAN", 136))
    orders = [wo(1, "mr001", "Maya Rao", "Bluepeak Inc", "85", aliases=["MAYA LAKSHMI RAO KRISHNAN"])]
    d = run(inv([("Maya Lakshmi Rao Krishnan", "136", "85.00", "11560.00")], "11560.00", vendor="Bluepeak LLC"),
            timesheet=sheet, orders=orders)
    assert d.decision == GO, d.reasons


def test_overtime_needs_a_person():
    d = run(inv([("Alice Smith", "168", "100.00", "16800.00")], "16800.00"), timesheet=ts(as001=("ALICE SMITH", 160, 8)))
    assert d.decision == REVIEW and "overtime" in d.reasons[0]


def test_unlinked_consultant_needs_a_person():
    orders = [wo(1, None, "Alice Smith", "Acme LLC", "100")]
    d = run(inv([("Alice Smith", "160", "100.00", "16000.00")], "16000.00"), orders=orders)
    assert d.decision == REVIEW and "not linked to an ATT ID" in d.reasons[0]


def test_missing_field_is_review():
    ext = inv([("Alice Smith", "160", "100.00", "16000.00")], "16000.00")
    ext["invoice_number"] = None
    assert run(ext).decision == REVIEW


def test_duplicate_invoice_after_approval_is_no(tmp_path):
    con = workorders.connect(tmp_path / "db.sqlite")
    ext = inv([("Alice Smith", "160", "100.00", "16000.00")], "16000.00")
    first = run(ext, con=con)
    assert first.decision == GO
    record(con, first, "h")
    again = run(ext, con=con)
    assert again.decision == NO and "already approved" in again.reasons[0]


def test_resubmission_after_rejection_is_allowed(tmp_path):
    con = workorders.connect(tmp_path / "db.sqlite")
    bad = run(inv([("Alice Smith", "168", "100.00", "16800.00")], "16800.00"), con=con)
    record(con, bad, "h1")
    fixed = run(inv([("Alice Smith", "160", "100.00", "16000.00")], "16000.00"), con=con, sha="h2")
    assert fixed.decision == GO, fixed.reasons


# ---------------------------------------------------------------- timesheet --
def _sheet(path, banner=True, rows=()):
    wb = Workbook(); s = wb.active; s.title = "ATT Time Report"
    s.append([None]); s.append(["HOURS: 11/1/2026 - 11/30/2026" if banner else "Report"])
    s.append(["Period Year", "Period Month", "Period Week", "Period Day", "Week Start Date", "Week End Date",
              "Vendor ID", "Vendor Name", "Supervisor ATTUID", "Segment ID", "Segment Name", "Contractor ATTUID",
              "Contractor First Name", "Contractor Last Name", "Correcting Hours Indicator", "Regular Hours by Day",
              "OT Hours by Day", "Period Week Regular Hours", "Period Week OT Hours"])
    for r in rows:
        s.append(r)
    wb.save(path)


def _row(month, day, attuid="as001", reg=8, ot=0):
    return [2026, month, "1", day, None, None, "V", "CompNova LLC", "sup", 1, "seg", attuid, "Alice", "Smith", "N", reg, ot, 40, 0]


def test_timesheet_totals_and_out_of_period_rows(tmp_path):
    p = tmp_path / "ts.xlsx"
    _sheet(p, rows=[_row(11, 2), _row(11, 3, reg=4), _row(11, 4, ot=2), _row(10, 30)])
    t = load_timesheet(p)
    c = t.contractors["as001"]
    assert (c.regular, c.ot, c.total, c.excluded_rows) == (Decimal(20), Decimal(2), Decimal(22), 1)


def test_timesheet_without_banner_refuses_to_guess(tmp_path):
    p = tmp_path / "ts.xlsx"
    _sheet(p, banner=False, rows=[_row(11, 2)])
    with pytest.raises(TimesheetError):
        load_timesheet(p)


def test_report_handles_a_batch_with_no_go(tmp_path):
    from invoice_check.report import markdown
    d = run(inv([("Alice Smith", "168", "100.00", "16800.00")], "16800.00"))
    assert d.decision == NO
    out = markdown([d], ALICE_TS, [])
    assert "| Go ahead with payment | 0 | 0.00 |" in out and "**MISMATCH**" in out


# ------------------------------------------------------------------ messages --
def test_yes_message_states_the_amount_cross_checked():
    d = run(inv([("Alice Smith", "160", "100.00", "16000.00")], "16000.00"))
    assert d.pay == "Yes"
    assert d.summary == ("Yes, pay $16,000.00: 160 hrs x $100.00 = $16,000.00, "
                         "matches the timesheet and work order.")


def test_yes_message_mentions_tax_on_the_total():
    ext = inv([("Alice Smith", "160", "100.00", "16000.00")], "17280.00")
    ext["subtotal"] = "16000.00"
    d = run(ext, md=md_for(ext) + "\nSubtotal $16,000.00\n")
    assert d.summary.startswith("Yes, pay $17,280.00: 160 hrs x $100.00 = $16,000.00")
    assert "plus $1,280.00 tax/adjustments" in d.summary


def test_no_message_gives_the_reason_briefly():
    d = run(inv([("Alice Smith", "168", "100.00", "16800.00")], "16800.00"))
    assert d.pay == "No"
    assert d.summary.startswith("No: Alice Smith: invoice bills 168 hrs, timesheet shows 160 hrs")
    assert d.summary.endswith("(+1 more)")              # the amount check fails too


def test_review_message_is_short_and_detail_is_kept():
    orders = [wo(1, "as001", "Alice Smith", "Acme LLC", "100", end=date(2026, 3, 31))]
    d = run(inv([("Alice Smith", "160", "100.00", "16000.00")], "16000.00"), orders=orders)
    assert d.pay == "No"
    assert d.summary == "No, needs review: work order for Alice Smith expired on 2026-03-31"
    assert "get a renewed work order" in d.reasons[0]


def test_two_consultants_on_one_invoice():
    sheet = ts(as001=("ALICE SMITH", 160), bj002=("BOB JONES", 80))
    orders = ALICE_WO + [wo(2, "bj002", "Bob Jones", "Acme LLC", "90")]
    d = run(inv([("Alice Smith", "160", "100.00", "16000.00"), ("Bob Jones", "80", "90.00", "7200.00")], "23200.00"),
            timesheet=sheet, orders=orders)
    assert d.summary == ("Yes, pay $23,200.00: 2 consultants, $16,000.00 + $7,200.00, "
                         "all match the timesheet and work orders.")
