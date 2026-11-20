"""Deterministic extraction of invoice and work-order fields from PDFs.

The extractor is deliberately layout-agnostic.  It never assumes a template.
Instead it works on *cells*: a row of text split at horizontal gaps, so a
label, a date and an amount are separate things even when the producer
drew them in one continuous line.

Two further ideas make the extraction reliable across four very different
invoice layouts:

* A **column header** that names a quantity, a unit price and an amount
  identifies the line-item table.  The cells underneath are then matched to
  those columns by position.
* Every number is **cross-checked against the document itself** -- quantity
  times rate must equal the line amount.  When a check fails the value is
  still returned, but the extraction is downgraded so the agent reviews it
  instead of trusting it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

import pymupdf

from .config import FORM_FIELD_TOLERANCE_PT
from .models import (
    EXTRACTION_FAILED,
    EXTRACTION_NEEDS_REVIEW,
    EXTRACTION_VERIFIED,
    FieldValue,
    InvoiceExtraction,
    LineItem,
    WorkOrderExtraction,
)
from .textutil import (
    Line,
    Word,
    clean_text,
    group_lines,
    norm_name,
    parse_amount,
    parse_month,
    parse_number,
    read_words,
)

# --------------------------------------------------------------------------- #
# Column vocabulary
# --------------------------------------------------------------------------- #

QTY_WORDS = ("quantity", "qty", "hours", "hrs", "billable hours", "hours worked")
RATE_WORDS = ("rate", "price", "unit price", "unit rate", "hourly rate", "rate/hr")
AMOUNT_WORDS = ("amount", "line total", "extended amount", "net amount", "total")
DESC_WORDS = (
    "description", "product or service", "items", "item", "activity",
    "services summary", "consultant name", "detail",
)
#: Words that never start a new column, however wide the gap.
COLUMN_CONNECTORS = ("of", "or", "and", "the", "for", "each", "per", "a", "to", "in", "-", "/")
#: Maximum gap, in points, between two header words of the same column.
COLUMN_GAP_PT = 12.0

TOTALS_LABELS = (
    "subtotal", "sub total", "total", "sales tax", "tax", "amount due",
    "balance due", "paid", "amount paid", "grand total", "net total",
)

#: A cell that starts a new field ends the value of the previous one.
LABEL_CELL_RE = re.compile(
    r"^(?:invoice|date|due|terms|total|subtotal|tax|amount|balance|paid|"
    r"bill to|ship to|to|sold to|net|payment|job|customer|qty|quantity|"
    r"rate|price|description|hours|item|items|activity|period|ref|number)",
    re.I,
)


def _normalise_label(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().lower()


def _matches_group(label: str, words: Sequence[str]) -> bool:
    label = _normalise_label(label)
    for word in words:
        if label == word:
            return True
        if len(word) > 4 and word in label:
            return True
    return False


def cell_text(cell: Sequence[Word]) -> str:
    """Join the words of one cell."""
    return " ".join(word.text for word in cell)


def cell_bounds(cell: Sequence[Word]) -> Tuple[float, float]:
    """Left and right edge of a cell."""
    return min(w.x0 for w in cell), max(w.x1 for w in cell)


# --------------------------------------------------------------------------- #
# Column detection
# --------------------------------------------------------------------------- #


@dataclass
class Column:
    """One column of a detected table."""

    x0: float
    x1: float
    label: str
    words: List[Word] = field(default_factory=list)

    @property
    def cx(self) -> float:
        return (self.x0 + self.x1) / 2.0


def detect_columns(header: Line) -> List[Column]:
    """Group a header row's words into columns.

    Words are merged into one column while the horizontal gap between them
    stays under :data:`COLUMN_GAP_PT`; connectors such as ``"of"`` always
    continue the current column.
    """
    columns: List[Column] = []
    for word in header.words:
        text = word.text.lower().strip(":.,")
        if columns and (
            word.x0 - columns[-1].x1 <= COLUMN_GAP_PT or text in COLUMN_CONNECTORS
        ):
            column = columns[-1]
            column.x0 = min(column.x0, word.x0)
            column.x1 = max(column.x1, word.x1)
            column.label = (column.label + " " + word.text).strip()
            column.words.append(word)
        else:
            columns.append(
                Column(x0=word.x0, x1=word.x1, label=word.text, words=[word])
            )
    return columns


def classify_columns(columns: Sequence[Column]) -> Dict[str, Optional[int]]:
    """Map the qty / rate / amount / description roles onto column indexes."""
    found: Dict[str, Optional[int]] = {
        "qty": None, "rate": None, "amount": None, "description": None,
    }
    descriptions: List[int] = []
    for index, column in enumerate(columns):
        label = _normalise_label(column.label)
        if found["qty"] is None and _matches_group(label, QTY_WORDS):
            found["qty"] = index
        elif found["rate"] is None and _matches_group(label, RATE_WORDS):
            found["rate"] = index
        elif found["amount"] is None and _matches_group(label, AMOUNT_WORDS):
            found["amount"] = index
        elif _matches_group(label, DESC_WORDS):
            descriptions.append(index)
    if descriptions:
        found["description"] = descriptions[0]
    found["descriptions"] = descriptions  # type: ignore[assignment]
    return found


def find_service_table(
    lines: Sequence[Line],
) -> Optional[Tuple[int, Dict[str, Optional[int]], List[Column]]]:
    """Locate the line-item table: a header naming qty, rate *and* amount."""
    for index, line in enumerate(lines):
        columns = detect_columns(line)
        roles = classify_columns(columns)
        if roles["qty"] is not None and roles["rate"] is not None and roles["amount"] is not None:
            return index, roles, columns
    return None


def assign_cells_to_columns(
    cells: Sequence[Sequence[Word]],
    columns: Sequence[Column],
) -> List[List[Word]]:
    """Match each cell to the column whose header sits closest to it.

    Using the nearest header centre rather than an exact x-range matters:
    invoice tables are often drawn with the header cell narrower than the
    data cell underneath it.
    """
    buckets: List[List[Word]] = [[] for _ in columns]
    for cell in cells:
        left, right = cell_bounds(cell)
        centre = (left + right) / 2.0
        best_index, best_distance = 0, None
        for index, column in enumerate(columns):
            distance = abs(column.cx - centre)
            if best_distance is None or distance < best_distance:
                best_index, best_distance = index, distance
        buckets[best_index].extend(cell)
    return buckets


def _is_totals_line(text: str) -> bool:
    """True for a ``Subtotal`` / ``Total`` / ``Tax`` style line."""
    cleaned = _normalise_label(text)
    return any(cleaned.startswith(label) for label in TOTALS_LABELS)


def read_line_items(
    lines: Sequence[Line],
    header_index: int,
    roles: Dict[str, Optional[int]],
    columns: Sequence[Column],
    page_no: int = 1,
) -> List[LineItem]:
    """Read the billable rows that follow a detected table header."""
    descriptions: List[int] = roles.get("descriptions") or []
    qty_col = roles["qty"]
    rate_col = roles["rate"]
    amount_col = roles["amount"]
    desc_col = roles["description"]

    items: List[LineItem] = []
    pending: List[str] = []

    def build(buckets: List[List[Word]], extra_description: List[str]) -> LineItem:
        qty = parse_number(cell_text(buckets[qty_col]))
        rate = parse_amount(cell_text(buckets[rate_col]))
        amount = parse_amount(cell_text(buckets[amount_col]))
        description = cell_text(buckets[desc_col]) if desc_col is not None else ""
        detail_parts = [
            cell_text(buckets[index])
            for index in descriptions[1:]
            if cell_text(buckets[index])
        ]
        detail_parts.extend(extra_description)
        item = LineItem(
            description=clean_text(description) or None,
            detail=clean_text(" ".join(detail_parts)) or None,
            quantity=qty,
            unit_rate=rate,
            amount=amount,
            page=page_no,
        )
        if qty is not None and rate is not None:
            expected = qty * rate
            item.arithmetic_consistent = abs(expected - (amount or expected)) < Decimal("0.01")
            if not item.arithmetic_consistent:
                item.arithmetic_note = (
                    f"{qty} x {rate} = {expected} but the invoice states {amount}"
                )
        return item

    for line in lines[header_index + 1:]:
        if _is_totals_line(line.text):
            break
        cells = line.cells()
        if not cells:
            continue
        buckets = assign_cells_to_columns(cells, columns)
        qty = parse_number(cell_text(buckets[qty_col]))
        amount = parse_amount(cell_text(buckets[amount_col]))
        desc_parts = [
            cell_text(buckets[index])
            for index in descriptions
            if cell_text(buckets[index])
        ]
        if qty is not None or amount is not None:
            # A row that carries the numbers ends any pending description.
            item = build(buckets, pending)
            pending = []
            items.append(item)
        elif desc_parts:
            joined = clean_text(" ".join(desc_parts))
            if items:
                # Continuation of the previous row (a wrapped description).
                previous = items[-1]
                previous.detail = clean_text(
                    " ".join(x for x in (previous.detail, joined) if x)
                ) or None
            else:
                pending.append(joined)
    return items


# --------------------------------------------------------------------------- #
# Form fields and inline values
# --------------------------------------------------------------------------- #


@dataclass
class FormField:
    """A label/value pair read from a document."""

    label: str
    value: str
    evidence: str
    page: int = 1


def _value_after(
    cells: Sequence[Sequence[Word]],
    start: int,
    remainder: str = "",
) -> str:
    """Collect the value that follows a label cell on the same row."""
    parts = [remainder] if remainder.strip() else []
    for cell in cells[start:]:
        text = cell_text(cell).strip()
        if text in (":", ":-", "="):
            continue
        if LABEL_CELL_RE.match(text):
            break
        parts.append(text)
    return clean_text(" ".join(parts))


def read_form_field(
    lines: Sequence[Line],
    label_pattern: str,
    page_no: int = 1,
    tolerance: float = FORM_FIELD_TOLERANCE_PT,
) -> Optional[FormField]:
    """Find a form label and read the value that belongs to it.

    The label is matched against a cell.  The value is the rest of that cell
    plus the cells to its right; when the form puts the value on the next
    printed row, the cells below and to the right of the label are used.
    """
    pattern = re.compile(label_pattern, re.I)
    for line in lines:
        cells = line.cells()
        for index, cell in enumerate(cells):
            text = clean_text(cell_text(cell))
            match = pattern.fullmatch(text)
            remainder = ""
            if match:
                pass
            else:
                match = pattern.match(text)
                if not match:
                    continue
                remainder = text[match.end():]
            value = _value_after(cells, index + 1, remainder)
            if not value:
                # The value sits on the next printed row, in the same column.
                left, right = cell_bounds(cell)
                candidates: List[Tuple[float, str]] = []
                for other in lines:
                    if other is line:
                        continue
                    if abs(other.cy - line.cy) > tolerance:
                        continue
                    for other_cell in other.cells():
                        o_left, o_right = cell_bounds(other_cell)
                        if o_left > right:
                            candidates.append(
                                (abs(other.cy - line.cy), cell_text(other_cell))
                            )
                if candidates:
                    candidates.sort(key=lambda pair: pair[0])
                    value = clean_text(candidates[0][1])
            if value:
                return FormField(
                    label=text,
                    value=value,
                    evidence=f"{text} -> {value}",
                    page=page_no,
                )
    return None


def find_inline_value(
    lines: Sequence[Line],
    pattern: str,
    page_no: int = 1,
    allow_column_fallback: bool = True,
) -> Optional[FormField]:
    """Find ``<label> <value>`` in a row of cells.

    A value sitting next to the label always wins.  Only when no row offers
    an inline value is the grid fallback used: the label is treated as a
    column header and the value is looked up underneath it, in the same
    horizontal band -- the way a printed grid aligns its columns.
    """
    regex = re.compile(pattern, re.I)
    deferred: List[Tuple[int, int, "re.Match", Sequence[Sequence[Word]]]] = []

    for row_index, line in enumerate(lines):
        cells = line.cells()
        for index, cell in enumerate(cells):
            text = clean_text(cell_text(cell))
            match = regex.search(text)
            if not match:
                continue
            remainder = text[match.end():]
            value = _value_after(cells, index + 1, remainder)
            if value:
                return FormField(
                    label=clean_text(match.group(0)),
                    value=value,
                    evidence=f"{clean_text(match.group(0))} -> {value}",
                    page=page_no,
                )
            deferred.append((row_index, index, match, cells))

    if not allow_column_fallback:
        return None

    for row_index, index, match, cells in deferred:
        label = clean_text(match.group(0))
        left, right = cell_bounds(cells[index])
        for line in lines[row_index + 1:]:
            for cell in line.cells():
                c_left, c_right = cell_bounds(cell)
                centre = (c_left + c_right) / 2.0
                if left - 25 <= centre <= right + 25:
                    value = clean_text(cell_text(cell))
                    if value:
                        return FormField(
                            label=label,
                            value=value,
                            evidence=f"{label} -> {value}",
                            page=page_no,
                        )
    return None


def _find_inline_value_in_pages(
    pages: Sequence[Sequence[Line]],
    pattern: str,
    allow_column_fallback: bool = True,
) -> Optional[FormField]:
    """Find <label> <value> on any page, in reading order.

    A one-page invoice answers immediately; a longer one puts its totals or
    its invoice number on a later page, and missing them would turn a good
    document into a false REVIEW_REQUIRED.
    """
    for page_index, page_lines in enumerate(pages):
        found = find_inline_value(
            page_lines, pattern, page_no=page_index + 1,
            allow_column_fallback=allow_column_fallback,
        )
        if found:
            return found
    return None


COMPANY_SUFFIXES = (
    "llc", "l.l.c", "inc", "inc.", "corp", "corp.", "corporation", "ltd",
    "ltd.", "limited", "co", "co.", "company", "group", "solutions",
    "technologies", "technology", "international", "services", "systems",
    "enterprises", "associates", "partners", "industries",
)


def looks_like_company(text: str) -> bool:
    """Heuristic: does this cell look like a company name?"""
    cleaned = clean_text(text)
    if not cleaned or any(ch.isdigit() for ch in cleaned):
        return False
    if "@" in cleaned or "http" in cleaned.lower():
        return False
    lowered = cleaned.lower().rstrip(".,")
    if any(lowered.endswith(suffix) for suffix in COMPANY_SUFFIXES):
        return True
    return " " in cleaned and len(cleaned.split()) <= 6


def find_issuer(lines: Sequence[Line]) -> Optional[FormField]:
    """Find the company issuing the invoice.

    The issuer sits above the ``Bill to`` / ``To`` block, so the search stops
    at the first row that names the customer.
    """
    stop = len(lines)
    for index, line in enumerate(lines):
        text = line.text
        if re.search(r"\b(bill\s+to|ship\s+to|to\s*:|sold\s+to)\b", text, re.I):
            stop = index
            break
        if re.search(r"\bcompnova\b", text, re.I):
            stop = index
            break
    for line in lines[:stop]:
        for cell in line.cells():
            text = clean_text(cell_text(cell))
            if looks_like_company(text):
                return FormField("issuer", text, text, page=1)
    return None


def find_customer(lines: Sequence[Line]) -> Optional[FormField]:
    """Find the party being billed (normally CompNova)."""
    for label in (r"bill to", r"ship to", r"sold to", r"to"):
        for line in lines:
            cells = line.cells()
            for index, cell in enumerate(cells):
                text = clean_text(cell_text(cell))
                if not re.fullmatch(label + r"[:.]?", text, re.I):
                    continue
                value = _value_after(cells, index + 1)
                if value:
                    return FormField("customer", value, text, page=1)
    for line in lines:
        for cell in line.cells():
            text = clean_text(cell_text(cell))
            if re.search(r"\bcompnova\b", text, re.I):
                return FormField("customer", text, text, page=1)
    return None


# --------------------------------------------------------------------------- #
# Work orders
# --------------------------------------------------------------------------- #

WORK_ORDER_FIELDS = {
    "vendor_name": r"Name\s+of\s+Vendor",
    "consultant_name": r"Name\s+of\s+Consultant",
    "billing_rate": r"Billing\s+Rate",
    "client_name": r"Client\s+Name\s+where\s+services\s+to\s+be\s+provided",
    "start_date": r"Auth\.\s*Start\s+Date",
    "end_date": r"Est\.\s*End\s+Date",
}

WORK_ORDER_REF_PATTERNS = (
    r"Work\s+Ref\.\s*Number",
    r"Work\s+Order\s+Ref\.?\s*Number",
    r"Work\s+Order\s+Reference",
)

_REF_TOKEN_RE = re.compile(r"\b(?:[A-Z]{2,4}-?\d{2,5}|[A-Z]\d[A-Z])\b")


def extract_work_order(path: Path) -> WorkOrderExtraction:
    """Read a vendor work order PDF into a :class:`WorkOrderExtraction`."""
    path = Path(path)
    doc = pymupdf.open(path)
    lines = group_lines(read_words(doc[0]))
    page_text = " ".join(
        cell_text(cell) for line in lines for cell in line.cells()
    )

    extraction = WorkOrderExtraction(
        source_file=path.name,
        page_count=doc.page_count,
    )

    for name, pattern in WORK_ORDER_FIELDS.items():
        field = read_form_field(lines, pattern)
        if field is None:
            extraction.warnings.append(f"No work-order field found for '{name}'.")
            continue
        setattr(extraction, name, FieldValue(
            value=field.value,
            evidence=field.evidence,
            page=field.page,
            confidence=0.95,
        ))

    # The work order reference is printed in a different place on each
    # vendor's form, so try the label first and fall back to a token scan.
    ref = None
    for pattern in WORK_ORDER_REF_PATTERNS:
        ref = read_form_field(lines, pattern)
        if ref:
            break
    if ref:
        extraction.work_order_ref = FieldValue(
            value=ref.value, evidence=ref.evidence, page=ref.page, confidence=0.9
        )
    else:
        match = _REF_TOKEN_RE.search(page_text)
        if match:
            extraction.work_order_ref = FieldValue(
                value=match.group(0),
                evidence="reference token found in the work order body",
                page=1,
                confidence=0.4,
            )

    net_days = re.search(r"No\s+discount\s+Net\s+(\d+)\s+days", page_text, re.I)
    if net_days:
        extraction.net_days = FieldValue(
            value=net_days.group(1),
            evidence=f"No discount Net {net_days.group(1)} days",
            page=1,
            confidence=0.95,
        )

    rate_text = extraction.billing_rate.value or ""
    if re.search(r"\bhourly\b", rate_text, re.I):
        extraction.rate_unit = FieldValue(
            value="hourly", evidence="work order states an hourly rate",
            page=1, confidence=0.9,
        )
    elif rate_text:
        extraction.rate_unit = FieldValue(
            value="hourly", evidence="work order bills consultants per hour",
            page=1, confidence=0.5,
        )

    doc.close()
    if not extraction.consultant_name.present():
        extraction.extraction_status = EXTRACTION_FAILED
    elif extraction.warnings:
        extraction.extraction_status = EXTRACTION_NEEDS_REVIEW
    else:
        extraction.extraction_status = EXTRACTION_VERIFIED
    return extraction


# --------------------------------------------------------------------------- #
# Invoices
# --------------------------------------------------------------------------- #

INVOICE_NUMBER_PATTERNS = (
    r"Invoice\s*(?:no\.?|number|#)\s*[:#\.]?\s*",
    r"\bInvoice\b(?!\s+(?:Date|Due|Shipped|details|no\b|#\s*Date))",
)
INVOICE_DATE_PATTERNS = (
    r"Invoice\s+Date\s*:?\s*",
    r"(?<!Due\s)(?<!Shipped\s)\bDate\b\s*:?\s*",
)
DUE_DATE_PATTERNS = (r"Due\s+Date\s*:?\s*",)
TOTALS_PATTERNS = {
    "subtotal": r"^Sub\s?Total\b\s*[:\-]?\s*",
    "total": r"^Total\b\s*[:\-]?\s*",
    "amount_due": r"^Amount\s+due\b\s*[:\-]?\s*",
    "tax": r"^(?:Sales\s+)?Tax\b\s*[:\-]?\s*",
}

PERIOD_RANGE_RE = re.compile(
    r"\d{1,2}/\d{1,2}/\d{4}\s*(?:-|to)\s*\d{1,2}/\d{1,2}/\d{4}"
)
PERIOD_MONTH_RE = re.compile(
    r"\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+\d{4}",
    re.I,
)

_TITLE_WORD_RE = re.compile(
    r"\b(?i:consulting|consultant|services|service|professional|staff)\s+"
    r"([A-Z][A-Za-z'.-]+(?:\s+[A-Z][A-Za-z'.-]+){1,2})"
)
_NAME_RE = re.compile(
    r"\b([A-Z][a-z'.-]{1,20}(?:\s+[A-Z][a-z'.-]{1,20}){1,2})\b"
)
_NAME_STOPWORDS = {
    "invoice", "bill", "total", "subtotal", "tax", "date", "due", "amount",
    "services", "service", "consulting", "consultant", "company", "corporation",
    "llc", "inc", "hours", "description", "payment", "terms", "customer",
    "job", "ship", "net", "october", "november", "september", "december",
}


def extract_consultant_name(text: str) -> str:
    """Pull a person name out of free invoice text.

    The roster match is authoritative; this is only the fallback used to give
    the agent something to look at when the name is not in the timesheet.
    """
    if not text:
        return ""
    match = _TITLE_WORD_RE.search(text)
    if match:
        return clean_text(match.group(1))
    for candidate in _NAME_RE.findall(text):
        words = candidate.split()
        if any(word.lower().strip(".,") in _NAME_STOPWORDS for word in words):
            continue
        if len(words) < 2:
            continue
        return clean_text(candidate)
    return ""


def match_contractor(text: str, roster: Sequence) -> List:
    """Find every roster contractor whose name appears in ``text``."""
    normalized = norm_name(text)
    return [c for c in roster if c.normalized_name and c.normalized_name in normalized]


def parse_filename(path: Path) -> Dict[str, str]:
    """Split the ``<Vendor>_<Consultant>_<period>.pdf`` naming convention."""
    parts = path.stem.split("_")
    result = {"vendor": "", "consultant": "", "period": ""}
    if len(parts) >= 3:
        result["vendor"] = parts[0].strip()
        result["consultant"] = parts[1].strip()
        result["period"] = parts[2].strip()
    elif len(parts) == 2:
        result["vendor"] = parts[0].strip()
        result["consultant"] = parts[1].strip()
    return result


def extract_invoice(path: Path, roster: Sequence = ()) -> InvoiceExtraction:
    """Read an invoice PDF into an :class:`InvoiceExtraction`.

    ``roster`` is the contractor registry from the master timesheet.  When a
    consultant's name can be matched against it, the contractor id comes from
    that validated mapping rather than being guessed.
    """
    path = Path(path)
    doc = pymupdf.open(path)
    pages: List[List[Line]] = [group_lines(read_words(page)) for page in doc]
    lines = pages[0] if pages else []
    doc.close()

    extraction = InvoiceExtraction(source_file=path.name, page_count=len(pages))
    warnings = extraction.warnings

    # --- line items -------------------------------------------------------
    # The billable table is not always on the first page: a long invoice can
    # push it onto page two.  Every page is searched, in reading order.
    table = None
    for page_index, page_lines in enumerate(pages):
        found = find_service_table(page_lines)
        if found is not None:
            table = (page_index, found)
            break
    if table is None:
        warnings.append(
            "No quantity/rate/amount table was found on the invoice; "
            "the amounts below are taken from the totals block only."
        )
    else:
        page_index, (header_index, roles, columns) = table
        extraction.line_items = read_line_items(
            pages[page_index], header_index, roles, columns, page_no=page_index + 1
        )
        if not extraction.line_items:
            warnings.append("The invoice table was found but contains no billable rows.")

    # --- totals -----------------------------------------------------------
    for name, pattern in TOTALS_PATTERNS.items():
        found = _find_inline_value_in_pages(
            pages, pattern, allow_column_fallback=(name != "tax")
        )
        if found and found.value:
            setattr(extraction, name, FieldValue(
                value=found.value, evidence=found.evidence, page=found.page,
                confidence=0.9,
            ))

    # --- header fields ----------------------------------------------------
    for name, patterns in (
        ("invoice_number", INVOICE_NUMBER_PATTERNS),
        ("invoice_date", INVOICE_DATE_PATTERNS),
        ("due_date", DUE_DATE_PATTERNS),
    ):
        for pattern in patterns:
            found = _find_inline_value_in_pages(pages, pattern)
            if found and found.value:
                setattr(extraction, name, FieldValue(
                    value=found.value, evidence=found.evidence, page=found.page,
                    confidence=0.9,
                ))
                break
        else:
            if name == "invoice_number":
                warnings.append("No invoice number could be located on the document.")

    issuer = find_issuer(lines)
    if issuer:
        extraction.vendor_name = FieldValue(
            value=issuer.value, evidence=issuer.evidence, page=1, confidence=0.8
        )
    else:
        warnings.append("Could not identify the company that issued the invoice.")

    customer = find_customer(lines)
    if customer:
        extraction.customer_name = FieldValue(
            value=customer.value, evidence=customer.evidence, page=1, confidence=0.7
        )

    # --- billing period ---------------------------------------------------
    document_text = " ".join(
        cell_text(cell) for page in pages for line in page for cell in line.cells()
    )
    period_evidence = ""
    period = None
    match = PERIOD_RANGE_RE.search(document_text)
    if match:
        period = parse_month(match.group(0))
        period_evidence = match.group(0)
    if period is None:
        match = PERIOD_MONTH_RE.search(document_text)
        if match:
            period = parse_month(match.group(0))
            period_evidence = match.group(0)
    if period:
        extraction.period_year, extraction.period_month = period
        extraction.billing_period = FieldValue(
            value=period_evidence, evidence=period_evidence, page=1, confidence=0.9
        )
    else:
        warnings.append(
            "The billing period could not be read from the invoice; "
            "the reconciliation needs it to pick the right timesheet rows."
        )

    # --- currency ---------------------------------------------------------
    if re.search(r"\bUSD\b", document_text):
        extraction.currency = FieldValue(
            value="USD", evidence="document states USD", page=1, confidence=0.9
        )
    elif "$" in document_text:
        extraction.currency = FieldValue(
            value="USD", evidence="amounts use the $ symbol", page=1, confidence=0.7
        )

    # --- consultant -------------------------------------------------------
    item_text = " ".join(
        " ".join(filter(None, (item.description, item.detail)))
        for item in extraction.line_items
    )
    name_guess = extract_consultant_name(item_text) or extract_consultant_name(document_text)
    if name_guess:
        extraction.contractor_name = FieldValue(
            value=name_guess, evidence=f"consultant name '{name_guess}'",
            page=1, confidence=0.7,
        )
    hits = match_contractor(item_text or document_text, roster)
    if len(hits) == 1:
        extraction.contractor_id = FieldValue(
            value=hits[0].contractor_id,
            evidence=f"matched '{hits[0].full_name}' in the master timesheet",
            page=1, confidence=0.95,
        )
        if not extraction.contractor_name.present():
            extraction.contractor_name = FieldValue(
                value=hits[0].full_name, evidence="from the master timesheet",
                page=1, confidence=0.95,
            )
    elif len(hits) > 1:
        warnings.append(
            "The consultant name matches more than one timesheet record: "
            + ", ".join(f"{h.full_name} ({h.contractor_id})" for h in hits)
        )

    # Cross-check the file name against what was read from the document.
    stem = parse_filename(path)
    if stem["vendor"] and extraction.vendor_name.present():
        if norm_name(stem["vendor"]) not in norm_name(extraction.vendor_name.value):
            warnings.append(
                f"The file name names the vendor '{stem['vendor']}' but the "
                f"document names '{extraction.vendor_name.value}'."
            )

    # --- internal consistency --------------------------------------------
    line_amount = extraction.billed_amount()
    subtotal = parse_amount(extraction.subtotal.value)
    if line_amount is not None and subtotal is not None:
        if abs(line_amount - subtotal) > Decimal("0.01"):
            warnings.append(
                f"The line items total {line_amount} but the invoice states a "
                f"subtotal of {subtotal}."
            )
    for item in extraction.line_items:
        if item.arithmetic_consistent is False:
            warnings.append(
                f"Line '{item.description or 'unnamed'}': {item.arithmetic_note}."
            )

    if not extraction.line_items and not extraction.total.present():
        extraction.extraction_status = EXTRACTION_FAILED
    elif warnings:
        extraction.extraction_status = EXTRACTION_NEEDS_REVIEW
    else:
        extraction.extraction_status = EXTRACTION_VERIFIED
    return extraction
