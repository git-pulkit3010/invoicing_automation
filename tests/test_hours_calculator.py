"""Checks the calculator against the worked example in count_hour_logic.md.

Run with:  python -m pytest tests -q     (or)  python tests/test_hours_calculator.py
"""

import datetime as dt
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from hours_calculator import find_workbook, load_report  # noqa: E402


def _report():
    path = find_workbook(ROOT)
    assert path is not None, "sample workbook not found"
    return load_report(path)


def test_harish_giriraju_october_2026_totals():
    report = _report()
    result = report.compute("hg5064", 2026, 10)

    assert result.total_hours == 160
    assert result.regular_hours == 160
    assert result.ot_hours == 0
    assert result.days_recorded == 20
    assert result.benchmark_hours == 176
    assert result.weekdays_in_month == 22


def test_harish_weekly_breakdown_matches_the_document():
    report = _report()
    result = report.compute("hg5064", 2026, 10)

    weekly = [(w.days_recorded, w.total_hours) for w in result.weeks]
    assert weekly == [(2, 16), (5, 40), (3, 24), (5, 40), (5, 40)]

    # The workbook's own weekly totals must agree with the summed daily hours.
    for week in result.weeks:
        assert week.workbook_weekly_hours == week.total_hours


def test_harish_missing_weekdays():
    report = _report()
    result = report.compute("hg5064", 2026, 10)

    missing = [d.isoformat() for d in result.missing_weekdays]
    assert missing == ["2026-10-12", "2026-10-13"]
    assert result.difference_from_benchmark == -16


def test_lookups_by_name_and_metadata():
    report = _report()

    contractor = report.contractor_by_name("HARISH GIRIRAJU")
    assert contractor is not None
    assert contractor.attuid == "hg5064"
    assert contractor.full_name == "HARISH GIRIRAJU"

    assert len(report.contractors) == 133
    assert 2026 in report.years
    assert report.months_with_data[0]["month_name"] == "October"


def test_every_contractor_month_sum_is_consistent():
    """The weekly rollups must always add up to the monthly total."""
    report = _report()
    for period in report.months_with_data:
        for contractor in report.contractors:
            result = report.compute(contractor.attuid, period["year"], period["month"])
            if not result.daily:
                continue
            assert sum(w.total_hours for w in result.weeks) == result.total_hours
            assert result.days_recorded == len({r.day for r in result.daily})


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS  {name}")
            except AssertionError as exc:
                failures += 1
                print(f"FAIL  {name}: {exc}")
    print("\nAll good." if not failures else f"\n{failures} test(s) failed.")
    sys.exit(1 if failures else 0)
