"""Invoice parser.

Four different billing systems produce the invoices in this workflow
(TCPDF, Bill.com, Stripe/Zoho-style and a QuickBooks-style template), so the
parser is built on a generic, self-validating rule rather than four
templates:

    a billable line is a row that carries three numbers where
        quantity x rate == amount

That identity is what every one of these invoices must satisfy, and it
makes the line item self-identifying: the parser can prove it found the
right row instead of guessing which column is which.

The contractor's name is then recovered by matching the row text against the
timesheet roster using exact token-set equality, so "Chaitanya Motupalli" can
never be confused with "Chaitanya Kumar Nara".
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Optional

from ..models import Invoice, InvoiceLine
from ..names import NameRoster
from ..numeric import (
    currency_of,
    find_date_range,
    find_month,
    merge_digit_runs,
    parse_date,
    parse_number,
)
from ..pdf_layout import Line, extract_lines, page_text

_FILENAME_RE = re.compile(
    r"^(?P<client>.+?)_(?P<person>.+?)_(?P<start>\d{4})-(?P<end>\d{6})\.pdf$",
    re.IGNORECASE,
)

# Words that follow the word "invoice" without being an invoice number.
_NOT_A_NUMBER = {
    "date", "due", "terms", "total", "amount", "shipped", "no", "number",
    "#", "paid", "from", "to", "address", "status",
}

_COMPANY_SUFFIX = (
    r"L\.?L\.?C\.?|Incorporated|Corporation|Corp\.?|Ltd\.?|Inc\.?|Solutions|"
    r"Technologies|Group|Services|INFO"
)
_COMPANY_RE = re.compile(
    rf"\b([A-Z][\w&.'\-]*(?:\s+[\w&.'\-]*){{0,5}}\s+(?:{_COMPANY_SUFFIX}))"
)


@dataclass
class NumberToken:
    """A number recovered from a line, with the words it came from."""

    x0: float
    x1: float
    value: Decimal
    currency: Optional[str]
    text: str
    word_indices: tuple[int, ...]


@dataclass
class FilenameInfo:
    """Client / contractor / period encoded in the file name."""

    client: Optional[str] = None
    person: Optional[str] = None
    period_start: Optional[date] = None
    period_end: Optional[date] = None


def parse_invoice(path: Path, roster: Optional[NameRoster] = None) -> Invoice:
    """Parse an invoice PDF into an Invoice."""
    lines = extract_lines(path)
    filename = _parse_filename(path)
    flags: list[str] = []

    total_amount = _find_total(lines)

    invoice_lines: list[InvoiceLine] = []
    for line, numbers, _consumed in _iter_number_lines(lines):
        for candidate in _candidate_line_items(numbers, total_amount):
            quantity, rate, amount = candidate
            if quantity.value > 0 and rate.value > 0:
                invoice_lines.append(_build_line(line, candidate, lines, roster, filename))
                break

    if not invoice_lines:
        flags.append("no billable line item could be identified")
    elif len(invoice_lines) > 1:
        flags.append(f"{len(invoice_lines)} billable line items found")

    period_start, period_end = _find_period(lines, invoice_lines)

    vendor = filename.client or _guess_issuer(lines)

    invoice = Invoice(
        path=path,
        vendor_name=vendor,
        invoice_number=_find_invoice_number(lines),
        invoice_date=_find_labeled_date(lines, [
            r"invoice\s*date\s*:?\s*",
            r"(?<!due\s)(?<!shipped\s)\bdate\s*:?\s*",
        ]),
        due_date=_find_labeled_date(lines, [r"due\s*date\s*:?\s*"])
        or _find_column_date(lines, ["due", "date"]),
        terms=_find_terms(lines),
        period_start=period_start,
        period_end=period_end,
        lines=invoice_lines,
        total_amount=total_amount,
        flags=flags,
        filename_client=filename.client,
        filename_person=filename.person,
        filename_period_start=filename.period_start,
        filename_period_end=filename.period_end,
    )
    return invoice


# --------------------------------------------------------------------------
# File name convention: <Client>_<Contractor>_<MMDD>-<MMDDYY>.pdf
# --------------------------------------------------------------------------

def _parse_filename(path: Path) -> FilenameInfo:
    match = _FILENAME_RE.match(path.name)
    if not match:
        return FilenameInfo()
    info = FilenameInfo(client=match.group("client").strip(), person=match.group("person").strip())
    start = _date_from_compact(match.group("start"), default_day=1)
    end = _date_from_compact(match.group("end"), default_day=None)
    info.period_start, info.period_end = start, end
    return info


def _date_from_compact(token: str, default_day: Optional[int]) -> Optional[date]:
    try:
        if len(token) == 4:
            month, day = int(token[:2]), int(token[2:])
        else:
            month, day, year = int(token[:2]), int(token[2:4]), int(token[4:])
            return date(2000 + year, month, day)
        year = 2026
        return date(year, month, day)
    except (ValueError, TypeError):
        return None


# --------------------------------------------------------------------------
# Numbers on a line
# --------------------------------------------------------------------------

def _line_numbers(words: list[dict]) -> tuple[list[NumberToken], set[int]]:
    """Extract numeric tokens from a line, left to right.

    A currency symbol rendered as its own run ('$' then '98.00') is attached
    to the number that follows it.
    """
    tokens: list[NumberToken] = []
    consumed: set[int] = set()
    index = 0
    while index < len(words):
        word = words[index]
        raw = word["text"].strip()
        if raw in {"$", "€", "£", "¥", "₹"}:
            currency = currency_of(raw)
            lookahead = index + 1
            while lookahead < len(words) and parse_number(words[lookahead]["text"]) is None:
                lookahead += 1
            if lookahead < len(words):
                value = parse_number(words[lookahead]["text"])
                tokens.append(NumberToken(
                    x0=words[lookahead]["x0"], x1=words[lookahead]["x1"], value=value,
                    currency=currency, text=words[lookahead]["text"],
                    word_indices=(index, lookahead),
                ))
                consumed.update({index, lookahead})
                index = lookahead + 1
                continue
            index += 1
            continue

        value = parse_number(raw)
        if value is not None:
            tokens.append(NumberToken(
                x0=word["x0"], x1=word["x1"], value=value, currency=None, text=raw,
                word_indices=(index,),
            ))
            consumed.add(index)
        index += 1
    return tokens, consumed


def _iter_number_lines(lines: list[Line]):
    """Yield (line, numbers, consumed_word_indices) for lines with enough numbers."""
    for line in lines:
        numbers, consumed = _line_numbers(line.words)
        if len(numbers) >= 3:
            yield line, numbers, consumed


def _candidate_line_items(numbers: list[NumberToken], total_amount: Optional[Decimal]):
    """All (quantity, rate, amount) triples where quantity x rate == amount."""
    candidates = []
    count = len(numbers)
    for i in range(count):
        for j in range(i + 1, count):
            for k in range(j + 1, count):
                quantity, rate, amount = numbers[i], numbers[j], numbers[k]
                if quantity.value <= 0 or rate.value <= 0:
                    continue
                if abs(quantity.value * rate.value - amount.value) > Decimal("0.01"):
                    continue
                if amount.value < rate.value:
                    continue
                candidates.append((quantity, rate, amount))
    return candidates


def _build_line(
    line: Line,
    candidate,
    lines: list[Line],
    roster: Optional[NameRoster],
    filename: FilenameInfo,
) -> InvoiceLine:
    quantity, rate, amount = candidate
    consumed: set[int] = set()
    for token in (quantity, rate, amount):
        consumed.update(token.word_indices)

    words = [w["text"] for i, w in enumerate(line.words) if i not in consumed]
    contractor_name = None
    if roster is not None:
        match = roster.find_in_text(words)
        if match:
            contractor_name = match[0]
    if contractor_name is None and filename.person:
        contractor_name = filename.person

    description = _description(line, lines)
    currency = quantity.currency or rate.currency or amount.currency or "USD"

    notes: list[str] = []
    if quantity.value != quantity.value.to_integral_value():
        notes.append("quantity is not a whole number of hours")

    return InvoiceLine(
        description=description,
        contractor_name=contractor_name,
        quantity=quantity.value,
        rate=rate.value,
        amount=amount.value,
        currency=currency,
        source_row=line.text.strip(),
        notes=notes,
    )


def _description(line: Line, lines: list[Line]) -> str:
    """Line text plus any wrapped continuation lines that carry no numbers."""
    parts = [line.text]
    index = next((i for i, l in enumerate(lines) if l is line), -1)
    current = line
    for offset in (1, 2):
        if index < 0 or index + offset >= len(lines):
            break
        nxt = lines[index + offset]
        if nxt.top - current.top > 30:
            break
        numbers, _ = _line_numbers(nxt.words)
        if numbers:
            break
        parts.append(nxt.text)
        current = nxt
    return re.sub(r"\s+", " ", " ".join(parts)).strip()


# --------------------------------------------------------------------------
# Header fields
# --------------------------------------------------------------------------

def _find_total(lines: list[Line]) -> Optional[Decimal]:
    """The invoice total, taken from a 'Total' row."""
    fallback = None
    for line in lines:
        text = line.text.strip()
        if re.match(r"^total\b", text, re.IGNORECASE) and "sales tax" not in text.lower():
            numbers, _ = _line_numbers(line.words)
            if numbers:
                return numbers[-1].value
        if re.match(r"^amount\s+due\b", text, re.IGNORECASE):
            numbers, _ = _line_numbers(line.words)
            if numbers:
                fallback = numbers[-1].value
    return fallback


def _find_invoice_number(lines: list[Line]) -> Optional[str]:
    """The invoice number.

    Every real invoice number contains at least one digit, which keeps
    'INVOICE Indotronix International Corporation' and 'Invoice details'
    from being mistaken for the number itself.
    """
    pattern = re.compile(
        r"\binvoice\s*(?:no\.?|number|#)?\s*:?\s*([A-Za-z0-9][A-Za-z0-9\-/]*)", re.IGNORECASE
    )
    for line in lines:
        for match in pattern.finditer(line.text):
            token = match.group(1)
            if token.lower() in _NOT_A_NUMBER:
                continue
            if not any(ch.isdigit() for ch in token):
                continue
            return token
    return None


def _find_labeled_date(lines: list[Line], label_patterns: list[str]) -> Optional[date]:
    """Find a date that follows a label.

    Several patterns are tried in order, so a strict 'Invoice Date:' label
    wins over a looser 'Date:' fallback.
    """
    for pattern in label_patterns:
        regex = re.compile(pattern, re.IGNORECASE)
        for line in lines:
            if not regex.search(line.text):
                continue
            value = line.value_after_colon()
            for candidate in _date_candidates(value or line.text):
                parsed = parse_date(candidate)
                if parsed:
                    return parsed
    return None


def _date_candidates(text: str) -> list[str]:
    candidates = re.findall(r"\d{1,2}/\d{1,2}/\d{2,4}", text)
    candidates += re.findall(r"[A-Z][a-z]{2,8}\.?\s+\d{1,2},\s*\d{4}", text)
    candidates += re.findall(r"\d{1,2}\s+[A-Z][a-z]{2,8}\.?\s+\d{4}", text)
    return candidates


def _find_terms(lines: list[Line]) -> Optional[str]:
    for line in lines:
        match = re.search(r"terms\s*:?\s*([A-Za-z]+\s*\d+)", line.text, re.IGNORECASE)
        if match:
            return re.sub(r"\s+", " ", match.group(1)).strip()
    return None


def _find_column_date(lines: list[Line], header_words: list[str]) -> Optional[date]:
    """Find a date that sits in the column of a table header.

    Some templates print the due date as a table cell under a 'Due Date'
    header rather than as a labelled field, so the value is located by
    horizontal position instead of by label.
    """
    header_regex = re.compile(r"\s+".join(header_words), re.IGNORECASE)
    for index, line in enumerate(lines):
        match = header_regex.search(line.text)
        if not match:
            continue
        centre = _word_centre(line, match)
        if centre is None:
            continue
        best: Optional[date] = None
        best_distance = 80.0
        for offset in range(1, 4):
            if index + offset >= len(lines):
                break
            for word in lines[index + offset].words:
                parsed = parse_date(word["text"])
                if parsed is None:
                    continue
                distance = abs((word["x0"] + word["x1"]) / 2 - centre)
                if distance < best_distance:
                    best, best_distance = parsed, distance
            if best is not None:
                return best
    return None


def _word_centre(line: Line, match: re.Match) -> Optional[float]:
    """Horizontal centre of the word that contains the start of a match."""
    cursor = 0
    for word in line.words:
        start = line.text.find(word["text"], cursor)
        if start < 0:
            cursor += len(word["text"]) + 1
            continue
        if start <= match.start() < start + len(word["text"]):
            return (word["x0"] + word["x1"]) / 2
        cursor = start + len(word["text"])
    return None


def _guess_issuer(lines: list[Line]) -> Optional[str]:
    """Best-effort detection of the company that issued the invoice."""
    candidates: list[str] = []
    for line in lines[:16]:
        for match in _COMPANY_RE.finditer(line.text):
            candidate = match.group(1).strip()
            if candidate.lower().startswith("invoice "):
                candidate = candidate[len("invoice "):].strip()
            candidates.append(candidate)
    if not candidates:
        return None
    # Prefer the longest candidate: company names are more specific than
    # short suffix matches such as "Solutions".
    return max(candidates, key=len)


# --------------------------------------------------------------------------
# Service period
# --------------------------------------------------------------------------

def _find_period(lines: list[Line], invoice_lines: list[InvoiceLine]) -> tuple[Optional[date], Optional[date]]:
    """Locate the service period near the billable line."""
    if not invoice_lines:
        return None, None

    anchor = None
    for line in lines:
        for invoice_line in invoice_lines:
            if invoice_line.source_row == line.text.strip():
                anchor = line
                break
        if anchor:
            break
    if anchor is None:
        return None, None

    index = next((i for i, l in enumerate(lines) if l is anchor), -1)
    if index < 0:
        return None, None
    window = list(range(max(0, index - 3), min(len(lines), index + 4)))
    window.sort(key=lambda i: (abs(i - index), i))

    for i in window:
        line = lines[i]
        neighbour = lines[i + 1].text if i + 1 < len(lines) else ""
        combined = f"{line.text} {neighbour}".strip()
        for text in (line.text, combined):
            found = find_date_range(text)
            if found:
                return found
        for text in (line.text, combined):
            found = find_month(text)
            if found:
                return found
    return None, None
