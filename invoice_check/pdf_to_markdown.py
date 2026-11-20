#!/usr/bin/env python3
"""
pdf_to_markdown.py - Deterministically convert the text of a PDF into clean, readable Markdown.

No AI or ML involved (PyMuPDF's optional layout model and table finder are not used), so the
same PDF always gives the same Markdown on any machine. Images are ignored. Typically < 0.1 s per page.

What it handles
  * Headings         font size relative to body text (#, ##, ###); short bold lines -> ####
  * Paragraphs       wrapped lines joined, hyphenated line breaks repaired
  * Bold / italic    preserved inline
  * Lists            bullets (•, -, ▪ ...) and numbering (1. / 1) / a) / i.)
  * Ruled tables     grids detected from the drawn lines, including rows not separated by rules
  * Checkboxes       drawn boxes become [x] / [ ]
  * Borderless tables  rows of text aligned in 3+ columns (invoices, statements, reports)
  * Key/value pairs  "Label ..... value" rows (totals, invoice details, form fields) -> bullet list
  * Short-line blocks  addresses / contact blocks keep their line breaks
  * Code             monospace text -> fenced code blocks
  * Headers/footers  repeated top/bottom lines and page numbers removed
  * Two-column pages read column by column

Usage
    python pdf_to_markdown.py input.pdf                 # writes input.md next to the PDF
    python pdf_to_markdown.py input.pdf -o out.md
    python pdf_to_markdown.py input.pdf --pages 1-5,8
    python pdf_to_markdown.py input.pdf --page-breaks   # marker between pages
    python pdf_to_markdown.py input.pdf --no-tables     # treat everything as text
    python pdf_to_markdown.py folder_of_pdfs/ -o md_out/

Requires:  pip install pymupdf
"""

from __future__ import annotations

import argparse
import re
import sys
import unicodedata
import warnings
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

warnings.filterwarnings("ignore")
try:
    import pymupdf  # PyMuPDF >= 1.24
except ImportError:
    try:
        import fitz as pymupdf  # type: ignore
    except ImportError:
        sys.exit("PyMuPDF is required:  pip install pymupdf")


# --------------------------------------------------------------------------- #
# Patterns and constants
# --------------------------------------------------------------------------- #
BULLET_CHARS = "•●○◦▪▫■□►▸‣⁃∙·\\-–—*✓✔➢➤→"
BULLET_RE = re.compile(rf"^\s*([{BULLET_CHARS}])\s+(.*)$")
NUMBERED_RE = re.compile(r"^\s*((?:\d{1,3}|[a-zA-Z]|[ivxlcdm]{1,5})[.)])\s+(.*)$", re.I)
MARKER_ONLY_RE = re.compile(rf"^\s*(?:([{BULLET_CHARS}])|(\d{{1,3}}|[a-zA-Z]|[ivxlcdm]{{1,5}})[.)]?)\s*$", re.I)
PAGE_NUM_RE = re.compile(r"^\s*(page\s*)?\d{1,4}(\s*(of|/)\s*\d{1,4})?\s*$", re.I)
MONO_FONT_RE = re.compile(r"mono|courier|consol|menlo|code|inconsolata|source\s?code", re.I)
VALUE_RE = re.compile(
    r"^\s*(?:[$£€₹¥]|rs\.?|inr|usd|gbp|eur)?\s*[-+(]?\s*[\d.,:/%\- ]*\d[\d.,:/%\- )]*\s*"
    r"(?:[$£€₹¥%]|[a-z]{1,4})?\s*$"            # money, numbers, percentages, numeric dates
    r"|^\s*\S+@\S+\.\S+\s*$"                    # e-mail
    r"|^\s*(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+\d{1,2},?\s+\d{2,4}\s*$"
    r"|^\s*\d{1,2}\s+(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?,?\s+\d{2,4}\s*$"
    r"|^\s*[A-Z]{2,}[-/]?\d[\w\-/]*\s*$",       # IDs like INV-3337
    re.I,
)

NUM_TOKEN_RE = re.compile(r"^[-+(]?[$£€₹¥]?\d[\d.,]*%?\)?$")
FLAG_ITALIC, FLAG_MONO, FLAG_BOLD = 2, 8, 16


# --------------------------------------------------------------------------- #
# Data structures
# --------------------------------------------------------------------------- #
@dataclass
class Frag:
    """A run of text on one baseline with no large horizontal gap inside it (= one cell)."""
    text: str      # markdown (inline bold/italic)
    plain: str
    size: float
    bold: bool
    italic: bool
    mono: bool
    x0: float
    y0: float
    x1: float
    y1: float
    bid: int = 0   # source block id
    base: float = 0.0  # baseline y
    words: list = field(default_factory=list)  # [(x0, x1, word)] for re-splitting cells


@dataclass
class Line:
    cells: list[Frag]
    page: int
    page_width: float

    @property
    def x0(self): return self.cells[0].x0
    @property
    def x1(self): return self.cells[-1].x1
    @property
    def y0(self): return min(c.y0 for c in self.cells)
    @property
    def y1(self): return max(c.y1 for c in self.cells)
    @property
    def text(self): return " ".join(c.text for c in self.cells)
    @property
    def plain(self): return " ".join(c.plain for c in self.cells)
    @property
    def size(self): return max(self.cells, key=lambda c: len(c.plain)).size
    @property
    def bold(self): return all(c.bold for c in self.cells)
    @property
    def mono(self): return all(c.mono for c in self.cells)
    @property
    def multi(self): return len(self.cells) >= 2


@dataclass
class Block:
    kind: str                       # "text" | "table"
    lines: list[Line] = field(default_factory=list)
    table_md: str = ""
    y0: float = 0.0
    x0: float = 0.0
    page: int = 0


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #
_REPL = {" ": " ", " ": " ", " ": " ", " ": " ", "​": "", "\t": " ",
         "ﬁ": "fi", "ﬂ": "fl", "ﬀ": "ff", "ﬃ": "ffi", "ﬄ": "ffl", "­": ""}


def clean(text: str) -> str:
    for k, v in _REPL.items():
        text = text.replace(k, v)
    # unmapped glyphs (control / private-use codes) are almost always symbol-font bullets
    out = []
    for ch in text:
        cat = unicodedata.category(ch)
        if cat == "Cs" or ch == "\ufffd":        # broken / unmappable code points
            continue
        out.append("•" if cat in ("Cc", "Co") and ch != "\n" else ch)
    return "".join(out)


def escape_md(text: str) -> str:
    return re.sub(r"([\\`*_|])", r"\\\1", text)


def span_bold(s): f = s.get("font", "").lower(); return bool(s["flags"] & FLAG_BOLD) or any(w in f for w in ("bold", "black", "heavy", "semibold"))
def span_italic(s): f = s.get("font", "").lower(); return bool(s["flags"] & FLAG_ITALIC) or "italic" in f or "oblique" in f
def span_mono(s): return bool(s["flags"] & FLAG_MONO) or bool(MONO_FONT_RE.search(s.get("font", "")))


def parse_page_ranges(spec: str, n: int) -> list[int]:
    pages: set[int] = set()
    for part in filter(None, (p.strip() for p in spec.split(","))):
        if "-" in part:
            a, b = part.split("-", 1)
            pages.update(range(int(a) if a else 1, (int(b) if b else n) + 1))
        else:
            pages.add(int(part))
    return sorted(p - 1 for p in pages if 1 <= p <= n)


def col_gap(size: float) -> float:
    """Horizontal gap that separates two cells/columns rather than two words."""
    return max(1.5 * size, 9)


def is_cell_break(left: str, right: str, gap: float, size: float) -> bool:
    """Is the gap between two pieces of text a column boundary rather than a word space?"""
    if gap > col_gap(size):
        return True
    lw, rw = left.strip().split()[-1:] or [""], right.strip().split()[:1] or [""]
    # two numbers side by side need far less space to be separate columns
    return gap > 0.6 * size and bool(NUM_TOKEN_RE.match(lw[0])) and bool(NUM_TOKEN_RE.match(rw[0]))


def inside(x: float, y: float, r: tuple) -> bool:
    return r[0] - 1 <= x <= r[2] + 1 and r[1] - 1 <= y <= r[3] + 1


# --------------------------------------------------------------------------- #
# Fragments: spans -> cells
# --------------------------------------------------------------------------- #
def make_frag(spans: list[dict], bid: int) -> Frag | None:
    pieces, sizes = [], Counter()
    n_bold = n_ital = n_mono = total = 0
    for s in spans:
        t = clean(s["text"])
        if not t:
            continue
        b, i, m = span_bold(s), span_italic(s), span_mono(s)
        n = len(t.strip())
        total += n
        n_bold += n * b; n_ital += n * i; n_mono += n * m
        sizes[round(s["size"], 1)] += n
        pieces.append((t, b, i))
    plain = re.sub(r"\s+", " ", "".join(p[0] for p in pieces)).strip()
    if not plain or not total:
        return None
    all_b, all_i, all_m = n_bold / total > 0.9, n_ital / total > 0.9, n_mono / total > 0.9
    size = sizes.most_common(1)[0][0]
    x0 = min(s["bbox"][0] for s in spans); y0 = min(s["bbox"][1] for s in spans)
    x1 = max(s["bbox"][2] for s in spans); y1 = max(s["bbox"][3] for s in spans)

    if all_m or (all_b and all_i):
        text = escape_md(plain) if not all_m else plain
    else:
        merged: list[list] = []
        for t, b, i in pieces:
            b = b and not all_b
            i = i and not all_i
            if merged and merged[-1][1] == b and merged[-1][2] == i:
                merged[-1][0] += t
            else:
                merged.append([t, b, i])
        out = []
        for t, b, i in merged:
            core = escape_md(t.strip())
            if not core:
                out.append(" ")
                continue
            mark = "***" if b and i else "**" if b else "*" if i else ""
            out.append((" " if t[:1].isspace() else "") + f"{mark}{core}{mark}" + (" " if t[-1:].isspace() else ""))
        text = re.sub(r"\s+", " ", "".join(out)).strip()
        text = re.sub(r"\*\*\s+\*\*", " ", text)
    base = Counter(round(s["origin"][1], 1) for s in spans).most_common(1)[0][0] if "origin" in spans[0] else y1
    words = []
    for s in spans:                                   # approximate word boxes inside each span
        t = clean(s["text"]); sx0, sx1 = s["bbox"][0], s["bbox"][2]
        if not t.strip():
            continue
        cw = (sx1 - sx0) / max(len(t), 1)
        for m in re.finditer(r"\S+", t):
            words.append((sx0 + m.start() * cw, sx0 + m.end() * cw, m.group()))
    return Frag(text, plain, size, all_b, all_i, all_m, x0, y0, x1, y1, bid, base, words)


def explode_span(s: dict) -> list[dict]:
    """rawdict span -> word-level pieces with exact boxes (spaces kept as their own pieces)."""
    chars = s.get("chars")
    if chars is None:                       # plain "dict" span
        return [s]
    out, cur = [], []

    def emit(group):
        if not group:
            return
        t = "".join(c["c"] for c in group)
        x0 = group[0]["bbox"][0]; x1 = group[-1]["bbox"][2]
        y0 = min(c["bbox"][1] for c in group); y1 = max(c["bbox"][3] for c in group)
        out.append({**{k: v for k, v in s.items() if k != "chars"}, "text": t, "bbox": (x0, y0, x1, y1),
                    "origin": group[0].get("origin", s.get("origin"))})

    for c in chars:
        sp = c["c"].isspace()
        if cur and sp != cur[-1]["c"].isspace():
            emit(cur); cur = []
        cur.append(c)
    emit(cur)
    return out


def split_line_into_frags(ln: dict, bid: int) -> list[Frag]:
    """Split a PyMuPDF line wherever there is a wide horizontal gap (column / tab stop)."""
    spans = [p for s in ln.get("spans", []) for p in explode_span(s) if p["text"]]
    while spans and not spans[0]["text"].strip():
        spans.pop(0)
    if not spans:
        return []
    groups, cur = [], [spans[0]]
    last_ink = spans[0]                       # last span that has visible characters
    for s in spans[1:]:
        if not s["text"].strip():             # word spaces are often separate spans: keep them
            cur.append(s)
            continue
        gap = s["bbox"][0] - last_ink["bbox"][2]
        if is_cell_break(last_ink["text"], s["text"], gap, s["size"]):
            groups.append(cur); cur = [s]
        else:
            if gap > 0.15 * s["size"] and not cur[-1]["text"][-1:].isspace() \
                    and not s["text"][:1].isspace():
                cur.append({**s, "text": " "})   # visible gap but no space char: insert one
            cur.append(s)
        last_ink = s
    groups.append(cur)
    return [f for f in (make_frag(g, bid) for g in groups) if f]


# --------------------------------------------------------------------------- #
# Page extraction
# --------------------------------------------------------------------------- #
def extract_page(page, page_no: int, use_tables: bool) -> list[Block]:
    pw = page.rect.width
    blocks: list[Block] = []

    # 1) text -> fragments grouped per source block
    data = page.get_text("rawdict", flags=pymupdf.TEXT_PRESERVE_WHITESPACE | pymupdf.TEXT_MEDIABOX_CLIP)
    raw: dict[int, list[Frag]] = {}
    for bid, b in enumerate(data.get("blocks", [])):
        if b.get("type") != 0:
            continue
        for ln in b.get("lines", []):
            dx, dy = ln.get("dir", (1, 0))
            if abs(dy) > 0.1:                       # rotated / vertical text
                continue
            for f in split_line_into_frags(ln, bid):
                raw.setdefault(bid, []).append(f)

    # 2) tables drawn with ruling lines (detected from the vector drawings, no ML involved)
    try:
        drawings = page.get_drawings()
    except Exception:
        drawings = []
    if use_tables:
        for grid in find_grids(drawings):
            md, used = grid_table(grid, raw)
            if md:
                gx0, gy0 = grid[0][0], grid[1][0]
                blocks.append(Block("table", table_md=md, y0=gy0, x0=gx0, page=page_no))
                for f in used:
                    raw[f.bid].remove(f)
        raw = {k: v for k, v in raw.items() if v}

    mark_checkboxes(drawings, raw)
    two_col = is_two_column(raw, pw)

    # 3) union blocks that share baselines (cells of the same row living in different blocks)
    parent = {b: b for b in raw}

    def find(a):
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    if not two_col:
        frags = [f for fs in raw.values() for f in fs]
        frags.sort(key=lambda f: f.base)
        for i, f in enumerate(frags):
            for g in frags[i + 1:]:
                if g.base - f.base > 2.5:
                    break
                if g.bid != f.bid and (g.x0 >= f.x1 or f.x0 >= g.x1) \
                        and (f.x1 - f.x0) < 0.45 * pw and (g.x1 - g.x0) < 0.45 * pw:
                    parent[find(g.bid)] = find(f.bid)

    groups: dict[int, list[Frag]] = {}
    for bid, fs in raw.items():
        groups.setdefault(find(bid), []).extend(fs)

    for fs in groups.values():
        lines = frags_to_lines(fs, page_no, pw)
        attach_list_markers(lines)
        if lines:
            blocks.append(Block("text", lines=lines, y0=lines[0].y0, x0=min(l.x0 for l in lines), page=page_no))

    return order_blocks(blocks, pw, two_col)


def frags_to_lines(frags: list[Frag], page_no: int, pw: float) -> list[Line]:
    frags = sorted(frags, key=lambda f: (f.base, f.x0))
    rows: list[list[Frag]] = []
    for f in frags:
        if rows and abs(rows[-1][0].base - f.base) <= 2.5:
            rows[-1].append(f)
        else:
            rows.append([f])
    lines = []
    for r in rows:
        r.sort(key=lambda f: f.x0)
        # re-join fragments that ended up adjacent (no real gap)
        merged = [r[0]]
        for f in r[1:]:
            p = merged[-1]
            if not is_cell_break(p.plain, f.plain, f.x0 - p.x1, f.size):
                mk = MARKER_ONLY_RE.match(p.plain)
                if mk and len(p.plain) <= 4:          # stand-alone bullet / number glyph
                    num = mk.group(2)
                    pre = "•" if mk.group(1) else (f"{num}." if num.isdigit() else f"{num})")
                    p = Frag(pre, pre, f.size, f.bold, f.italic, f.mono, p.x0, p.y0, p.x1, p.y1, p.bid, p.base)
                main_size = p.size if len(p.plain) >= len(f.plain) else f.size
                merged[-1] = Frag(p.text + " " + f.text, p.plain + " " + f.plain, main_size,
                                  p.bold and f.bold, p.italic and f.italic, p.mono and f.mono,
                                  p.x0, min(p.y0, f.y0), f.x1, max(p.y1, f.y1), p.bid, p.base,
                                  p.words + f.words)
            else:
                merged.append(f)
        lines.append(Line(merged, page_no, pw))
    return lines


def attach_list_markers(lines: list[Line]) -> None:
    """A bullet / number drawn as its own object becomes the first 'cell'; glue it to the text."""
    for l in lines:
        if len(l.cells) >= 2 and MARKER_ONLY_RE.match(l.cells[0].plain) and len(l.cells[0].plain) <= 4:
            m = MARKER_ONLY_RE.match(l.cells[0].plain)
            nxt = l.cells[1]
            if nxt.x0 - l.cells[0].x1 > 40:
                continue
            num = m.group(2)
            prefix = "•" if m.group(1) else (f"{num}." if num.isdigit() else f"{num})")
            l.cells[1] = Frag(prefix + " " + nxt.text, prefix + " " + nxt.plain, nxt.size, nxt.bold,
                              nxt.italic, nxt.mono, l.cells[0].x0, nxt.y0, nxt.x1, nxt.y1, nxt.bid, nxt.base)
            del l.cells[0]


CHECK_GLYPHS = {"x", "X", "✓", "✔", "✗", "✘", "■", "●", "•"}


def mark_checkboxes(drawings, raw: dict[int, list[Frag]]) -> None:
    """Small drawn squares followed by a label become '[x] label' / '[ ] label'."""
    boxes = [it[1] for d in drawings for it in d.get("items", [])
             if it[0] == "re" and 5 <= it[1].width <= 16 and abs(it[1].width - it[1].height) < 2]
    if not boxes:
        return
    frags = [f for fs in raw.values() for f in fs]
    for r in boxes:
        checked = False
        for f in list(frags):                       # a tick / cross drawn inside the box
            if f.plain in CHECK_GLYPHS and r.x0 - 2 <= (f.x0 + f.x1) / 2 <= r.x1 + 2 \
                    and r.y0 - 2 <= (f.y0 + f.y1) / 2 <= r.y1 + 2:
                checked = True
                raw[f.bid].remove(f); frags.remove(f)
        label = min((f for f in frags if 0 <= f.x0 - r.x1 < 20 and f.y0 - 3 <= (r.y0 + r.y1) / 2 <= f.y1 + 3),
                    key=lambda f: f.x0 - r.x1, default=None)
        if label:
            box = "[x] " if checked else "[ ] "
            label.text, label.plain = "• " + box + label.text, "• " + box + label.plain
            label.x0 = r.x0


def is_two_column(raw: dict[int, list[Frag]], pw: float) -> bool:
    """True only for real two-column prose (papers, newsletters), not forms or invoices."""
    mid = pw / 2
    left = right = across = total = 0
    for fs in raw.values():
        x0, x1 = min(f.x0 for f in fs), max(f.x1 for f in fs)
        chars = sum(len(f.plain) for f in fs)
        avg = chars / max(len(fs), 1)
        total += chars
        if avg < 25:                     # labels, numbers, table cells: not prose
            continue
        if x1 < mid + 10:
            left += chars
        elif x0 > mid - 10:
            right += chars
        else:
            across += chars
    return total > 0 and left > 0.15 * total and right > 0.15 * total and across < left + right


def order_blocks(blocks: list[Block], pw: float, two_col: bool) -> list[Block]:
    if not two_col:
        return sorted(blocks, key=lambda b: (round(b.y0 / 3), b.x0))
    mid = pw / 2

    def full(b: Block) -> bool:
        if b.kind == "table":
            return True
        return b.x0 < mid - 10 and max(l.x1 for l in b.lines) > mid + 10

    ordered, band = [], []
    for b in sorted(blocks, key=lambda b: b.y0):
        if full(b):
            ordered += sorted(band, key=lambda x: (x.x0 >= mid - 10, x.y0)); band = []
            ordered.append(b)
        else:
            band.append(b)
    return ordered + sorted(band, key=lambda x: (x.x0 >= mid - 10, x.y0))


# --------------------------------------------------------------------------- #
# Tables
# --------------------------------------------------------------------------- #
def md_table(rows: list[list[str]]) -> str:
    width = max(len(r) for r in rows)
    rows = [r + [""] * (width - len(r)) for r in rows]
    keep = [i for i in range(width) if any(r[i].strip() for r in rows)]
    rows = [[r[i] for i in keep] for r in rows]
    header = [h or " " for h in rows[0]]
    out = ["| " + " | ".join(header) + " |", "|" + "|".join(" --- " for _ in header) + "|"]
    out += ["| " + " | ".join(r) + " |" for r in rows[1:]]
    return "\n".join(out)


def rule_segments(drawings) -> tuple[list, list]:
    """Horizontal (y, x0, x1) and vertical (x, y0, y1) rules from the page's vector drawings."""
    hs, vs = [], []
    for d in drawings:
        stroked = "s" in (d.get("type") or "s")
        for it in d.get("items", []):
            if it[0] == "l":
                a, b = it[1], it[2]
                if abs(a.y - b.y) < 1.5 and abs(a.x - b.x) > 4:
                    hs.append(((a.y + b.y) / 2, min(a.x, b.x), max(a.x, b.x)))
                elif abs(a.x - b.x) < 1.5 and abs(a.y - b.y) > 4:
                    vs.append(((a.x + b.x) / 2, min(a.y, b.y), max(a.y, b.y)))
            elif it[0] == "re":
                r = it[1]
                if r.height < 3 and r.width > 4:                    # hairline as thin rectangle
                    hs.append(((r.y0 + r.y1) / 2, r.x0, r.x1))
                elif r.width < 3 and r.height > 4:
                    vs.append(((r.x0 + r.x1) / 2, r.y0, r.y1))
                elif stroked and r.width > 16 and r.height > 8:     # outlined box (not a checkbox)
                    hs += [(r.y0, r.x0, r.x1), (r.y1, r.x0, r.x1)]
                    vs += [(r.x0, r.y0, r.y1), (r.x1, r.y0, r.y1)]
    return hs, vs


def find_grids(drawings) -> list[tuple[list[float], list[float]]]:
    """Connected sets of crossing rules that form a table with at least one inner column rule.
    Returns (column boundaries xs, row boundaries ys) for each grid."""
    hs, vs = rule_segments(drawings)
    if not hs or not vs:
        return []
    n = len(hs) + len(vs)
    parent = list(range(n))

    def find(a):
        while parent[a] != a:
            parent[a] = parent[parent[a]]; a = parent[a]
        return a

    tol = 2.5
    for i, (y, hx0, hx1) in enumerate(hs):
        for j, (x, vy0, vy1) in enumerate(vs):
            if hx0 - tol <= x <= hx1 + tol and vy0 - tol <= y <= vy1 + tol:
                parent[find(i)] = find(len(hs) + j)
    comps: dict[int, tuple[list, list]] = {}
    for i, h in enumerate(hs):
        comps.setdefault(find(i), ([], []))[0].append(h)
    for j, v in enumerate(vs):
        comps.setdefault(find(len(hs) + j), ([], []))[1].append(v)

    def uniq(vals):
        out = []
        for v in sorted(vals):
            if not out or v - out[-1] > 2:
                out.append(v)
        return out

    grids = []
    for chs, cvs in comps.values():
        if not chs or not cvs:
            continue
        x0 = min(min(h[1] for h in chs), min(v[0] for v in cvs))
        x1 = max(max(h[2] for h in chs), max(v[0] for v in cvs))
        y0 = min(min(h[0] for h in chs), min(v[1] for v in cvs))
        y1 = max(max(h[0] for h in chs), max(v[2] for v in cvs))
        if x1 - x0 < 30 or y1 - y0 < 10:
            continue
        inner = [v for v in cvs if x0 + 5 < v[0] < x1 - 5 and v[2] - v[1] >= 0.4 * (y1 - y0)]
        if not inner:                     # a box or underlines, not a table
            continue
        xs = uniq([x0, x1] + [v[0] for v in inner])
        ys = uniq([y0, y1] + [h[0] for h in chs if h[2] - h[1] >= 0.4 * (x1 - x0)])
        grids.append((xs, ys))
    return grids


def grid_table(grid, raw: dict[int, list[Frag]]) -> tuple[str, list[Frag]]:
    xs, ys = grid
    used = [f for fs in raw.values() for f in fs
            if xs[0] - 1 <= (f.x0 + f.x1) / 2 <= xs[-1] + 1 and ys[0] - 1 <= (f.y0 + f.y1) / 2 <= ys[-1] + 1]
    if not used:
        return "", []

    def idx(bounds, v):
        for k in range(len(bounds) - 1):
            if v <= bounds[k + 1]:
                return k
        return len(bounds) - 2

    # cell -> list of text lines (grouped by baseline)
    cells: dict[tuple[int, int], list[list[Frag]]] = {}
    for f in sorted(used, key=lambda f: (f.base, f.x0)):
        key = (idx(ys, (f.y0 + f.y1) / 2), idx(xs, (f.x0 + f.x1) / 2))
        lines = cells.setdefault(key, [])
        if lines and abs(lines[-1][0].base - f.base) <= 2.5:
            lines[-1].append(f)
        else:
            lines.append([f])

    def line_text(fr: list[Frag], header: bool) -> str:
        return " ".join(cell_md(f, header) for f in sorted(fr, key=lambda f: f.x0))

    nrows, ncols = len(ys) - 1, len(xs) - 1
    rows: list[list[str]] = []
    for r in range(nrows):
        cl = [cells.get((r, c), []) for c in range(ncols)]
        filled = [x for x in cl if x]
        if not filled:
            continue
        counts = {len(x) for x in filled}
        header = not rows
        # several unruled rows inside one ruled row: same number of lines in every cell -> split them
        if len(filled) >= 2 and len(counts) == 1 and next(iter(counts)) >= 2:
            for k in range(next(iter(counts))):
                rows.append([line_text(x[k], header and k == 0) if x else "" for x in cl])
        else:
            rows.append([join_lines([line_text(ln, header) for ln in x]) for x in cl])
    if len(rows) < 1 or max(sum(1 for c in r if c) for r in rows) < 2:
        return "", []
    return md_table(rows), used


def cell_md(f: Frag, header: bool = False) -> str:
    t = f.text if not f.mono else "`" + f.plain.replace("|", "\\|") + "`"
    if f.bold and not header and not t.startswith("**"):
        t = f"**{t}**"
    if f.italic and not f.bold and not t.startswith("*"):
        t = f"*{t}*"
    return t


def render_grid(run: list[Line]) -> str:
    """Rows of aligned cells -> Markdown table (3+ columns) or key/value list (2 columns)."""
    multi = [l for l in run if l.multi]
    ncols = max(len(l.cells) for l in multi)

    if ncols == 2:
        labels = [l.cells[0].plain for l in multi]
        values = [l.cells[1].plain for l in multi]
        kv = sum(bool(re.search(r":(\s|$)", lb)) for lb in labels) + sum(bool(VALUE_RE.match(v)) for v in values)
        if kv >= len(multi) * 0.5 or len(multi) == 1:
            items: list[str] = []
            for l in run:
                if l.multi and all(re.match(r"^[^:]{1,40}:\s*\S", c.plain) for c in l.cells):
                    for c in l.cells:                         # "Signature: ____   Date: ____"
                        k, v = c.plain.split(":", 1)
                        items.append(f"- **{escape_md(k.strip())}:** {escape_md(v.strip())}")
                    continue
                if l.multi:
                    label = escape_md(" ".join(c.plain for c in l.cells[:-1]).rstrip().rstrip(":"))
                    items.append(f"- **{label}:** {cell_md(l.cells[-1])}")
                elif items:                                   # wrapped / extra value lines
                    items[-1] += "  \n  " + l.text
            return "\n".join(items)

    # column ranges from ALL rows: narrow cells first, so a wide cell can't glue two columns together
    cols: list[list[float]] = []
    for f in sorted((c for l in multi for c in l.cells), key=lambda c: (c.x1 - c.x0, c.x0)):
        hits = [k for k in cols if min(k[1], f.x1) - max(k[0], f.x0) > 0]
        if len(hits) == 1:
            hits[0][0], hits[0][1] = min(hits[0][0], f.x0), max(hits[0][1], f.x1)
        elif not hits:
            cols.append([f.x0, f.x1])
    cols = [tuple(k) for k in sorted(cols)]

    def col_of(f: Frag) -> int:
        best, score = 0, None
        for i, (a, b) in enumerate(cols):
            ov = min(b, f.x1) - max(a, f.x0)
            dist = abs((a + b) / 2 - (f.x0 + f.x1) / 2)
            s = (ov > 0, ov if ov > 0 else -dist)
            if score is None or s > score:
                best, score = i, s
        return best

    def split_wide(f: Frag) -> list[tuple[int, str]]:
        """A cell covering several columns (e.g. tightly spaced header words): split by word position."""
        hit = [i for i, (a, b) in enumerate(cols) if min(b, f.x1) - max(a, f.x0) > 0]
        if len(hit) < 2 or not f.words:
            return [(col_of(f), cell_md(f, header=not rows))]
        parts: dict[int, list[str]] = {}
        for wx0, wx1, w in f.words:
            c = min(hit, key=lambda i: 0 if cols[i][0] - 2 <= (wx0 + wx1) / 2 <= cols[i][1] + 2
                    else min(abs(cols[i][0] - wx0), abs(cols[i][1] - wx1)))
            parts.setdefault(c, []).append(w)
        mark = "**" if f.bold and rows else ""
        return [(c, mark + escape_md(" ".join(ws)) + mark) for c, ws in parts.items()]

    rows: list[list[str]] = []
    prev: Line | None = None
    prev_filled = 0
    parent_gap = None          # space above the last real row
    for l in run:
        # text wrapped inside its cells: much closer to the row above than that row is to the one
        # before it (dense tables with uniform spacing never merge), and fills few columns
        gap = l.y0 - prev.y1 if prev is not None else None
        wrapped = prev is not None and rows and gap < 0.25 * l.size \
            and parent_gap is not None and gap < parent_gap - 0.3 * l.size \
            and len(l.cells) <= max(1, prev_filled // 2)
        if wrapped or (not l.multi and rows):
            for f in l.cells:
                c = col_of(f)
                if wrapped and not f.italic:
                    rows[-1][c] = join_wrapped(rows[-1][c], cell_md(f))
                elif wrapped:
                    rows[-1][c] = (rows[-1][c] + " - " + cell_md(f)).strip()
                else:
                    sep = " - " if f.italic else " "
                    rows[-1][c] = (rows[-1][c] + sep + cell_md(f)).strip()
            prev = l
            continue
        row = [""] * len(cols)
        for f in l.cells:
            for c, txt in split_wide(f):
                row[c] = (row[c] + " " + txt).strip()
        rows.append(row)
        parent_gap = gap
        prev, prev_filled = l, sum(1 for x in row if x)
    return md_table(rows)


def join_wrapped(a: str, b: str) -> str:
    """Re-join a cell value that the PDF wrapped onto the next line."""
    if not a:
        return b
    if re.search(r"[A-Za-z]-$", a) and b[:1].isalpha():
        return a[:-1] + b                       # Ali- + son -> Alison
    if re.search(r"[\d,.]$", a) and b[:1].isdigit():
        return a + b                            # $11,26 + 4.00 -> $11,264.00
    return a + " " + b


# --------------------------------------------------------------------------- #
# Header / footer removal
# --------------------------------------------------------------------------- #
def margin_key(text: str) -> str:
    return re.sub(r"\d+", "#", text.lower()).strip()


def find_repeating_lines(pages_blocks, heights) -> set[str]:
    if len(pages_blocks) < 2:
        return set()
    counter: Counter = Counter()
    for blocks, h in zip(pages_blocks, heights):
        seen = set()
        for b in blocks:
            for l in b.lines:
                if l.y0 < h * 0.08 or l.y1 > h * 0.92:
                    k = margin_key(l.plain)
                    if k and k not in seen:
                        seen.add(k); counter[k] += 1
    return {k for k, c in counter.items() if c >= max(2, len(pages_blocks) * 0.5)}


def strip_margins(blocks, h, repeating) -> list[Block]:
    ys = [(l.y0, l.y1) for b in blocks if b.kind == "text" for l in b.lines]
    if not ys:
        return blocks
    top, bottom = min(y[0] for y in ys), max(y[1] for y in ys)
    out = []
    for b in blocks:
        if b.kind == "text":
            b.lines = [l for l in b.lines if not (
                ((l.y0 < h * 0.08 or l.y1 > h * 0.92) and margin_key(l.plain) in repeating)
                or (PAGE_NUM_RE.match(l.plain) and len(b.lines) == 1
                    and (l.y1 >= bottom - 1 or l.y0 <= top + 1)))]   # lone number at very top/bottom
            if not b.lines:
                continue
        out.append(b)
    return out


# --------------------------------------------------------------------------- #
# Headings
# --------------------------------------------------------------------------- #
def build_heading_map(blocks: list[Block]) -> tuple[float, dict[float, int]]:
    sizes: Counter = Counter()
    for b in blocks:
        for l in b.lines:
            for c in l.cells:
                sizes[c.size] += len(c.plain)
    if not sizes:
        return 0.0, {}
    body = sizes.most_common(1)[0][0]
    total = sum(sizes.values())
    bigger = sorted((s for s in sizes if s >= body + 1.5 and sizes[s] / total < 0.25), reverse=True)
    levels: list[float] = []
    for s in bigger:
        if not levels or levels[-1] - s > 0.6:
            levels.append(s)
    levels = levels[:3]
    hmap = {}
    for s in bigger:
        hmap[s] = next((i + 1 for i, lv in enumerate(levels) if abs(s - lv) <= 0.6), len(levels))
    return body, hmap


def heading_level(l: Line, body: float, hmap: dict, seg_len: int) -> int:
    if l.multi or len(l.plain) > 150 or (l.plain.endswith((".", ",", ";")) and len(l.plain) > 60):
        return 0
    if BULLET_RE.match(l.plain) or VALUE_RE.match(l.plain):
        return 0
    if l.size in hmap:
        return hmap[l.size]
    if l.bold and seg_len <= 2 and len(l.plain) < 90 and not l.plain.endswith((".", ":", ",")) \
            and abs(l.size - body) < 1.5 and "@" not in l.plain:
        return 4
    return 0


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #
def join_lines(lines: list[str]) -> str:
    out = ""
    for t in (x.strip() for x in lines):
        if not out:
            out = t
        elif re.search(r"[A-Za-z]-$", out) and t[:1].islower():
            out = out[:-1] + t
        else:
            out += " " + t
    return out


def segments(lines: list[Line]) -> list[list[Line]]:
    """Split a block wherever there is a clear vertical gap."""
    segs: list[list[Line]] = []
    for l in lines:
        if segs and l.y0 - segs[-1][-1].y1 <= 0.6 * l.size:
            segs[-1].append(l)
        else:
            segs.append([l])
    return segs


def is_short_line_block(seg: list[Line]) -> bool:
    """Addresses, contact details, signature blocks: keep the line breaks."""
    if len(seg) < 2 or len(seg) > 10 or any(l.multi or l.mono for l in seg):
        return False
    if any(BULLET_RE.match(l.plain) or NUMBERED_RE.match(l.plain) for l in seg):
        return False
    avg = sum(len(l.plain) for l in seg) / len(seg)
    widest = max(l.x1 - l.x0 for l in seg)
    if avg >= 45 or widest >= 0.5 * seg[0].page_width:
        return False
    if len(seg) == 2:
        return all(len(l.plain) < 40 for l in seg)
    # wrapped prose: most lines (except the last) run to the same right edge
    right = max(l.x1 for l in seg)
    full = sum(1 for l in seg[:-1] if right - l.x1 < 0.04 * widest and len(l.plain) >= 25)
    return full < 0.6 * (len(seg) - 1)


def render(blocks: list[Block], body: float, hmap: dict, page_breaks: bool, title: str | None) -> str:
    md: list[str] = []
    if title:
        md.append(f"# {escape_md(title)}")

    # flatten into a stream of (line, segment info) so tables can span blocks
    stream: list[tuple[str, object]] = []
    for b in blocks:
        if b.kind == "table":
            stream.append(("table", b))
            continue
        for seg in segments(b.lines):
            stream.append(("seg", seg))

    current_page = None
    last_item = None          # (chunk, marker x0, bottom y, page) of the last list item written
    i = 0
    while i < len(stream):
        kind, obj = stream[i]
        page = obj.page if kind == "table" else obj[0].page
        if page_breaks and page != current_page:
            if current_page is not None:
                md.append(f"---\n<!-- Page {page + 1} -->")
            current_page = page

        if kind == "table":
            md.append(obj.table_md)
            i += 1
            continue

        seg: list[Line] = obj
        # ---- grid rows (tables / key-value) can continue into following segments
        if any(l.multi for l in seg):
            pre = []
            while seg and not seg[0].multi:
                pre.append(seg.pop(0))
            if pre:
                stream.insert(i, ("seg", pre)); stream[i + 1] = ("seg", seg)
                continue
            run = []
            j = i
            ncat = None
            prev_line = prev_any = None
            while j < len(stream) and stream[j][0] == "seg":
                s: list[Line] = stream[j][1]
                if s[0].page != seg[0].page:
                    break
                if run and s[0].y0 - run[-1].y1 > 2.5 * s[0].size:
                    break
                ok = True
                for k, l in enumerate(s):
                    cat = None if not l.multi else (2 if len(l.cells) == 2 else 3)
                    if l.multi:
                        if ncat and cat != ncat:
                            # a 2-cell first row followed by 3+ cell rows is a header with tight spacing
                            if ncat == 2 and cat == 3 and len(run) + k == 1 and len(run + s[:k]) == 1:
                                ncat = 3
                            elif ncat == 3 and cat == 2 and prev_any is not None \
                                    and l.y0 - prev_any.y1 < 0.25 * l.size:
                                pass          # cells of the row above wrapped onto this line
                            else:
                                ok = False
                        else:
                            ncat = cat
                    else:
                        # single cell continues the row only if it sits inside the grid, close below it
                        last = prev_line
                        above = prev_any
                        tight = above is not None and l.y0 - above.y1 < 0.25 * l.size
                        if not last or l.y0 - above.y1 > 1.2 * l.size or (l.x0 <= last.x0 + 5 and not tight):
                            ok = False
                        elif tight and any(abs(l.x0 - c.x0) <= 6 for c in last.cells):
                            pass                                  # wrapped text inside a column
                        elif not any(abs(l.x0 - c.x0) <= 6 or c.x0 <= l.x0 <= c.x1 for c in last.cells[1:]):
                            ok = False
                    if not ok:
                        if k > 0:                                # split segment: rest goes back to stream
                            stream.insert(j + 1, ("seg", s[k:]))
                            stream[j] = ("seg", s[:k])
                            run += s[:k]
                            j += 1
                        break
                    prev_line = l if l.multi else prev_line
                    prev_any = l
                    if k == len(s) - 1:
                        run += s
                if not ok:
                    break
                j += 1
            if not run:                                           # safety
                run = [seg[0]]; stream[i] = ("seg", seg[1:]) if len(seg) > 1 else stream[i]; j = i + (len(seg) == 1)
            md.append(render_grid(run))
            i = max(j, i + 1)
            continue

        first = seg[0]
        if last_item and md and md[-1] is last_item[0] and first.page == last_item[3] \
                and first.x0 > last_item[1] + 3 and 0 <= first.y0 - last_item[2] < 1.5 * first.size \
                and not (BULLET_RE.match(first.plain) or NUMBERED_RE.match(first.plain) or first.multi):
            md[-1] = md[-1] + " " + join_lines([l.text for l in seg])     # wrapped list item
            last_item = (md[-1], last_item[1], seg[-1].y1, first.page)
            i += 1
            continue
        chunks = render_segment(seg, body, hmap)
        md.extend(chunks)
        last_item = None
        if chunks and re.match(r"^(- |\d+\. )", chunks[-1]):
            item_line = next((l for l in reversed(seg) if BULLET_RE.match(l.plain) or NUMBERED_RE.match(l.plain)), None)
            if item_line:
                last_item = (chunks[-1], item_line.x0, seg[-1].y1, first.page)
        i += 1

    # a table continued on the next page under an identical header becomes one table
    joined: list[str] = []
    for chunk in (c for c in md if c.strip()):
        if chunk.startswith("| ") and joined:
            k = len(joined) - 1
            while k >= 0 and joined[k].startswith("---\n<!-- Page"):
                k -= 1
            if k >= 0 and joined[k].startswith("| ") and joined[k].split("\n", 1)[0] == chunk.split("\n", 1)[0]:
                joined[k] += "\n" + chunk.split("\n", 2)[2] if chunk.count("\n") >= 2 else ""
                continue
        joined.append(chunk)

    # tight lists; blank line between different list types
    out: list[str] = []
    for chunk in joined:
        kind = "ul" if chunk.startswith("- ") else "ol" if re.match(r"^\d+\. ", chunk) else None
        last = out[-1].splitlines()[-1] if out else ""
        pk = "ul" if last.startswith(("- ", "  ")) and out[-1].startswith("- ") else \
             "ol" if re.match(r"^\d+\. ", out[-1] if out else "") else None
        if kind and kind == pk:
            out[-1] += "\n" + chunk
        else:
            out.append(chunk)
    text = "\n\n".join(out)
    return re.sub(r"\n{3,}", "\n\n", text).strip() + "\n"


def render_segment(seg: list[Line], body: float, hmap: dict) -> list[str]:
    out: list[str] = []
    para: list[str] = []
    code: list[str] = []
    list_x0 = None
    short = is_short_line_block([l for l in seg if not heading_level(l, body, hmap, len(seg))])

    def flush():
        nonlocal para, list_x0
        if para:
            out.append("  \n".join(p.strip() for p in para) if short and not list_x0 else join_lines(para))
        para, list_x0 = [], None

    def flush_code():
        nonlocal code
        if code:
            out.append("```\n" + "\n".join(code) + "\n```"); code = []

    head_buf, head_lvl = [], 0
    for n, l in enumerate(seg):
        lone_mono = l.mono and not (n > 0 and seg[n - 1].mono) and not (n + 1 < len(seg) and seg[n + 1].mono)
        if l.mono and not lone_mono:
            flush(); code.append(l.plain); continue
        if lone_mono:                                   # a single monospace line reads better inline
            flush_code()
            para.append(f"`{l.plain}`")
            continue
        flush_code()
        lvl = heading_level(l, body, hmap, len(seg))
        if lvl:
            flush()
            if head_buf and lvl == head_lvl:
                head_buf.append(l.plain); continue
            if head_buf:
                out.append("#" * head_lvl + " " + escape_md(" ".join(head_buf)))
            head_buf, head_lvl = [l.plain], lvl
            continue
        if head_buf:
            out.append("#" * head_lvl + " " + escape_md(" ".join(head_buf))); head_buf = []

        mb, mn = BULLET_RE.match(l.plain), NUMBERED_RE.match(l.plain)
        if mb or (mn and len(l.plain) < 400):
            flush()
            if mb:
                txt = re.sub(rf"^\s*(\*\*|\*)?\s*\\?[{BULLET_CHARS}]\s*", r"\1", l.text, count=1)
                para = [f"- {txt}"]
            else:
                num = mn.group(1)
                rest = l.text.split(num, 1)[1].strip() if num in l.text else mn.group(2)
                para = [f"{num[:-1]}. {rest}" if num[:-1].isdigit() else f"- **{num}** {rest}"]
            list_x0 = l.x0
            continue
        if list_x0 is not None:
            if l.x0 > list_x0 + 3:
                para.append(l.text); continue
            flush()
        text = l.text
        if not para and re.match(r"^(#|>|\d+\.\s|[-+]\s)", text):
            text = "\\" + text
        para.append(text)

    if head_buf:
        flush(); out.append("#" * head_lvl + " " + escape_md(" ".join(head_buf)))
    flush(); flush_code()
    return out


# --------------------------------------------------------------------------- #
# Driver
# --------------------------------------------------------------------------- #
def convert(pdf_path: Path, out_path: Path, pages: str | None = None, page_breaks: bool = False,
            use_tables: bool = True, password: str | None = None) -> Path:
    doc = pymupdf.open(pdf_path)
    if doc.needs_pass and (not password or not doc.authenticate(password)):
        raise RuntimeError(f"{pdf_path.name} is password-protected (use --password)")

    ids = parse_page_ranges(pages, doc.page_count) if pages else list(range(doc.page_count))
    pages_blocks, heights = [], []
    for i in ids:
        pages_blocks.append(extract_page(doc[i], i, use_tables))
        heights.append(doc[i].rect.height)

    repeating = find_repeating_lines(pages_blocks, heights)
    blocks: list[Block] = []
    for pb, h in zip(pages_blocks, heights):
        blocks += strip_margins(pb, h, repeating)

    body, hmap = build_heading_map(blocks)
    meta_title = ((doc.metadata or {}).get("title") or "").strip()
    has_h1 = any(v == 1 for v in hmap.values())
    title = meta_title if meta_title and not has_h1 and len(meta_title) < 150 \
        and meta_title.lower() not in ("untitled", "sample") else None

    md = render(blocks, body, hmap, page_breaks, title)
    if not md.strip():
        md = f"<!-- No extractable text in {pdf_path.name}: it is probably a scanned PDF and needs OCR. -->\n"
    out_path.write_text(md, encoding="utf-8")
    doc.close()
    return out_path


def main() -> None:
    ap = argparse.ArgumentParser(description="Convert PDF text into clean, readable Markdown (deterministic).")
    ap.add_argument("input", type=Path, help="PDF file, or a folder of PDFs")
    ap.add_argument("-o", "--output", type=Path, help="Output .md file (or folder when input is a folder)")
    ap.add_argument("--pages", help="Page selection, e.g. '1-5,8,10-'")
    ap.add_argument("--page-breaks", action="store_true", help="Insert a divider between pages")
    ap.add_argument("--no-tables", action="store_true", help="Disable ruled-table detection")
    ap.add_argument("--password", help="Password for encrypted PDFs")
    args = ap.parse_args()

    if args.input.is_dir():
        pdfs = sorted(p for p in args.input.iterdir() if p.suffix.lower() == ".pdf")
        if not pdfs:
            sys.exit(f"No PDFs found in {args.input}")
        out_dir = args.output or args.input
        out_dir.mkdir(parents=True, exist_ok=True)
        targets = [(p, out_dir / (p.stem + ".md")) for p in pdfs]
    else:
        if not args.input.exists():
            sys.exit(f"File not found: {args.input}")
        targets = [(args.input, args.output or args.input.with_suffix(".md"))]

    failures = 0
    for src, dst in targets:
        try:
            convert(src, dst, args.pages, args.page_breaks, not args.no_tables, args.password)
            print(f"OK   {src.name} -> {dst}")
        except Exception as e:
            failures += 1
            print(f"FAIL {src.name}: {e}", file=sys.stderr)
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
