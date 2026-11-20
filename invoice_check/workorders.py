"""Work orders: read the CompNova work order form once, store it, link it to an ATT ID.

Work orders change rarely, so they are parsed when they arrive (or are renewed),
checked by a person once, and then read from the database on every monthly run.
Linking to an ATT ID uses the contractors already loaded with ts-load.

The form is a flat PDF with 'Label : value' rows. A value is found by taking the
words on the same visual row as its label, to the right of it.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Optional

import pymupdf

from .common import name_tokens, parse_date, sha256, to_decimal
from . import db
from .timesheet import ContractorTime

LABELS = {
    "vendor": r"name\s+of\s+vendor",
    "consultant": r"name\s+of\s+consultant",
    "rate": r"billing\s+rate",
    "start": r"auth\.?\s*start\s+date",
    "end": r"est\.?\s*end\s+date",
    "status": r"^status",
}




@dataclass
class WorkOrder:
    id: int
    attuid: Optional[str]
    consultant_name: str
    aliases: list[str]
    vendor: Optional[str]
    rate: Optional[Decimal]
    currency: Optional[str]
    rate_basis: Optional[str]
    start: Optional[date]
    end: Optional[date]
    status: Optional[str]
    source_file: str
    link_method: Optional[str]
    confirmed: bool

    def all_names(self) -> list[str]:
        return [self.consultant_name, *self.aliases]

    def covers(self, start: date, end: date) -> bool:
        return bool(self.start and self.end and self.start <= start and end <= self.end)


def connect(db_path: Path) -> sqlite3.Connection:
    return db.connect(db_path)


# ------------------------------------------------------------------ parsing --
def _rows(doc) -> list[list[tuple]]:
    """Words grouped into visual rows: [(x0, x1, text), ...] per row."""
    out = []
    for page in doc:
        words = sorted(page.get_text("words"), key=lambda w: ((w[1] + w[3]) / 2, w[0]))
        rows: list[list] = []          # [y0, y1, words]
        for w in words:
            if rows:
                y0, y1 = rows[-1][0], rows[-1][1]
                overlap = min(y1, w[3]) - max(y0, w[1])
                if overlap > 0.5 * min(y1 - y0, w[3] - w[1]):   # same visual row
                    rows[-1][0], rows[-1][1] = min(y0, w[1]), max(y1, w[3])
                    rows[-1][2].append(w)
                    continue
            rows.append([w[1], w[3], [w]])
        out += [sorted(r[2], key=lambda w: w[0]) for r in rows]
    return out


def _value(rows, label_re: str) -> str:
    for row in rows:
        text = " ".join(w[4] for w in row)
        m = re.search(label_re, text, re.I)
        if not m:
            continue
        rest = text[m.end():]
        rest = re.sub(r"^\s*:?\s*", "", rest).strip()
        if rest:
            return re.sub(r"\s+", " ", rest)
    return ""


def _rate(raw: str) -> tuple[Optional[str], Optional[Decimal], Optional[str]]:
    """'USD 8 3 hourly' -> ('USD', 83, 'hourly'); digits rendered as separate runs are joined."""
    raw = re.sub(r"(?<=\d) (?=\d\b)", "", raw)
    cur = re.search(r"\b(USD|EUR|GBP|INR|CAD)\b|\$", raw, re.I)
    num = re.search(r"\d[\d,]*(?:\.\d+)?", raw)
    basis = re.search(r"\b(hourly|hour|hr|daily|weekly|monthly)\b", raw, re.I)
    currency = None
    if cur:
        currency = "USD" if cur.group() == "$" else cur.group().upper()
    return currency, (to_decimal(num.group()) if num else None), (basis.group().lower() if basis else None)


def parse_work_order(path: Path) -> dict:
    doc = pymupdf.open(path)
    try:
        rows = _rows(doc)
    finally:
        doc.close()
    currency, rate, basis = _rate(_value(rows, LABELS["rate"]))
    status = _value(rows, LABELS["status"]).split(" ")[0] or None
    return {
        "vendor": _value(rows, LABELS["vendor"]).rstrip(",. ") or None,
        "consultant_name": _value(rows, LABELS["consultant"]) or None,
        "currency": currency,
        "rate": rate,
        "rate_basis": basis,
        "start": parse_date(_value(rows, LABELS["start"]).split(" ")[0]),
        "end": parse_date(_value(rows, LABELS["end"]).split(" ")[0]),
        "status": status,
    }


# ------------------------------------------------------------------ linking --
def link_to_timesheet(consultant: str, people: list[ContractorTime]) -> tuple[Optional[str], Optional[str], Optional[str]]:
    """(attuid, method, timesheet_name). Exact token match first, then a unique subset match."""
    want = name_tokens(consultant)
    exact = [c for c in people if name_tokens(c.name) == want]
    if len(exact) == 1:
        return exact[0].attuid, "exact", exact[0].name
    if len(exact) > 1:
        return None, None, None                      # same name twice: a person must choose
    subset = [c for c in people
              if len(want) >= 2 and (want <= name_tokens(c.name) or name_tokens(c.name) <= want)]
    if len(subset) == 1:
        return subset[0].attuid, "subset", subset[0].name
    return None, None, None


# ------------------------------------------------------------------ storage --
def ingest(con: sqlite3.Connection, path: Path, people: Optional[list[ContractorTime]]) -> tuple[str, dict]:
    """Parse one work order and upsert it. Returns (action, parsed fields)."""
    digest = sha256(path)
    if con.execute("SELECT 1 FROM work_orders WHERE source_sha256=?", (digest,)).fetchone():
        return "unchanged", {}
    wo = parse_work_order(path)
    if not wo["consultant_name"]:
        return "unreadable", wo
    attuid = method = ts_name = None
    if people:
        attuid, method, ts_name = link_to_timesheet(wo["consultant_name"], people)
    aliases = [ts_name] if ts_name and name_tokens(ts_name) != name_tokens(wo["consultant_name"]) else []
    con.execute(
        """INSERT INTO work_orders (attuid, consultant_name, aliases, vendor, rate, currency, rate_basis,
               start_date, end_date, status, source_file, source_sha256, link_method, confirmed, updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,0,?)""",
        (attuid, wo["consultant_name"], json.dumps(aliases), wo["vendor"],
         str(wo["rate"]) if wo["rate"] is not None else None, wo["currency"], wo["rate_basis"],
         wo["start"].isoformat() if wo["start"] else None, wo["end"].isoformat() if wo["end"] else None,
         wo["status"], path.name, digest, method, datetime.now().isoformat(timespec="seconds")),
    )
    con.commit()
    return "added", {**wo, "attuid": attuid, "link_method": method}


def load_all(con: sqlite3.Connection) -> list[WorkOrder]:
    out = []
    for r in con.execute("SELECT * FROM work_orders ORDER BY consultant_name, start_date"):
        out.append(WorkOrder(
            id=r["id"], attuid=r["attuid"], consultant_name=r["consultant_name"],
            aliases=json.loads(r["aliases"] or "[]"), vendor=r["vendor"],
            rate=to_decimal(r["rate"]), currency=r["currency"], rate_basis=r["rate_basis"],
            start=parse_date(r["start_date"]), end=parse_date(r["end_date"]), status=r["status"],
            source_file=r["source_file"], link_method=r["link_method"], confirmed=bool(r["confirmed"]),
        ))
    return out


def update(con: sqlite3.Connection, wo_id: int, **fields) -> None:
    allowed = {"attuid", "consultant_name", "vendor", "rate", "start_date", "end_date", "status", "confirmed"}
    sets = {k: v for k, v in fields.items() if k in allowed and v is not None}
    if "add_alias" in fields and fields["add_alias"]:
        row = con.execute("SELECT aliases FROM work_orders WHERE id=?", (wo_id,)).fetchone()
        aliases = json.loads(row["aliases"] or "[]") if row else []
        if fields["add_alias"] not in aliases:
            aliases.append(fields["add_alias"])
        sets["aliases"] = json.dumps(aliases)
    if "attuid" in sets:
        sets["link_method"] = "manual"
    if not sets:
        return
    sets["updated_at"] = datetime.now().isoformat(timespec="seconds")
    con.execute(f"UPDATE work_orders SET {', '.join(k + '=?' for k in sets)} WHERE id=?", (*sets.values(), wo_id))
    con.commit()
