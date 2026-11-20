"""ATT Time Report -> database -> total hours per contractor ATT ID for a month.

Columns are found by their header text, not by position, so an inserted
column does not silently shift every value. The report period comes from the
'HOURS: m/d/yyyy - m/d/yyyy' banner; if it is missing the load fails rather
than guessing a month.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from openpyxl import load_workbook

from .common import parse_date, sha256

SHEET = "ATT Time Report"
HEADERS = {
    "year": "period year",
    "month": "period month",
    "day": "period day",
    "attuid": "contractor attuid",
    "first": "contractor first name",
    "last": "contractor last name",
    "regular": "regular hours by day",
    "ot": "ot hours by day",
}
OPTIONAL_HEADERS = {"correcting": "correcting hours indicator"}
_BANNER = re.compile(r"(\d{1,2}/\d{1,2}/\d{4})\s*-\s*(\d{1,2}/\d{1,2}/\d{4})")


class TimesheetError(ValueError):
    pass


@dataclass
class ContractorTime:
    attuid: str
    name: str
    regular: Decimal = Decimal(0)
    ot: Decimal = Decimal(0)
    days: set = field(default_factory=set)          # dates with recorded time
    excluded_rows: int = 0                          # rows outside the report period

    @property
    def total(self) -> Decimal:
        return self.regular + self.ot


@dataclass
class Timesheet:
    path: Path
    period_start: date
    period_end: date
    contractors: dict[str, ContractorTime]

    def weekdays(self) -> list[date]:
        d, out = self.period_start, []
        while d <= self.period_end:
            if d.weekday() < 5:
                out.append(d)
            d += timedelta(days=1)
        return out

    def missing_weekdays(self, attuid: str) -> list[date]:
        c = self.contractors.get(attuid)
        have = c.days if c else set()
        return [d for d in self.weekdays() if d not in have]


@dataclass
class DayRow:
    attuid: str
    name: str
    day: date | None
    regular: Decimal
    ot: Decimal
    correcting: bool
    source_row: int


def read_report(path: Path) -> tuple[date, date, list[DayRow]]:
    """Every daily row of the workbook, plus the report period from its banner."""
    wb = load_workbook(path, read_only=True, data_only=True)
    try:
        if SHEET not in wb.sheetnames:
            raise TimesheetError(f"sheet '{SHEET}' not found in {path.name}")
        rows = list(wb[SHEET].iter_rows(values_only=True))
    finally:
        wb.close()

    period = None
    header_idx = None
    for i, row in enumerate(rows[:15]):
        cells = [str(c).strip() if c is not None else "" for c in row]
        for c in cells:
            m = _BANNER.search(c)
            if m and period is None:
                period = (parse_date(m.group(1)), parse_date(m.group(2)))
        if any(c.lower() == HEADERS["attuid"] for c in cells):
            header_idx = i
            break
    if period is None or None in period:
        raise TimesheetError(f"{path.name}: no 'HOURS: start - end' period banner found; refusing to guess the month")
    if header_idx is None:
        raise TimesheetError(f"{path.name}: header row with 'Contractor ATTUID' not found")
    if (period[0].year, period[0].month) != (period[1].year, period[1].month):
        raise TimesheetError(f"{path.name}: report period {period[0]} to {period[1]} is not a single month")

    header = [str(c).strip().lower() if c is not None else "" for c in rows[header_idx]]
    col = {}
    for key, title in HEADERS.items():
        if title not in header:
            raise TimesheetError(f"{path.name}: column '{title}' not found")
        col[key] = header.index(title)
    for key, title in OPTIONAL_HEADERS.items():
        col[key] = header.index(title) if title in header else None

    out: list[DayRow] = []
    for n, row in enumerate(rows[header_idx + 1:], start=header_idx + 2):
        if row is None or len(row) <= max(v for v in col.values() if v is not None):
            continue
        attuid = row[col["attuid"]]
        reg = row[col["regular"]]
        if not isinstance(attuid, str) or not attuid.strip() or not isinstance(reg, (int, float)):
            continue
        try:
            day = date(int(row[col["year"]]), int(row[col["month"]]), int(row[col["day"]]))
        except (TypeError, ValueError):
            day = None
        corr = col["correcting"] is not None and str(row[col["correcting"]] or "").strip().upper() == "Y"
        out.append(DayRow(attuid.strip().lower(), f"{row[col['first']] or ''} {row[col['last']] or ''}".strip(),
                          day, Decimal(str(reg or 0)), Decimal(str(row[col["ot"]] or 0)), corr, n))
    return period[0], period[1], out


def _build(path: Path, start: date, end: date, rows) -> Timesheet:
    people: dict[str, ContractorTime] = {}
    for r in rows:
        c = people.setdefault(r.attuid, ContractorTime(r.attuid, r.name))
        if r.day is None or not (start <= r.day <= end):
            c.excluded_rows += 1
            continue
        c.regular += r.regular
        c.ot += r.ot
        c.days.add(r.day)
    return Timesheet(path, start, end, people)


def load_timesheet(path: Path) -> Timesheet:
    """Read the workbook directly (used by tests; the pipeline reads from the database)."""
    start, end, rows = read_report(path)
    return _build(path, start, end, rows)


# ------------------------------------------------------------------ database --
def store(con: sqlite3.Connection, path: Path) -> dict:
    """Load one month's report into the database, replacing that month if it was loaded before."""
    start, end, rows = read_report(path)
    month = f"{start.year:04d}-{start.month:02d}"
    kept = [r for r in rows if r.day is not None and start <= r.day <= end]   # counted rows
    with con:                                          # one transaction: all or nothing
        con.execute("DELETE FROM timesheet_days WHERE month=?", (month,))
        con.execute("DELETE FROM timesheet_loads WHERE month=?", (month,))
        con.execute("INSERT INTO timesheet_loads VALUES (?,?,?,?,?,?,?,?)",
                    (month, start.isoformat(), end.isoformat(), path.name, sha256(path), len(kept),
                     len(rows) - len(kept), datetime.now().isoformat(timespec="seconds")))
        con.executemany("INSERT INTO timesheet_days VALUES (?,?,?,?,?,?,?,?,?)",
                        [(month, r.attuid, r.name, r.day.isoformat() if r.day else None,
                          int(r.day is not None and start <= r.day <= end), str(r.regular), str(r.ot),
                          int(r.correcting), r.source_row) for r in rows])
    return {"month": month, "period_start": start, "period_end": end, "rows_loaded": len(kept),
            "rows_excluded": len(rows) - len(kept), "contractors": len({r.attuid for r in rows})}


def from_db(con: sqlite3.Connection, month: str) -> Timesheet:
    load = con.execute("SELECT * FROM timesheet_loads WHERE month=?", (month,)).fetchone()
    if load is None:
        loaded = [r["month"] for r in con.execute("SELECT month FROM timesheet_loads ORDER BY month")]
        raise TimesheetError(f"no timesheet loaded for {month} (loaded: {', '.join(loaded) or 'none'}); run ts-load")
    rows = [DayRow(r["attuid"], r["name"], parse_date(r["day"]) if r["in_period"] else None,
                   Decimal(r["regular_hours"]), Decimal(r["ot_hours"]), bool(r["correcting"]), r["source_row"])
            for r in con.execute("SELECT * FROM timesheet_days WHERE month=? ORDER BY attuid, day", (month,))]
    ts = _build(Path(load["source_file"]), parse_date(load["period_start"]), parse_date(load["period_end"]), rows)
    return ts


def all_contractors(con: sqlite3.Connection) -> list[ContractorTime]:
    """Every ATT ID seen in any loaded month, with its latest name (for linking work orders)."""
    return [ContractorTime(r["attuid"], r["name"]) for r in con.execute(
        "SELECT attuid, name FROM timesheet_days t WHERE month = "
        "(SELECT MAX(month) FROM timesheet_days WHERE attuid = t.attuid) GROUP BY attuid")]
