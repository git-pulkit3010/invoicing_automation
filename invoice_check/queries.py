"""Deterministic lookups against the database.

These are what the agent calls instead of opening the timesheet or a work order.
Same database + same arguments = same answer, every time. Each returns a plain
dict (printed as text or JSON by the CLI).
"""

from __future__ import annotations

import sqlite3
from datetime import date
from decimal import Decimal
from typing import Optional

from .common import name_tokens, vendors_agree
from .timesheet import from_db
from .workorders import WorkOrder, load_all


def _wo_dict(w: WorkOrder) -> dict:
    return {"work_order_id": w.id, "attuid": w.attuid, "consultant_name": w.consultant_name,
            "aliases": w.aliases, "vendor": w.vendor, "rate": w.rate, "currency": w.currency,
            "rate_basis": w.rate_basis, "start": w.start, "end": w.end, "status": w.status,
            "confirmed": w.confirmed, "source_file": w.source_file}


def months(con: sqlite3.Connection) -> list[dict]:
    return [dict(r) for r in con.execute(
        "SELECT month, period_start, period_end, source_file, rows_loaded, rows_excluded, loaded_at "
        "FROM timesheet_loads ORDER BY month")]


def who(con: sqlite3.Connection, name: str, vendor: Optional[str] = None) -> dict:
    """Name (as on an invoice) -> ATT ID and work orders. result: match | ambiguous | none."""
    want = name_tokens(name)
    orders = load_all(con)
    hits = [w for w in orders if any(name_tokens(n) == want for n in w.all_names())]
    how = "exact"
    if not hits:
        hits = [w for w in orders if len(want) >= 2 and any(
            want <= name_tokens(n) or name_tokens(n) <= want for n in w.all_names() if len(name_tokens(n)) >= 2)]
        how = "partial name"
    if vendor and len({w.attuid or f"wo{w.id}" for w in hits}) > 1:
        narrowed = [w for w in hits if w.vendor and vendors_agree(w.vendor, vendor)]
        if narrowed:
            hits, how = narrowed, how + " + vendor"
    people = sorted({w.attuid or f"(unlinked work order {w.id})" for w in hits})
    if not hits:
        # not in the work orders: is the person at least in a timesheet?
        ts_hits = [dict(r) for r in con.execute("SELECT DISTINCT attuid, name FROM timesheet_days")
                   if name_tokens(r["name"]) == want]
        return {"query": name, "result": "none", "detail": "no work order for this name",
                "timesheet_matches": ts_hits}
    result = "match" if len(people) == 1 else "ambiguous"
    return {"query": name, "result": result, "matched_by": how, "attuid": people[0] if result == "match" else None,
            "candidates": people, "work_orders": [_wo_dict(w) for w in hits]}


def hours(con: sqlite3.Connection, attuid: str, month: str) -> dict:
    """Timesheet hours for one ATT ID in one month."""
    ts = from_db(con, month)
    attuid = attuid.lower()
    c = ts.contractors.get(attuid)
    if c is None:
        return {"attuid": attuid, "month": month, "found": False, "total_hours": Decimal(0),
                "detail": "no rows for this ATT ID in that month"}
    missing = ts.missing_weekdays(attuid)
    return {"attuid": attuid, "month": month, "found": True, "name": c.name,
            "regular_hours": c.regular, "ot_hours": c.ot, "total_hours": c.total,
            "days_recorded": len(c.days), "weekdays_in_month": len(ts.weekdays()),
            "missing_weekdays": [d.isoformat() for d in missing]}


def expected(con: sqlite3.Connection, attuid: str, month: str) -> dict:
    """What the invoice for this person and month should be: timesheet hours x work order rate."""
    h = hours(con, attuid, month)
    ts = from_db(con, month)
    orders = [w for w in load_all(con) if w.attuid == attuid.lower()]
    covering = [w for w in orders if w.covers(ts.period_start, ts.period_end)]
    out = {"attuid": attuid.lower(), "month": month, "total_hours": h["total_hours"],
           "ot_hours": h.get("ot_hours", Decimal(0))}
    if not orders:
        return {**out, "result": "no work order", "expected_amount": None}
    w = covering[-1] if covering else max(orders, key=lambda x: x.end or date.min)
    out.update({"work_order": _wo_dict(w), "rate": w.rate,
                "work_order_covers_month": bool(covering)})
    if w.rate is None:
        return {**out, "result": "work order has no rate", "expected_amount": None}
    amount = h["total_hours"] * w.rate
    result = "ok" if covering else f"work order does not cover {ts.period_start} to {ts.period_end}"
    if h.get("ot_hours"):
        result += "; includes overtime hours at the regular rate"
    return {**out, "result": result, "expected_amount": amount}
