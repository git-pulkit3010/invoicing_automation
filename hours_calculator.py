"""Read an ATT time-report workbook and compute total worked hours.

The logic implemented here mirrors `count_hour_logic.md`:

* A person's worked hours for a month = sum of `Regular Hours by Day` plus
  `OT Hours by Day` over every daily row of that person for that period.
* The week a day belongs to is the workbook's `Week Start Date` /
  `Week End Date` pair (period weeks are Sunday-anchored), so weekly totals
  can be cross-checked against the workbook's own weekly-hour columns.
* The full-month benchmark is 8 hours for every Monday-Friday of the month.

The module is deliberately free of Flask so it can be unit tested on its own.
"""

from __future__ import annotations

import calendar
import datetime as dt
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

import openpyxl

__all__ = [
    "HourReport",
    "TimeReportData",
    "Contractor",
    "load_report",
    "find_workbook",
]


SHEET_NAME = "ATT Time Report"

# Columns the calculator cannot work without.
REQUIRED_COLUMNS: Tuple[str, ...] = (
    "Period Year",
    "Period Month",
    "Period Day",
    "Contractor ATTUID",
    "Contractor First Name",
    "Contractor Last Name",
    "Regular Hours by Day",
    "OT Hours by Day",
)

# Columns used for reporting / auditing when present.
OPTIONAL_COLUMNS: Tuple[str, ...] = (
    "Period Week",
    "Week Start Date",
    "Week End Date",
    "Vendor Name",
    "Supervisor ATTUID",
    "Segment Name",
    "Correcting Hours Indicator",
    "Period Week Regular Hours",
    "Period Week OT Hours",
)

STANDARD_DAY_HOURS = 8.0


def _as_int(value, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_float(value, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_date(value) -> Optional[dt.date]:
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    return None


def _clean_text(value) -> str:
    return "" if value is None else str(value).strip()


def _fmt_hours(value: float) -> str:
    """Render 8.0 as `8` and 7.5 as `7.5`."""
    if value is None:
        return "0"
    if abs(value - round(value)) < 1e-9:
        return str(int(round(value)))
    return f"{value:.2f}".rstrip("0").rstrip(".")


@dataclass(frozen=True)
class Contractor:
    attuid: str
    first_name: str
    last_name: str

    @property
    def full_name(self) -> str:
        return f"{self.first_name} {self.last_name}".strip()


@dataclass
class DayEntry:
    year: int
    month: int
    period_week: str
    day: int
    week_start: Optional[dt.date]
    week_end: Optional[dt.date]
    contractor_attuid: str
    first_name: str
    last_name: str
    supervisor_attuid: str
    vendor_name: str
    segment_name: str
    correcting_hours: str
    regular_hours: float
    ot_hours: float
    week_regular_hours: Optional[float]
    week_ot_hours: Optional[float]

    @property
    def total_hours(self) -> float:
        return self.regular_hours + self.ot_hours

    @property
    def work_date(self) -> Optional[dt.date]:
        try:
            return dt.date(self.year, self.month, self.day)
        except ValueError:
            return None


@dataclass
class WeekSummary:
    week_start: Optional[dt.date]
    week_end: Optional[dt.date]
    days_recorded: int
    regular_hours: float
    ot_hours: float
    workbook_weekly_hours: Optional[float]

    @property
    def total_hours(self) -> float:
        return self.regular_hours + self.ot_hours

    @property
    def label(self) -> str:
        if self.week_start and self.week_end:
            return f"{self.week_start.strftime('%b %d')} - {self.week_end.strftime('%b %d, %Y')}"
        return "Unknown week"


@dataclass
class AuditNote:
    level: str  # "ok" | "warn" | "info"
    check: str
    finding: str


@dataclass
class HourReport:
    """The full computed result for one contractor and one month."""

    contractor: Contractor
    year: int
    month: int
    total_hours: float
    regular_hours: float
    ot_hours: float
    days_recorded: int
    benchmark_hours: float
    weekdays_in_month: int
    missing_weekdays: List[dt.date]
    weeks: List[WeekSummary]
    daily: List[DayEntry]
    aggregate_rows: bool = False
    audits: List[AuditNote] = field(default_factory=list)

    @property
    def month_name(self) -> str:
        return calendar.month_name[self.month]

    @property
    def period_label(self) -> str:
        return f"{self.month_name} {self.year}"

    @property
    def difference_from_benchmark(self) -> float:
        return self.total_hours - self.benchmark_hours

    def as_dict(self) -> Dict:
        return {
            "contractor": {
                "attuid": self.contractor.attuid,
                "first_name": self.contractor.first_name,
                "last_name": self.contractor.last_name,
                "full_name": self.contractor.full_name,
            },
            "period": {
                "year": self.year,
                "month": self.month,
                "month_name": self.month_name,
                "label": self.period_label,
            },
            "summary": {
                "total_hours": _fmt_hours(self.total_hours),
                "total_hours_value": self.total_hours,
                "regular_hours": _fmt_hours(self.regular_hours),
                "ot_hours": _fmt_hours(self.ot_hours),
                "days_recorded": self.days_recorded,
                "benchmark_hours": _fmt_hours(self.benchmark_hours),
                "difference": _fmt_hours(self.difference_from_benchmark),
                "weekdays_in_month": self.weekdays_in_month,
                "has_data": bool(self.daily),
                "aggregate_rows": self.aggregate_rows,
            },
            "missing_weekdays": [
                {"date": d.isoformat(), "label": f"{d.strftime('%a, %b %d, %Y')}"}
                for d in self.missing_weekdays
            ],
            "weeks": [
                {
                    "label": w.label,
                    "week_start": w.week_start.isoformat() if w.week_start else None,
                    "week_end": w.week_end.isoformat() if w.week_end else None,
                    "days_recorded": w.days_recorded,
                    "hours": _fmt_hours(w.total_hours),
                    "hours_value": w.total_hours,
                    "workbook_hours": (
                        _fmt_hours(w.workbook_weekly_hours)
                        if w.workbook_weekly_hours is not None
                        else None
                    ),
                }
                for w in self.weeks
            ],
            "audits": [
                {"level": a.level, "check": a.check, "finding": a.finding} for a in self.audits
            ],
        }


class TimeReportData:
    """Parsed workbook plus the computation API used by the Flask layer."""

    def __init__(self, entries: Sequence[DayEntry], source: Optional[Path] = None):
        self.entries: List[DayEntry] = list(entries)
        self.source = source
        self._contractors: Dict[str, Contractor] = {}
        for entry in self.entries:
            if entry.contractor_attuid not in self._contractors:
                self._contractors[entry.contractor_attuid] = Contractor(
                    attuid=entry.contractor_attuid,
                    first_name=entry.first_name,
                    last_name=entry.last_name,
                )

    # ------------------------------------------------------------------
    # Meta data
    # ------------------------------------------------------------------
    @property
    def contractors(self) -> List[Contractor]:
        return sorted(
            self._contractors.values(), key=lambda c: (c.last_name, c.first_name, c.attuid)
        )

    def contractor(self, attuid: str) -> Optional[Contractor]:
        return self._contractors.get(attuid)

    def contractor_by_name(self, full_name: str) -> Optional[Contractor]:
        wanted = full_name.strip().upper()
        for c in self._contractors.values():
            if c.full_name.upper() == wanted:
                return c
        return None

    @property
    def years(self) -> List[int]:
        return sorted({e.year for e in self.entries}, reverse=True)

    @property
    def months_with_data(self) -> List[Dict]:
        counts: Dict[Tuple[int, int], int] = {}
        people: Dict[Tuple[int, int], set] = {}
        for e in self.entries:
            key = (e.year, e.month)
            counts[key] = counts.get(key, 0) + 1
            people.setdefault(key, set()).add(e.contractor_attuid)
        return [
            {
                "year": year,
                "month": month,
                "month_name": calendar.month_name[month],
                "people": len(people[(year, month)]),
                "rows": counts[(year, month)],
            }
            for (year, month) in sorted(counts, reverse=True)
        ]

    # ------------------------------------------------------------------
    # Computation
    # ------------------------------------------------------------------
    def entries_for(self, attuid: str, year: int, month: int) -> List[DayEntry]:
        return [
            e
            for e in self.entries
            if e.contractor_attuid == attuid and e.year == year and e.month == month
        ]

    def compute(self, attuid: str, year: int, month: int) -> HourReport:
        contractor = self._contractors.get(attuid)
        if contractor is None:
            raise KeyError(f"Unknown contractor ATTUID: {attuid}")
        rows = sorted(
            self.entries_for(attuid, year, month),
            key=lambda e: (e.day, e.week_start or dt.date.min),
        )
        return _build_report(contractor, year, month, rows)


def _weekdays_in_month(year: int, month: int) -> List[dt.date]:
    days = calendar.monthrange(year, month)[1]
    return [
        dt.date(year, month, d)
        for d in range(1, days + 1)
        if dt.date(year, month, d).weekday() < 5
    ]


def _build_report(
    contractor: Contractor, year: int, month: int, rows: List[DayEntry]
) -> HourReport:
    total = sum(r.total_hours for r in rows)
    regular = sum(r.regular_hours for r in rows)
    ot = sum(r.ot_hours for r in rows)

    recorded_days = {r.day for r in rows}
    days_recorded = len(recorded_days)

    weekdays = _weekdays_in_month(year, month)
    recorded_dates = {r.work_date for r in rows if r.work_date}
    # A period stored as one monthly total has no daily coverage to check.
    aggregate_rows = any(r.regular_hours > 24 for r in rows)
    missing = (
        []
        if (not rows or aggregate_rows)
        else [d for d in weekdays if d not in recorded_dates]
    )
    benchmark = STANDARD_DAY_HOURS * len(weekdays)

    # Weekly rollup keyed by the workbook's own period-week start date.
    weeks: Dict[Optional[dt.date], Dict] = {}
    for r in rows:
        key = r.week_start
        bucket = weeks.setdefault(
            key,
            {
                "week_start": key,
                "week_end": r.week_end,
                "days": set(),
                "regular": 0.0,
                "ot": 0.0,
                "workbook_values": set(),
            },
        )
        bucket["days"].add(r.day)
        bucket["regular"] += r.regular_hours
        bucket["ot"] += r.ot_hours
        if r.week_regular_hours is not None or r.week_ot_hours is not None:
            wb_value = (r.week_regular_hours or 0.0) + (r.week_ot_hours or 0.0)
            bucket["workbook_values"].add(round(wb_value, 4))

    week_summaries: List[WeekSummary] = []
    for _key, bucket in weeks.items():
        values = bucket["workbook_values"]
        wb_value = sorted(values)[0] if values else None
        week_summaries.append(
            WeekSummary(
                week_start=bucket["week_start"],
                week_end=bucket["week_end"] or bucket["week_start"],
                days_recorded=len(bucket["days"]),
                regular_hours=bucket["regular"],
                ot_hours=bucket["ot"],
                workbook_weekly_hours=wb_value,
            )
        )
    week_summaries.sort(key=lambda w: (w.week_start or dt.date.min))

    audits = _audit(rows, week_summaries, missing, weekdays, aggregate_rows)

    return HourReport(
        contractor=contractor,
        year=year,
        month=month,
        total_hours=total,
        regular_hours=regular,
        ot_hours=ot,
        days_recorded=days_recorded,
        benchmark_hours=benchmark,
        weekdays_in_month=len(weekdays),
        missing_weekdays=missing,
        weeks=week_summaries,
        daily=rows,
        aggregate_rows=aggregate_rows,
        audits=audits,
    )


def _audit(
    rows: List[DayEntry],
    weeks: List[WeekSummary],
    missing: List[dt.date],
    weekdays: List[dt.date],
    aggregate_rows: bool = False,
) -> List[AuditNote]:
    notes: List[AuditNote] = []

    if not rows:
        notes.append(
            AuditNote("warn", "Records", "No time entries found for this contractor and period.")
        )
        return notes

    # Duplicate daily entries: same contractor, same day, more than once.
    day_counts: Dict[int, int] = {}
    for r in rows:
        day_counts[r.day] = day_counts.get(r.day, 0) + 1
    dupes = [d for d, c in day_counts.items() if c > 1]
    notes.append(
        AuditNote(
            "ok" if not dupes else "warn",
            "Duplicate daily entries",
            "None" if not dupes else f"Day(s) recorded more than once: {sorted(dupes)}",
        )
    )

    # Rows carrying a monthly aggregate instead of a single day's hours.
    bulk = [r for r in rows if r.regular_hours > 24] if aggregate_rows else []
    notes.append(
        AuditNote(
            "ok" if not bulk else "warn",
            "Aggregate rows",
            "None"
            if not bulk
            else (
                f"{len(bulk)} row(s) hold a monthly total rather than daily hours "
                f"(day(s): " + ", ".join(str(r.day) for r in bulk) + ")"
            ),
        )
    )

    # Weekend hours.
    weekend = [r for r in rows if r.work_date and r.work_date.weekday() >= 5]
    notes.append(
        AuditNote(
            "ok" if not weekend else "warn",
            "Weekend hours",
            "None" if not weekend else f"{len(weekend)} entry/entries on a weekend",
        )
    )

    # Daily sum vs the workbook's own weekly total for each period week.
    mismatches = [
        w
        for w in weeks
        if w.workbook_weekly_hours is not None
        and abs(w.workbook_weekly_hours - w.total_hours) > 1e-6
    ]
    notes.append(
        AuditNote(
            "ok" if not mismatches else "warn",
            "Daily vs weekly totals",
            "Match exactly"
            if not mismatches
            else f"{len(mismatches)} period week(s) disagree with the workbook weekly totals",
        )
    )

    ot_total = sum(r.ot_hours for r in rows)
    notes.append(
        AuditNote(
            "ok" if not ot_total else "info",
            "Overtime",
            "Zero" if not ot_total else f"{_fmt_hours(ot_total)} overtime hours recorded",
        )
    )

    correcting = [
        r for r in rows if (r.correcting_hours or "N").strip().upper() != "N"
    ]
    notes.append(
        AuditNote(
            "ok" if not correcting else "warn",
            "Correcting-hours indicators",
            "All N" if not correcting else f"{len(correcting)} row(s) flagged as correcting hours",
        )
    )

    if aggregate_rows:
        notes.append(
            AuditNote(
                "warn",
                "Missing weekdays",
                "Not determinable - this period is stored as a monthly total, not day by day",
            )
        )
    elif missing:
        notes.append(
            AuditNote(
                "warn",
                "Missing weekdays",
                f"{len(missing)} of {len(weekdays)} weekday(s) have no entry "
                f"({_fmt_hours(len(missing) * STANDARD_DAY_HOURS)} hours below the full-month benchmark)",
            )
        )
    else:
        notes.append(
            AuditNote("ok", "Missing weekdays", f"None - all {len(weekdays)} weekday(s) covered")
        )

    return notes


def find_workbook(base_dir: Path, explicit: Optional[Path] = None) -> Optional[Path]:
    """Locate the time-report workbook."""
    if explicit is not None:
        p = Path(explicit)
        if p.is_file():
            return p
        raise FileNotFoundError(f"Excel file not found: {p}")

    candidates: List[Path] = []
    for pattern in ("*.xlsx", "*.xlsm", "*.xls"):
        candidates.extend(sorted(base_dir.rglob(pattern)))
    preferred = [
        c for c in candidates if "report" in c.name.lower() or "time" in c.name.lower()
    ]
    ordered = preferred + [c for c in candidates if c not in preferred]
    for candidate in ordered:
        try:
            wb = openpyxl.load_workbook(candidate, read_only=True, data_only=True)
            names = {n.strip().lower() for n in wb.sheetnames}
            wb.close()
        except Exception:
            continue
        if SHEET_NAME.lower() in names:
            return candidate
    return ordered[0] if ordered else None


def load_report(path: Path) -> TimeReportData:
    """Parse the ATT time report workbook into a :class:`TimeReportData`."""
    path = Path(path)
    workbook = openpyxl.load_workbook(path, data_only=True)

    sheet = None
    for name in workbook.sheetnames:
        if name.strip().lower() == SHEET_NAME.lower():
            sheet = workbook[name]
            break
    if sheet is None:
        sheet = workbook[workbook.sheetnames[0]]

    header_row, header_index = _find_header_row(sheet)
    if header_index is None:
        raise ValueError(
            f"Could not find the header row in {path.name} (sheet '{sheet.title}'). "
            f"Expected columns: {', '.join(REQUIRED_COLUMNS)}"
        )

    missing = [c for c in REQUIRED_COLUMNS if c not in header_index]
    if missing:
        raise ValueError(f"{path.name} is missing required column(s): {', '.join(missing)}")

    entries: List[DayEntry] = []
    for row in sheet.iter_rows(min_row=header_row + 1, values_only=True):
        entry = _row_to_entry(row, header_index)
        if entry is not None:
            entries.append(entry)

    return TimeReportData(entries, source=path)


def _find_header_row(sheet) -> Tuple[int, Dict[str, int]]:
    wanted = {c.lower() for c in REQUIRED_COLUMNS}
    for r in range(1, min(sheet.max_row, 25) + 1):
        values = [c.value for c in sheet[r]]
        index: Dict[str, int] = {}
        for i, value in enumerate(values, start=1):
            if value is None:
                continue
            text = str(value).strip()
            if text and text not in index:
                index[text] = i
        if wanted.issubset({k.lower() for k in index}):
            return r, index
    return -1, {}


def _row_to_entry(row: Sequence, header_index: Dict[str, int]) -> Optional[DayEntry]:
    def get(name: str, default=None):
        idx = header_index.get(name)
        if idx is None or idx > len(row):
            return default
        return row[idx - 1]

    attuid = _clean_text(get("Contractor ATTUID"))
    if not attuid:
        return None

    year = _as_int(get("Period Year"), 0)
    month = _as_int(get("Period Month"), 0)
    if not year or not month:
        return None

    week_reg = get("Period Week Regular Hours")
    week_ot = get("Period Week OT Hours")

    return DayEntry(
        year=year,
        month=month,
        period_week=_clean_text(get("Period Week", "")),
        day=_as_int(get("Period Day"), 0),
        week_start=_as_date(get("Week Start Date")),
        week_end=_as_date(get("Week End Date")),
        contractor_attuid=attuid,
        first_name=_clean_text(get("Contractor First Name")).upper(),
        last_name=_clean_text(get("Contractor Last Name")).upper(),
        supervisor_attuid=_clean_text(get("Supervisor ATTUID")),
        vendor_name=_clean_text(get("Vendor Name")),
        segment_name=_clean_text(get("Segment Name")),
        correcting_hours=_clean_text(get("Correcting Hours Indicator", "N")) or "N",
        regular_hours=_as_float(get("Regular Hours by Day"), 0.0),
        ot_hours=_as_float(get("OT Hours by Day"), 0.0),
        week_regular_hours=None if week_reg is None else _as_float(week_reg, 0.0),
        week_ot_hours=None if week_ot is None else _as_float(week_ot, 0.0),
    )
