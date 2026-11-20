"""Fast path: read invoices with Pulkit's rule-based extractor, keep only what verifies.

Tier 1 (this module, no AI, well under a second for a batch):
  his geometry extractor reads each text invoice -> our extraction JSON -> the
  same verification and checks as the agent path.
Tier 2 (only for what tier 1 can't settle):
  an agent reads the invoice Markdown, as before.

An invoice leaves tier 1 only when its result could not have been caused by a
misread: every number is on the invoice, the arithmetic holds, the consultant
resolves to exactly one work order, and any NO is for a reason that doesn't
depend on the extracted numbers (an expired work order, a duplicate).
Everything else is queued for the agent, with the reason.
"""

from __future__ import annotations

import calendar
import json
import sqlite3
import sys
from pathlib import Path
from typing import Optional

from .common import to_decimal
from .queries import who

FIELDS = ("vendor_name", "invoice_number", "invoice_date", "total", "amount_due", "subtotal",
          "contractor_name", "period_year", "period_month")


BUNDLED = Path(__file__).resolve().parent.parent / "vendor" / "pulkit"


def load_extractor(pulkit_root: Optional[Path] = None):
    """Import Pulkit's extract_invoice (the bundled copy unless another folder is given)."""
    root = str((pulkit_root or BUNDLED).resolve())
    if root not in sys.path:
        sys.path.insert(0, root)
    from invoicing.extract import extract_invoice  # type: ignore
    return extract_invoice


def _value(field) -> Optional[str]:
    if isinstance(field, dict):
        field = field.get("value")
    if field in (None, ""):
        return None
    return str(field)


def _money(text: Optional[str]) -> Optional[str]:
    """'4320.00 USD' / '$12,340.00' -> '4320.00'."""
    if not text:
        return None
    import re
    d = to_decimal(re.sub(r"\b(USD|EUR|GBP|INR|CAD)\b", "", str(text), flags=re.I))
    return str(d) if d is not None else None


def _vendor_from_filename(name: str) -> Optional[str]:
    stem = Path(name).stem
    parts = stem.split("_")
    return parts[0] if len(parts) >= 3 and " " in stem else None


def _person_from_filename(name: str) -> Optional[str]:
    """'Vendor Name_Person Name_1001-103126.pdf' -> 'Person Name' (spaces or underscores)."""
    stem = Path(name).stem
    parts = stem.split("_")
    if len(parts) >= 3 and " " in stem:
        return parts[-2]
    return None


def _pick_consultant(candidates: list[Optional[str]], vendor: str, con: sqlite3.Connection) -> tuple[Optional[str], str]:
    """First candidate name that resolves to exactly one work order."""
    tried = []
    for c in candidates:
        if not c or c in tried:
            continue
        tried.append(c)
        r = who(con, c, vendor)
        if r["result"] == "match":
            return c, "resolved"
    return (tried[0] if tried else None), "unresolved: " + ", ".join(repr(t) for t in tried)


def to_extraction(raw: dict, pdf_name: str, con: sqlite3.Connection) -> dict:
    """Map Pulkit's InvoiceExtraction to our extraction JSON."""
    v = {k: _value(raw.get(k)) for k in FIELDS}
    vendor = v["vendor_name"] or _vendor_from_filename(pdf_name) or ""
    year, month = to_decimal(v["period_year"]), to_decimal(v["period_month"])
    if year and month:
        y, m = int(year), int(month)
        period = (f"{y:04d}-{m:02d}-01", f"{y:04d}-{m:02d}-{calendar.monthrange(y, m)[1]:02d}")
    else:
        period = (None, None)
    total = _money(v["amount_due"]) or _money(v["total"]) or _money(v["subtotal"])

    lines, notes = [], []
    for li in raw.get("line_items") or []:
        name, how = _pick_consultant(
            [li.get("description"), v["contractor_name"], _person_from_filename(pdf_name)], vendor, con)
        if how != "resolved":
            notes.append(f"consultant {how}")
        lines.append({"consultant": name or "", "hours": li.get("quantity"), "rate": li.get("unit_rate"),
                      "amount": li.get("amount")})
    if not v["vendor_name"] and vendor:
        notes.append(f"vendor taken from the file name: {vendor}")
    for w in raw.get("warnings") or []:
        notes.append(str(w))
    return {
        "file": pdf_name, "vendor": vendor, "invoice_number": v["invoice_number"],
        "invoice_date": v["invoice_date"], "period_start": period[0], "period_end": period[1],
        "currency": _value(raw.get("currency")) or "USD", "lines": lines,
        "total": total, "subtotal": _money(v["subtotal"]),
        "notes": " | ".join(notes), "source": "prefill",
    }


def prefill(manifest: dict, work: Path, pulkit_root: Optional[Path], con: sqlite3.Connection) -> dict[str, dict]:
    """Run the rule-based extractor on every text invoice. Returns {pdf name: extraction}."""
    extract_invoice = load_extractor(pulkit_root)
    out = {}
    for name, m in manifest.items():
        if not m["has_text"]:
            continue
        try:
            raw = extract_invoice(Path(m["pdf"]))
            raw = raw.model_dump(mode="json") if hasattr(raw, "model_dump") else raw
        except Exception as e:                      # noqa: BLE001 - any failure just means "ask the agent"
            out[name] = {"file": name, "error": f"rule-based extractor failed: {e}"}
            continue
        out[name] = to_extraction(raw, name, con)
    return out
