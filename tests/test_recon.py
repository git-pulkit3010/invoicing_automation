"""Regression tests for the reconciliation toolkit.

These lock in the behaviour that was verified by hand against the October
2026 source documents, so a future change to the parsers cannot silently
alter a reconciliation result.
"""

from __future__ import annotations

import sys
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from recon.config import ReconConfig
from recon.extract.invoice import parse_invoice
from recon.extract.timesheet import load_timesheet
from recon.extract.workorder import parse_work_order
from recon.models import Status
from recon.names import NameRoster, name_tokens, normalize_name
from recon.numeric import (
    currency_of,
    find_date_range,
    find_month,
    merge_digit_runs,
    parse_date,
    parse_number,
)
from recon.reconcile import Reconciler


@pytest.fixture(scope="module")
def config() -> ReconConfig:
    return ReconConfig.from_root(ROOT)


@pytest.fixture(scope="module")
def report(config):
    return load_timesheet(config.excel)


@pytest.fixture(scope="module")
def reconciliation(config):
    return Reconciler(config).run()


# --------------------------------------------------------------------------
# Timesheet
# --------------------------------------------------------------------------

def test_report_period_comes_from_the_banner(report):
    assert report.period_start == date(2026, 10, 1)
    assert report.period_end == date(2026, 10, 31)


def test_report_contains_every_contractor(report):
    assert len(report.contractors) == 133


def test_harish_totals_match_the_verified_figure(report):
    harish = report.contractors["hg5064"]
    assert harish.full_name == "HARISH GIRIRAJU"
    assert harish.total_hours == Decimal(160)
    assert harish.regular_hours == Decimal(160)
    assert harish.ot_hours == Decimal(0)
    assert harish.days_recorded == 20
    assert harish.missing_weekdays == [12, 13]


def test_harish_weekly_breakdown_matches_the_verified_figure(report):
    harish = report.contractors["hg5064"]
    buckets = [(b.week_start, b.days, b.hours) for b in harish.weeks]
    assert buckets == [
        (date(2026, 9, 27), 2, Decimal(16)),
        (date(2026, 10, 4), 5, Decimal(40)),
        (date(2026, 10, 11), 3, Decimal(24)),
        (date(2026, 10, 18), 5, Decimal(40)),
        (date(2026, 10, 25), 5, Decimal(40)),
    ]
    assert sum(b.hours for b in harish.weeks) == harish.total_hours


def test_partially_worked_days_are_summed_not_averaged(report):
    """Lila Ward worked 22 days but only 167 hours."""
    lila = report.contractors["lw3021"]
    assert lila.days_recorded == 22
    assert lila.total_hours == Decimal(167)
    assert lila.total_hours != Decimal(8 * lila.days_recorded)


def test_out_of_period_rows_are_excluded_by_default(report):
    """SAI PRASAD JONNALA's only row belongs to September, not October."""
    jonnala = report.contractors["sj1016"]
    assert jonnala.total_hours == Decimal(0)
    assert any("outside" in flag for flag in jonnala.flags)


def test_out_of_period_rows_can_be_included(config):
    report = load_timesheet(config.excel, include_out_of_period_rows=True)
    assert report.contractors["sj1016"].total_hours == Decimal(176)


def test_aggregate_only_contractor_is_flagged(report):
    """A single row carrying a monthly total is not a daily entry."""
    kakkireni = report.contractors["sk6604"]
    assert kakkireni.total_hours == Decimal(176)
    assert kakkireni.days_recorded == 1
    assert any("aggregate" in flag for flag in kakkireni.flags)


def test_overtime_is_zero_across_the_whole_report(report):
    assert all(c.ot_hours == 0 for c in report.contractors.values())


# --------------------------------------------------------------------------
# Numeric helpers
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "text,expected",
    [
        ("160", Decimal(160)),
        ("73.00", Decimal("73.00")),
        ("$15,200.00", Decimal("15200.00")),
        ("11680.00", Decimal("11680.00")),
        ("0", Decimal(0)),
    ],
)
def test_parse_number_accepts_plain_numbers(text, expected):
    assert parse_number(text) == expected


@pytest.mark.parametrize(
    "text",
    ["1.5%", "10/01/2026", "57977W", "Comp2026-11VB", "TX:ITS", "AT&T", "", None],
)
def test_parse_number_rejects_everything_else(text):
    assert parse_number(text) is None


def test_merge_digit_runs_rejoins_split_digits():
    assert merge_digit_runs("USD 8 3".split()) == "USD 83"
    assert merge_digit_runs(["83"]) == "83"
    assert merge_digit_runs(["4", "5"]) == "45"


def test_currency_detection():
    assert currency_of("$") == "USD"
    assert currency_of("USD") == "USD"
    assert currency_of("EUR") == "EUR"


def test_parse_date_handles_both_year_forms():
    assert parse_date("11/17/2026") == date(2026, 11, 17)
    assert parse_date("04/08/25") == date(2025, 4, 8)
    assert parse_date("12/1/26") == date(2026, 12, 1)


def test_find_date_range_and_month():
    assert find_date_range("Services 10/01/2026 - 10/31/2026") == (
        date(2026, 10, 1), date(2026, 10, 31),
    )
    assert find_month("Services for Oct 2026") == (
        date(2026, 10, 1), date(2026, 10, 31),
    )
    assert find_month("Services for the month of October 2026") == (
        date(2026, 10, 1), date(2026, 10, 31),
    )


# --------------------------------------------------------------------------
# Names
# --------------------------------------------------------------------------

def test_names_normalise_case_and_punctuation():
    assert normalize_name("chaitanya  kumar nara") == "CHAITANYA KUMAR NARA"
    assert normalize_name("Chaitanya, Motupalli.") == "CHAITANYA MOTUPALLI"


def test_roster_matches_on_token_set_not_substring():
    roster = NameRoster()
    roster.add("CHAITANYA MOTUPALLI", "cx6008")
    roster.add("CHAITANYA KUMAR NARA", "cn7957")
    roster.add("HARISH GIRIRAJU", "hg5064")
    roster.add("HARISH SUGGUNA", "hs4598")

    assert roster.match("Chaitanya Motupalli") == "cx6008"
    assert roster.match("Chaitanya Kumar Nara") == "cn7957"
    assert roster.match("Harish Giriraju") == "hg5064"
    assert roster.match("Chaitanya") is None
    assert roster.match("Motupalli") is None


def test_find_in_text_prefers_the_longest_name():
    roster = NameRoster()
    roster.add("VAIBHAV BANSAL", "vb771h")
    match = roster.find_in_text(["Consulting", "Vaibhav", "Bansal"])
    assert match is not None
    assert match[1] == "vb771h"


# --------------------------------------------------------------------------
# Invoice parsing
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "filename,quantity,rate,amount,contractor",
    [
        ("Indotronix International Corporation_Harish Giriraju_1001-103126.pdf",
         Decimal(160), Decimal("73.00"), Decimal("11680.00"), "Harish Giriraju"),
        ("Edge Centric_Chaitanya Motupalli_1001-103126.pdf",
         Decimal(152), Decimal("100.00"), Decimal("15200.00"), "Chaitanya Motupalli"),
        ("Itek Info_Srilasya Samudrala_1001-103126.pdf",
         Decimal(176), Decimal("83.00"), Decimal("14608.00"), "Srilasya Samudrala"),
        ("Simple CTI Solutions_Vaibhav Bansal_1001-103126.pdf",
         Decimal("176.00"), Decimal("98.00"), Decimal("17248.00"), "Vaibhav Bansal"),
    ],
)
def test_each_invoice_template_yields_one_line_item(
    config, filename, quantity, rate, amount, contractor
):
    invoice = parse_invoice(config.invoices_dir / filename)
    assert len(invoice.lines) == 1
    line = invoice.lines[0]
    assert line.quantity == quantity
    assert line.rate == rate
    assert line.amount == amount
    assert line.contractor_name == contractor


@pytest.mark.parametrize(
    "filename,period_start,period_end",
    [
        ("Indotronix International Corporation_Harish Giriraju_1001-103126.pdf",
         date(2026, 10, 1), date(2026, 10, 31)),
        ("Edge Centric_Chaitanya Motupalli_1001-103126.pdf",
         date(2026, 10, 1), date(2026, 10, 31)),
        ("Itek Info_Srilasya Samudrala_1001-103126.pdf",
         date(2026, 10, 1), date(2026, 10, 31)),
        ("Simple CTI Solutions_Vaibhav Bansal_1001-103126.pdf",
         date(2026, 10, 1), date(2026, 10, 31)),
    ],
)
def test_every_template_yields_the_same_service_period(
    config, filename, period_start, period_end
):
    invoice = parse_invoice(config.invoices_dir / filename)
    assert invoice.period_start == period_start
    assert invoice.period_end == period_end


def test_invoice_numbers_are_not_confused_with_other_headings(config):
    invoice = parse_invoice(
        config.invoices_dir
        / "Indotronix International Corporation_Harish Giriraju_1001-103126.pdf"
    )
    assert invoice.invoice_number == "57977W"
    assert invoice.invoice_date == date(2026, 11, 17)
    assert invoice.due_date == date(2027, 1, 1)
    assert invoice.terms == "Net 45"


# --------------------------------------------------------------------------
# Work order parsing
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "filename,consultant,rate,auth_start,est_end",
    [
        ("Harish Giriraju Vendor_Work_Order_CN.pdf", "Harish Giriraju",
         Decimal(73), date(2025, 4, 8), date(2026, 3, 31)),
        ("Chaitanya Motupalli Vendor_Work_Order_CN.pdf", "Chaitanya Motupalli",
         Decimal(100), date(2026, 4, 7), date(2026, 12, 31)),
        ("Srilasya Samudrala Vendor_Work_Order_CN.pdf", "Srilasya Samudrala",
         Decimal(83), date(2025, 12, 29), date(2026, 12, 31)),
        ("Chaitanya Nara Vendor_Work_Order_CN.pdf", "Chaitanya KUMAR Nara",
         Decimal(71), date(2025, 3, 17), date(2026, 3, 17)),
    ],
)
def test_work_order_fields(
    config, filename, consultant, rate, auth_start, est_end
):
    work_order = parse_work_order(config.work_orders_dir / filename)
    assert work_order.consultant_name == consultant
    assert work_order.rate == rate
    assert work_order.currency == "USD"
    assert work_order.rate_basis == "hourly"
    assert work_order.auth_start == auth_start
    assert work_order.est_end == est_end


def test_work_order_reference_is_read_from_the_labelled_field(config):
    work_order = parse_work_order(
        config.work_orders_dir / "Chaitanya Motupalli Vendor_Work_Order_CN.pdf"
    )
    assert work_order.work_order_ref == "PLC-3482"


def test_unlabelled_work_order_reference_is_recovered(config):
    """Harish's work order shows a bare 'C2C' instead of a labelled field."""
    work_order = parse_work_order(
        config.work_orders_dir / "Harish Giriraju Vendor_Work_Order_CN.pdf"
    )
    assert work_order.work_order_ref == "C2C"


# --------------------------------------------------------------------------
# Reconciliation
# --------------------------------------------------------------------------

def test_all_four_invoices_are_reconciled(reconciliation):
    assert len(reconciliation.reconciliations) == 4
    assert not reconciliation.errors


def test_results_by_contractor(reconciliation):
    by_name = {r.line.contractor_name: r for r in reconciliation.reconciliations}
    assert by_name["Harish Giriraju"].status is Status.FAIL
    assert by_name["Chaitanya Motupalli"].status is Status.PASS
    assert by_name["Srilasya Samudrala"].status is Status.PASS
    assert by_name["Vaibhav Bansal"].status is Status.FAIL


def test_harish_fails_because_the_work_order_expired(reconciliation):
    harish = next(r for r in reconciliation.reconciliations
                   if r.line.contractor_name == "Harish Giriraju")
    failing = [c for c in harish.checks if c.status is Status.FAIL]
    assert [c.key for c in failing] == ["period_in_work_order"]
    assert "expired" in failing[0].detail


def test_vaibhav_fails_because_no_work_order_exists(reconciliation):
    vaibhav = next(r for r in reconciliation.reconciliations
                    if r.line.contractor_name == "Vaibhav Bansal")
    assert vaibhav.work_order is None
    failing = [c for c in vaibhav.checks if c.status is Status.FAIL]
    assert [c.key for c in failing] == ["work_order_found"]


def test_missing_weekdays_are_an_advisory_not_a_failure(reconciliation):
    harish = next(r for r in reconciliation.reconciliations
                   if r.line.contractor_name == "Harish Giriraju")
    coverage = next(c for c in harish.checks if c.key == "missing_weekdays")
    assert coverage.status is Status.WARN
    assert harish.status is Status.FAIL  # because of the work order, not the gap


def test_headline_identity_holds_for_every_invoice(reconciliation):
    """hours x work order rate == invoice amount wherever a work order exists."""
    for reconciliation_row in reconciliation.reconciliations:
        contractor = reconciliation_row.contractor
        work_order = reconciliation_row.work_order
        if contractor is None or work_order is None or work_order.rate is None:
            continue
        expected = contractor.total_hours * work_order.rate
        assert expected == reconciliation_row.line.amount


def test_chaitanya_work_orders_are_not_crossed(reconciliation):
    """'Chaitanya Motupalli' must not pick up 'Chaitanya Kumar Nara's order."""
    motupalli = next(r for r in reconciliation.reconciliations
                     if r.line.contractor_name == "Chaitanya Motupalli")
    assert motupalli.work_order is not None
    assert motupalli.work_order.consultant_name == "Chaitanya Motupalli"
    assert motupalli.work_order.rate == Decimal(100)


def test_unlinked_work_order_is_reported(reconciliation):
    names = {w.consultant_name for w in reconciliation.unlinked_work_orders}
    assert "Chaitanya KUMAR Nara" in names
