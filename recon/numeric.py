"""Numeric, currency and date parsing helpers.

The PDFs in this workflow render numbers in several awkward ways:

  * ``$15,200.00``      - symbol glued to the number
  * ``$ 17,248.00``     - symbol in a separate text run, far from the number
  * ``8 3``             - digits rendered as separate runs (Srilasya's $83 rate)
  * ``1.5%``            - a percentage that must never be read as an amount
  * ``10/01/2026``      - a date that must never be read as a quantity

Every helper here is deliberately conservative: when in doubt it returns None
rather than guessing, so the caller can raise a parse warning instead of
silently producing a wrong number.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Iterable, Optional

# A plain monetary/quantity token: optional symbol, optional thousands
# separators, optional fractional part. Anything else (dates, percentages,
# identifiers such as "57977W") is rejected.
_NUMBER_RE = re.compile(r"^\$?\s?(\d{1,3}(?:,\d{3})*|\d+)(?:\.(\d{1,4}))?$")

_CURRENCY_SYMBOLS = {"$": "USD", "€": "EUR", "£": "GBP", "¥": "JPY", "₹": "INR"}
_CURRENCY_CODES = {"USD", "EUR", "GBP", "JPY", "INR", "CAD", "AUD"}

_MONTHS = {
    "january": 1, "jan": 1, "february": 2, "feb": 2, "march": 3, "mar": 3,
    "april": 4, "apr": 4, "may": 5, "june": 6, "jun": 6, "july": 7, "jul": 7,
    "august": 8, "aug": 8, "september": 9, "sep": 9, "sept": 9,
    "october": 10, "oct": 10, "november": 11, "nov": 11, "december": 12, "dec": 12,
}

_DATE_FORMATS = ("%m/%d/%Y", "%m/%d/%y", "%m-%d-%Y", "%Y-%m-%d", "%b %d, %Y", "%B %d, %Y")

_RANGE_SEPARATORS = (" - ", " – ", " — ", " to ", " thru ", " through ")


def parse_number(text: Optional[str]) -> Optional[Decimal]:
    """Parse a numeric token. Returns None when the token is not a plain number."""
    if text is None:
        return None
    stripped = text.strip()
    if not stripped:
        return None
    match = _NUMBER_RE.match(stripped)
    if not match:
        return None
    whole = match.group(1).replace(",", "")
    frac = match.group(2)
    try:
        return Decimal(whole + ("." + frac if frac else ""))
    except InvalidOperation:
        return None


def is_currency_symbol(text: str) -> bool:
    return text.strip() in _CURRENCY_SYMBOLS


def currency_of(text: str) -> Optional[str]:
    """Return the ISO currency code for a symbol or code token."""
    token = text.strip()
    if token in _CURRENCY_SYMBOLS:
        return _CURRENCY_SYMBOLS[token]
    upper = token.upper()
    if upper in _CURRENCY_CODES:
        return upper
    return None


def merge_digit_runs(texts: Iterable[str]) -> str:
    """Join digit fragments that were rendered as separate runs ('8' + '3' -> '83')."""
    out: list[str] = []
    for token in texts:
        if out and len(out[-1]) == 1 and out[-1].isdigit() and len(token) == 1 and token.isdigit():
            out[-1] = out[-1] + token
        else:
            out.append(token)
    return " ".join(out)


def parse_date(text: Optional[str]) -> Optional[date]:
    """Parse a date written in one of the common formats used across the PDFs."""
    if not text:
        return None
    token = text.strip()
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(token, fmt).date()
        except ValueError:
            continue
    # Two-digit years in work orders are always 20xx for this dataset.
    m = re.match(r"^(\d{1,2})/(\d{1,2})/(\d{2})$", token)
    if m:
        month, day, year = (int(g) for g in m.groups())
        try:
            return date(2000 + year, month, day)
        except ValueError:
            return None
    return None


def month_bounds(year: int, month: int) -> tuple[date, date]:
    """First and last calendar day of a given month."""
    start = date(year, month, 1)
    if month == 12:
        end = date(year, 12, 31)
    else:
        end = date(year, month + 1, 1) - timedelta(days=1)
    return start, end


def find_date_range(text: str) -> Optional[tuple[date, date]]:
    """Find an explicit 'start - end' date range inside free text."""
    for sep in _RANGE_SEPARATORS:
        if sep not in text:
            continue
        left, _, right = text.partition(sep)
        start = _last_date_in(left)
        end = _first_date_in(right)
        if start and end and start <= end:
            return start, end
    return None


def _last_date_in(text: str) -> Optional[date]:
    best = None
    for token in re.findall(r"\d{1,2}/\d{1,2}/\d{2,4}", text):
        parsed = parse_date(token)
        if parsed and (best is None or parsed > best):
            best = parsed
    return best


def _first_date_in(text: str) -> Optional[date]:
    for token in re.findall(r"\d{1,2}/\d{1,2}/\d{2,4}", text):
        parsed = parse_date(token)
        if parsed:
            return parsed
    return None


def find_month(text: str) -> Optional[tuple[date, date]]:
    """Find a 'Month YYYY' or 'Mon-YYYY' reference and expand it to a month range."""
    for match in re.finditer(r"\b([A-Za-z]{3,9})\.?\s*[-\s]?\s*(\d{4})\b", text):
        name = match.group(1).lower()
        if name in _MONTHS:
            year = int(match.group(2))
            month = _MONTHS[name]
            try:
                return month_bounds(year, month)
            except ValueError:
                return None
    return None
