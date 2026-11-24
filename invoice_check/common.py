"""Small shared helpers: numbers, dates, names, hashing."""

from __future__ import annotations

import hashlib
import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Iterable, Optional

CENT = Decimal("0.01")


# ---------------------------------------------------------------- numbers --
_NUM_IN_TEXT = re.compile(r"(?<![\w/])[$£€₹]?\s?\d[\d,]*(?:\.\d+)?(?![\w/])")


def to_decimal(value) -> Optional[Decimal]:
    """Decimal from an int/float/str like '$14,250.00'. None when not a number."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, Decimal)):
        return Decimal(value)
    if isinstance(value, float):
        return Decimal(str(value))
    text = re.sub(r"[\s$£€₹,]", "", str(value))
    if not re.fullmatch(r"-?\d+(\.\d+)?", text):
        return None
    try:
        return Decimal(text)
    except InvalidOperation:
        return None


def numbers_in_text(text: str) -> set[Decimal]:
    """Every plain number that appears in a document (dates and IDs excluded)."""
    found = set()
    for m in _NUM_IN_TEXT.finditer(text):
        d = to_decimal(m.group())
        if d is not None:
            found.add(d.normalize())
    return found


def same_money(a: Decimal, b: Decimal, tol: Decimal = CENT) -> bool:
    return abs(a - b) <= tol


def fmt_money(d: Optional[Decimal]) -> str:
    return "-" if d is None else f"{Decimal(d).quantize(CENT):,}"


def fmt_num(d: Optional[Decimal]) -> str:
    if d is None:
        return "-"
    d = d.normalize()
    return f"{d:,f}" if d == d.to_integral() else f"{d:,}"


# ------------------------------------------------------------------ dates --
def parse_date(value) -> Optional[date]:
    """Dates as the PDFs and the agent write them."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%m-%d-%Y", "%b %d, %Y", "%B %d, %Y", "%d %b %Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


# ------------------------------------------------------------------ names --
def name_tokens(name: str) -> frozenset[str]:
    return frozenset(re.sub(r"[^A-Z0-9 ]+", " ", (name or "").upper()).split())


def vendor_key(name: str) -> str:
    """'Northwind Systems, LLC' -> 'NORTHWIND SYSTEMS' (legal suffixes dropped)."""
    drop = {"LLC", "INC", "LTD", "CORP", "CORPORATION", "CO", "PVT", "LIMITED", "THE", "LLP", "PLLC"}
    return " ".join(t for t in re.sub(r"[^A-Z0-9 ]+", " ", (name or "").upper()).split() if t not in drop)


def vendors_agree(a: str, b: str) -> bool:
    """Loose vendor comparison: one key's words contain the other's first word."""
    ka, kb = vendor_key(a).split(), vendor_key(b).split()
    if not ka or not kb:
        return False
    return ka[0] in kb or kb[0] in ka


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def first(items: Iterable, default=None):
    for x in items:
        return x
    return default


def tidy_name(name: str) -> str:
    """'JORDAN lee SMITH' -> 'Jordan Lee Smith'; mixed-case words ('McDonald') are left alone."""
    return " ".join(w.capitalize() if (w.isupper() or w.islower()) and w.isalpha() else w
                    for w in (name or "").split())
