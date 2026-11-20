"""Position-aware PDF text extraction shared by the invoice and work order parsers.

Both document families are flattened (no AcroForm fields), so every value has
to be recovered from the glyph positions. This module turns a page into
visual "lines" of words that keep their x/y coordinates, which lets the
parsers anchor values to the form label they belong to.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional

import pdfplumber

# pdfminer emits a font-metric warning for these vendor PDFs; it is noise.
logging.getLogger("pdfminer").setLevel(logging.ERROR)

#: Words whose baselines differ by less than this are treated as one line.
LINE_TOLERANCE = 7.0


@dataclass
class Line:
    """A visual line of text with geometry preserved."""

    words: list[dict]
    page_number: int = 1

    @property
    def text(self) -> str:
        return " ".join(w["text"] for w in self.words)

    @property
    def top(self) -> float:
        return min(w["top"] for w in self.words)

    @property
    def bottom(self) -> float:
        return max(w["bottom"] for w in self.words)

    @property
    def x0(self) -> float:
        return min(w["x0"] for w in self.words)

    @property
    def x1(self) -> float:
        return max(w["x1"] for w in self.words)

    def value_after_colon(self) -> str:
        """Text following the final ':' on the line."""
        for index in range(len(self.words) - 1, -1, -1):
            if self.words[index]["text"].strip().endswith(":"):
                return " ".join(w["text"] for w in self.words[index + 1:]).strip()
        return ""

    def value_after_label(self, label_regex: str) -> str:
        """Text following the point where a label regex stops matching."""
        match = re.search(label_regex, self.text, re.IGNORECASE)
        if not match:
            return ""
        # Find the first word that starts at or after the end of the match.
        cursor = 0
        for index, word in enumerate(self.words):
            start = cursor
            cursor += len(word["text"]) + 1
            if start >= match.end():
                return " ".join(w["text"] for w in self.words[index:]).strip()
        return ""


def extract_lines(path: Path, page_numbers: Optional[Iterable[int]] = None) -> list[Line]:
    """Extract visual lines from a PDF, optionally restricted to some pages."""
    lines: list[Line] = []
    with pdfplumber.open(str(path)) as pdf:
        pages = list(pdf.pages)
        if page_numbers is not None:
            wanted = set(page_numbers)
            pages = [p for i, p in enumerate(pages, start=1) if i in wanted]
        for page_number, page in enumerate(pages, start=1):
            words = page.extract_words(use_text_flow=False)
            lines.extend(_cluster(words, page_number))
    return lines


def _cluster(words: list[dict], page_number: int) -> list[Line]:
    """Group words whose baselines are close together into visual lines."""
    clusters: list[list[dict]] = []
    for word in sorted(words, key=lambda w: (w["top"], w["x0"])):
        if clusters and abs(word["top"] - clusters[-1][-1]["top"]) <= LINE_TOLERANCE:
            clusters[-1].append(word)
        else:
            clusters.append([word])
    return [Line(words=sorted(c, key=lambda w: w["x0"]), page_number=page_number) for c in clusters]


def page_text(lines: list[Line], page_number: Optional[int] = None) -> str:
    """All text of a page (or every page) joined with spaces."""
    selected = [l for l in lines if page_number is None or l.page_number == page_number]
    return " ".join(l.text for l in selected)


def find_line(lines: list[Line], pattern: str) -> Optional[Line]:
    """First line matching a regex."""
    regex = re.compile(pattern, re.IGNORECASE)
    for line in lines:
        if regex.search(line.text):
            return line
    return None
