"""Low-level PDF geometry helpers and value normalisers.

Everything here is deterministic.  Two ideas make extraction robust across
four very different invoice layouts:

1. **Words carry coordinates.**  Grouping words into visual lines and then
   into columns lets us read a table or a form without depending on the
   order the producer happened to write the text stream in.

2. **Values are cross-checked arithmetically.**  A quantity and a unit price
   must multiply to the line amount.  When they do not, the extraction is
   flagged instead of being trusted.
"""

from __future__ import annotations

import datetime as dt
import re
import unicodedata
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import List, Optional, Sequence

import pymupdf

from .config import CELL_GAP_PT, LINE_CLUSTER_TOLERANCE_PT

# --------------------------------------------------------------------------- #
# Geometry
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Word:
    """A single word with its bounding box, in PDF points."""

    x0: float
    y0: float
    x1: float
    y1: float
    text: str

    @property
    def cy(self) -> float:
        """Vertical centre of the word."""
        return (self.y0 + self.y1) / 2.0

    @property
    def cx(self) -> float:
        """Horizontal centre of the word."""
        return (self.x0 + self.x1) / 2.0


@dataclass
class Line:
    """A visual line: words that share a baseline band."""

    words: List[Word] = field(default_factory=list)

    @property
    def y0(self) -> float:
        return min(w.y0 for w in self.words) if self.words else 0.0

    @property
    def y1(self) -> float:
        return max(w.y1 for w in self.words) if self.words else 0.0

    @property
    def cy(self) -> float:
        return (self.y0 + self.y1) / 2.0

    @property
    def x0(self) -> float:
        return min(w.x0 for w in self.words) if self.words else 0.0

    @property
    def x1(self) -> float:
        return max(w.x1 for w in self.words) if self.words else 0.0

    @property
    def text(self) -> str:
        return " ".join(w.text for w in self.words)

    def cells(self, gap: float = CELL_GAP_PT) -> List[List[Word]]:
        """Split the line into cells separated by a horizontal gap.

        A cell is a run of words that visually belong together -- a label, a
        date, a number.  This is the unit that actually carries meaning in
        an invoice, and it is what makes a single extraction routine work
        across four completely different layouts.
        """
        if not self.words:
            return []
        groups: List[List[Word]] = [[self.words[0]]]
        for word in self.words[1:]:
            if word.x0 - groups[-1][-1].x1 > gap:
                groups.append([word])
            else:
                groups[-1].append(word)
        return groups

    def words_right_of(self, x: float) -> List[Word]:
        return [w for w in self.words if w.x0 >= x]

    def span(self) -> str:
        """Text of the words between two x positions."""
        return " ".join(w.text for w in self.words if w.x0 >= x)


def read_words(page: "pymupdf.Page") -> List[Word]:
    """Return every word on a page, sorted top-to-bottom then left-to-right."""
    raw = page.get_text("words")
    words = [Word(w[0], w[1], w[2], w[3], w[4]) for w in raw]
    words.sort(key=lambda w: (round(w.cy / 3.0), w.x0))
    return words


def group_lines(words: Sequence[Word]) -> List[Line]:
    """Cluster words into visual lines.

    Words that sit within ``LINE_CLUSTER_TOLERANCE_PT`` of each other
    vertically belong to the same printed row, even when they are in
    different columns (a table header, for example).  The row is then split
    into cells by :meth:`Line.cells`.
    """
    lines: List[Line] = []
    for word in sorted(words, key=lambda w: (w.cy, w.x0)):
        if lines and abs(word.cy - lines[-1].cy) <= LINE_CLUSTER_TOLERANCE_PT:
            lines[-1].words.append(word)
        else:
            lines.append(Line(words=[word]))
    for line in lines:
        line.words.sort(key=lambda w: w.x0)
    return lines


def page_lines(page: "pymupdf.Page") -> List[Line]:
    """Convenience wrapper: words -> grouped lines for one page."""
    return group_lines(read_words(page))


# --------------------------------------------------------------------------- #
# Value normalisers
# --------------------------------------------------------------------------- #

_CURRENCY_SYMBOLS = "$€£¥₹"
_AMOUNT_RE = re.compile(
    r"(?P<neg>-)?\s*(?P<sym>[" + _CURRENCY_SYMBOLS + r"])?\s*"
    r"(?P<num>\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)"
    r"\s*(?P<code>[A-Z]{3})?"
)
_DATE_PATTERNS = (
    "%m/%d/%Y", "%m/%d/%y", "%Y-%m-%d", "%b %d, %Y", "%B %d, %Y",
    "%d %b %Y", "%d %B %Y", "%m-%d-%Y", "%m-%d-%y",
)


def clean_text(value: Optional[str]) -> str:
    """Collapse whitespace and strip stray unicode."""
    if not value:
        return ""
    return re.sub(r"\s+", " ", value.replace(" ", " ")).strip()


def norm_name(value: Optional[str]) -> str:
    """Normalise a person name for comparison.

    Uppercases, drops punctuation, and collapses whitespace so that
    ``"Jordan LEE Smith"`` and ``"jordan lee smith"`` compare equal.
    """
    if not value:
        return ""
    text = unicodedata.normalize("NFKD", value)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.replace("&", " and ")
    text = re.sub(r"[^A-Za-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip().upper()


def parse_amount(value: Optional[str]) -> Optional[Decimal]:
    """Parse ``$1,234.56`` / ``4320.00 USD`` into a :class:`Decimal`.

    Returns ``None`` when no number can be recovered.
    """
    if value is None:
        return None
    text = clean_text(value)
    if not text:
        return None
    match = _AMOUNT_RE.search(text)
    if not match:
        return None
    raw = match.group("num").replace(",", "")
    try:
        amount = Decimal(raw)
    except InvalidOperation:
        return None
    if match.group("neg"):
        amount = -amount
    return amount


def parse_number(value: Optional[str]) -> Optional[Decimal]:
    """Parse a plain quantity (``176``, ``176.00``) into a :class:`Decimal`."""
    if value is None:
        return None
    text = clean_text(value).replace(",", "")
    match = re.search(r"-?\d+(?:\.\d+)?", text)
    if not match:
        return None
    try:
        return Decimal(match.group(0))
    except InvalidOperation:
        return None


def parse_date(value: Optional[str]) -> Optional[dt.date]:
    """Parse the date formats that appear in these documents."""
    if not value:
        return None
    text = clean_text(value)
    if not text:
        return None
    for pattern in _DATE_PATTERNS:
        try:
            return dt.datetime.strptime(text, pattern).date()
        except ValueError:
            continue
    match = re.search(r"\d{1,2}/\d{1,2}/\d{2,4}", text)
    if match:
        return parse_date(match.group(0))
    return None


def parse_date_range(value: Optional[str]) -> tuple:
    """Parse ``10/01/2026 - 10/31/2026`` into a ``(start, end)`` pair."""
    if not value:
        return (None, None)
    text = clean_text(value)
    parts = re.split(r"\s*(?:-|to|through|thru)\s*", text)
    if len(parts) >= 2:
        return (parse_date(parts[0]), parse_date(parts[-1]))
    single = parse_date(text)
    return (single, single)


def parse_month(text: Optional[str]) -> Optional[tuple]:
    """Parse a billing period reference into a ``(year, month)`` pair.

    Accepts ``Oct 2026``, ``October 2026``, ``2026-10`` and
    ``10/01/2026 - 10/31/2026``.
    """
    if not text:
        return None
    value = clean_text(text)
    for pattern in (
        r"\b(?P<mon>jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)"
        r"[a-z]*\.?\s+(?P<year>\d{4})",
        r"\b(?P<year>\d{4})-(?P<mon>\d{1,2})\b",
    ):
        match = re.search(pattern, value, re.I)
        if not match:
            continue
        months = {
            "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
            "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
        }
        mon_raw = match.group("mon")
        month = months.get(mon_raw[:3].lower()) if not mon_raw.isdigit() else int(mon_raw)
        year = int(match.group("year"))
        if month:
            return (year, month)
    start, end = parse_date_range(value)
    if start:
        return (start.year, start.month)
    if end:
        return (end.year, end.month)
    return None


# --- APPEND-POINT-2 ---


# --------------------------------------------------------------------------- #
# Markdown helpers
#
# The documents are also read as Markdown: a small, plain-text rendering that
# the agent can read directly and that gives the pipeline a *second*,
# independent reading of the same invoice (see :mod:`invoicing.markdown`).
# These helpers are the value-level primitives that rendering needs.
# --------------------------------------------------------------------------- #

_MARKDOWN_DECORATION_RE = re.compile(r"[*_`~]+|\[(.*?)\]\([^)]*\)")
_MARKDOWN_NUMBER_RE = re.compile(r"-?\d[\d,]*(?:\.\d+)?")


def strip_markdown(text: Optional[str]) -> str:
    """Remove Markdown decoration, keeping the words.

    ``**Total:**`` becomes ``Total:``, ``[link](http://x)`` becomes ``link``.
    Used when a Markdown cell has to be compared with a value that was read
    from the PDF geometry.
    """
    if not text:
        return ""
    cleaned = _MARKDOWN_DECORATION_RE.sub(lambda m: m.group(1) or "", text)
    return clean_text(cleaned)


def markdown_numbers(text: Optional[str]) -> List[Decimal]:
    """Every number that appears in a Markdown document, in reading order.

    Grouped thousands separators are removed, so ``$12,340.00`` becomes
    ``Decimal('12340.00')``.  A bare year or a stray page number is included
    too: the caller decides what a number *means*, this only says which
    numbers the document shows.
    """
    if not text:
        return []
    values: List[Decimal] = []
    for raw in _MARKDOWN_NUMBER_RE.findall(text):
        cleaned = raw.replace(",", "")
        try:
            values.append(Decimal(cleaned))
        except InvalidOperation:
            continue
    return values


def same_number(left: Optional[Decimal], right: Optional[Decimal]) -> bool:
    """Compare two numbers with the money tolerance used everywhere else."""
    if left is None or right is None:
        return False
    return abs(left - right) <= Decimal("0.005")
