"""CMPM ATT Time Report reader.

This module encodes the hours-count logic that was verified against the
October 2026 report:

  * one row == one contractor working one day;
  * total hours  == SUM of "Regular Hours by Day" (column P) + SUM of
    "OT Hours by Day" (column Q);
  * days recorded == the number of daily rows for the contractor;
  * weekly buckets are grouped by "Week Start Date" (column E, a Sun-Sat
    pay week) and NOT by "Period Week" (column C), which is an ATT sequence
    counter that does not align with calendar weeks;
  * the workbook's own "Period Week Regular Hours" (column R) is a
    de-duplicated weekly total repeated on every row of that week, so it is
    only ever used as a cross-check, never summed directly.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Optional

from openpyxl import load_workbook

from ..models import ContractorHours, WeekBucket

# Column indices in the "ATT Time Report" sheet (0-based).
COL_YEAR = 0
COL_MONTH = 1
COL_PERIOD_WEEK = 2
COL_PERIOD_DAY = 3
COL_WEEK_START = 4
COL_WEEK_END = 5
COL_VENDOR_ID = 6
COL_VENDOR_NAME = 7
COL_SUPERVISOR = 8
COL_SEGMENT_ID = 9
COL_SEGMENT_NAME = 10
COL_ATTUID = 11
COL_FIRST_NAME = 12
COL_LAST_NAME = 13
COL_CORRECTING = 14
COL_REGULAR_HOURS = 15
COL_OT_HOURS = 16
COL_WEEK_REGULAR_HOURS = 17
COL_WEEK_OT_HOURS = 18

_PERIOD_RE = re.compile(r"(\d{1,2})/(\d{1,2})/(\d{4})\s*-\s*(\d{1,2})/(\d{1,2})/(\d{4})")


@dataclass
class TimesheetReport:
    """The whole time report, keyed by contractor ATTUID."""

    path: Path
    period_start: date
    period_end: date
    contractors: dict[str, ContractorHours] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

def _read_period(rows) -> tuple[date, date]:
    """Read the 'HOURS: mm/dd/yyyy - mm/dd/yyyy' banner, else infer from data."""
    for row in rows[:10]:
        if row and isinstance(row[0], str):
            match = _PERIOD_RE.search(row[0])
            if match:
                m1, d1, y1, m2, d2, y2 = (int(g) for g in match.groups())
                return date(y1, m1, d1), date(y2, m2, d2)
    return date(2026, 10, 1), date(2026, 10, 31)


def _weekdays_between(start: date, end: date) -> list[int]:
    """Every weekday day-of-month in the inclusive range."""
    days = []
    current = start
    while current <= end:
        if current.weekday() < 5:
            days.append(current.day)
        current += timedelta(days=1)
    return days


def _is_data_row(row: tuple) -> bool:
    """True for a real daily entry.

    The header row carries the literal text 'Contractor ATTUID' in the
    ATTUID column, so presence alone is not enough to identify data.
    """
    if row is None or len(row) <= COL_REGULAR_HOURS:
        return False
    attuid = row[COL_ATTUID]
    if not isinstance(attuid, str) or not attuid.strip():
        return False
    hours = row[COL_REGULAR_HOURS]
    if not isinstance(hours, (int, float, Decimal)) or isinstance(hours, bool):
        return False
    return True


def load_timesheet(path: Path, include_out_of_period_rows: bool = False) -> TimesheetReport:
    """Parse the ATT Time Report workbook into per-contractor hour totals."""
    workbook = load_workbook(path, read_only=True, data_only=True)
    try:
        sheet = workbook["ATT Time Report"]
        rows = list(sheet.iter_rows(values_only=True))
    finally:
        workbook.close()

    period_start, period_end = _read_period(rows)

    grouped: dict[str, list[tuple[int, tuple]]] = defaultdict(list)
    for row_number, row in enumerate(rows, start=1):
        if not _is_data_row(row):
            continue
        grouped[str(row[COL_ATTUID])].append((row_number, row))

    report = TimesheetReport(path=path, period_start=period_start, period_end=period_end)
    weekdays = _weekdays_between(period_start, period_end)

    for attuid, entries in grouped.items():
        contractor = _build_contractor(
            attuid, entries, period_start, period_end, weekdays,
            include_out_of_period_rows, report,
        )
        report.contractors[attuid] = contractor

    return report


def _build_contractor(
    attuid: str,
    entries: list[tuple[int, tuple]],
    period_start: date,
    period_end: date,
    weekdays: list[int],
    include_out_of_period_rows: bool,
    report: TimesheetReport,
) -> ContractorHours:
    sample = entries[0][1]
    flags: list[str] = []

    in_period: list[tuple[int, tuple]] = []
    for row_number, row in entries:
        row_month = row[COL_MONTH]
        row_year = row[COL_YEAR]
        row_date = _row_date(row)
        if row_date and not (period_start <= row_date <= period_end):
            if include_out_of_period_rows:
                in_period.append((row_number, row))
            else:
                flags.append(
                    f"row {row_number}: period {row_year}-{row_month:02d} is outside "
                    f"{period_start}..{period_end} and was excluded"
                )
            continue
        in_period.append((row_number, row))

    regular = sum((Decimal(str(row[COL_REGULAR_HOURS] or 0)) for _, row in in_period), Decimal(0))
    overtime = sum((Decimal(str(row[COL_OT_HOURS] or 0)) for _, row in in_period), Decimal(0))

    days_recorded = {int(row[COL_PERIOD_DAY]) for _, row in in_period}

    weeks: dict[date, dict] = defaultdict(
        lambda: {"hours": Decimal(0), "days": set(), "week_end": None}
    )
    for _, row in in_period:
        week_start = _as_date(row[COL_WEEK_START])
        bucket = weeks[week_start]
        bucket["hours"] += Decimal(str(row[COL_REGULAR_HOURS] or 0))
        bucket["days"].add(int(row[COL_PERIOD_DAY]))
        if bucket["week_end"] is None:
            bucket["week_end"] = _as_date(row[COL_WEEK_END])

    week_buckets = [
        WeekBucket(
            week_start=start,
            week_end=data["week_end"] or start,
            days=len(data["days"]),
            hours=data["hours"],
        )
        for start, data in sorted(weeks.items())
    ]

    # Cross-check the workbook's own weekly totals against the recomputed ones.
    for row_number, row in in_period:
        week_start = _as_date(row[COL_WEEK_START])
        computed = weeks[week_start]["hours"]
        reported = Decimal(str(row[COL_WEEK_REGULAR_HOURS] or 0))
        if computed != reported:
            flags.append(
                f"row {row_number}: workbook weekly total {reported} != recomputed {computed}"
            )

    missing = sorted(set(weekdays) - days_recorded)
    if missing:
        flags.append(f"{len(missing)} weekday(s) with no recorded time: {missing}")

    if len(in_period) != len(entries):
        flags.append(f"{len(entries) - len(in_period)} of {len(entries)} rows excluded as out of period")

    # A single row carrying a monthly total is not a daily entry.
    if len(in_period) == 1:
        flags.append("single aggregate row - no daily breakdown available")

    partial_days = [
        (row_number, Decimal(str(row[COL_REGULAR_HOURS])))
        for row_number, row in in_period
        if Decimal(str(row[COL_REGULAR_HOURS] or 0)) != 8
    ]
    if partial_days:
        detail = ", ".join(f"row {rn}={hrs}h" for rn, hrs in partial_days)
        flags.append(f"partial-day entries: {detail}")

    if any(str(row[COL_CORRECTING]).upper() == "Y" for _, row in in_period):
        flags.append("contains correcting-hours rows")

    return ContractorHours(
        attuid=attuid,
        first_name=str(sample[COL_FIRST_NAME] or "").strip(),
        last_name=str(sample[COL_LAST_NAME] or "").strip(),
        full_name=f"{str(sample[COL_FIRST_NAME] or '').strip()} {str(sample[COL_LAST_NAME] or '').strip()}".strip(),
        vendor_name=str(sample[COL_VENDOR_NAME] or "").strip(),
        supervisor_attuid=str(sample[COL_SUPERVISOR] or "").strip(),
        segment_id=str(sample[COL_SEGMENT_ID] or "").strip(),
        segment_name=str(sample[COL_SEGMENT_NAME] or "").strip(),
        period_start=period_start,
        period_end=period_end,
        regular_hours=regular,
        ot_hours=overtime,
        days_recorded=len(days_recorded),
        weeks=week_buckets,
        missing_weekdays=missing,
        flags=flags,
    )


def _as_date(value) -> Optional[date]:
    """Normalise a spreadsheet cell to a plain date."""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return None


def _row_date(row: tuple) -> Optional[date]:
    """Best-effort calendar date of a daily row."""
    week_start = _as_date(row[COL_WEEK_START])
    day = row[COL_PERIOD_DAY]
    if week_start is None or not isinstance(day, int):
        return None
    # Week start is the Sunday of a Sun-Sat week.
    candidate = week_start + timedelta(days=day % 7)
    if candidate.month != row[COL_MONTH]:
        candidate = date(row[COL_YEAR], row[COL_MONTH], day)
    return candidate
