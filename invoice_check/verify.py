"""Step 3: verify what was extracted and decide MATCH / MISMATCH / REVIEW_REQUIRED.

Verdicts and rules follow Pulkit's invoice-reconciliation skill:
  * hours come from the master timesheet, never from the invoice;
  * the rate comes from the work order in force during the billing period, and an expired
    (or missing) work order is REVIEW_REQUIRED even when the amount agrees;
  * hours x rate is compared with the invoice subtotal, so tax and adjustments on the
    payable total don't create false mismatches;
  * a missing rate, an ambiguous or unknown contractor, or an unreadable document is
    REVIEW_REQUIRED, never a confident match or mismatch;
  * duplicate or missing timesheet days are warnings, not verdicts.

The agent only reads values off the invoice. Everything here is plain code:
it confirms the agent's numbers really are on the invoice, then checks them
against the work order database and the timesheet.

Decision order
  1. REVIEW if the extraction itself can't be trusted (missing fields, a number
     that is not on the invoice, a scanned invoice read from an image).
  2. NO if any business check fails (with every reason listed).
  3. REVIEW if something needs a person but nothing failed (overtime, consultant
     not linked to an ATT ID).
  4. GO otherwise.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Optional

from .common import (fmt_money, fmt_num, name_tokens, numbers_in_text, parse_date, same_money,
                     to_decimal, vendor_key, vendors_agree)
from .timesheet import Timesheet
from .workorders import WorkOrder

GO, NO, REVIEW = "MATCH", "MISMATCH", "REVIEW_REQUIRED"
MESSAGES = {GO: "Go ahead with payment", NO: "Do not pay", REVIEW: "Needs a person to check"}
APPROVED = (GO, "GO")                                   # "GO" = records saved by earlier versions


@dataclass
class ConsultantResult:
    name: str
    attuid: Optional[str] = None
    work_order: Optional[str] = None
    invoice_hours: Optional[Decimal] = None
    timesheet_hours: Optional[Decimal] = None
    invoice_rate: Optional[Decimal] = None
    wo_rate: Optional[Decimal] = None
    invoice_amount: Optional[Decimal] = None
    expected_amount: Optional[Decimal] = None
    wo_valid: Optional[str] = None


@dataclass
class Decision:
    file: str
    vendor: str = ""
    invoice_number: str = ""
    period: str = ""
    total: Optional[Decimal] = None
    subtotal: Optional[Decimal] = None
    source: str = "text"
    decision: str = REVIEW
    untrusted: list[str] = field(default_factory=list)   # extraction problems -> REVIEW
    failures: list[str] = field(default_factory=list)    # business rule failures -> NO
    reviews: list[str] = field(default_factory=list)     # needs a person -> REVIEW
    warnings: list[str] = field(default_factory=list)    # advisory only
    misread_risk: list[str] = field(default_factory=list)  # results a misread value could cause
    consultants: list[ConsultantResult] = field(default_factory=list)

    @property
    def message(self) -> str:
        return MESSAGES[self.decision]

    @property
    def reasons(self) -> list[str]:
        if self.decision == REVIEW:
            return self.untrusted or self.reviews
        if self.decision == NO:
            return self.failures
        return []


# --------------------------------------------------------------- helpers ---
REQUIRED_LINE = ("consultant", "hours", "rate", "amount")


def _check_extraction(ext: dict, md_text: str, has_text: bool, d: Decision) -> list[dict]:
    """Validate the agent's JSON; return cleaned lines (Decimals)."""
    for key in ("vendor", "invoice_number", "period_start", "period_end", "total", "lines"):
        if ext.get(key) in (None, "", []):
            d.untrusted.append(f"extraction is missing '{key}'")
    if ext.get("currency") and str(ext["currency"]).upper() != "USD":
        d.reviews.append(f"invoice currency is {ext['currency']}, not USD")
    lines = []
    for i, ln in enumerate(ext.get("lines") or [], start=1):
        clean = {"consultant": (ln.get("consultant") or "").strip()}
        for k in ("hours", "rate", "amount"):
            clean[k] = to_decimal(ln.get(k))
        missing = [k for k in REQUIRED_LINE if clean.get(k) in (None, "")]
        if missing:
            d.untrusted.append(f"line {i}: missing {', '.join(missing)}")
        lines.append(clean)

    total = to_decimal(ext.get("total"))
    d.total = total
    d.subtotal = to_decimal(ext.get("subtotal"))
    if not has_text:
        d.source = "image"
        d.untrusted.append("scanned invoice (no text layer): values were read from an image, so a person "
                           "must confirm them before payment")
        return lines

    # every number the agent reports must literally appear on the invoice
    on_invoice = numbers_in_text(md_text)
    for i, ln in enumerate(lines, start=1):
        for k in ("hours", "rate", "amount"):
            v = ln.get(k)
            if v is not None and v.normalize() not in on_invoice:
                d.untrusted.append(f"line {i}: {k} {fmt_num(v)} does not appear on the invoice")
    if total is not None and total.normalize() not in on_invoice:
        d.untrusted.append(f"total {fmt_money(total)} does not appear on the invoice")
    if d.subtotal is not None and d.subtotal.normalize() not in on_invoice:
        d.untrusted.append(f"subtotal {fmt_money(d.subtotal)} does not appear on the invoice")
    inv_no = str(ext.get("invoice_number") or "").strip()
    if inv_no and inv_no.replace(" ", "") not in md_text.replace(" ", "").replace("\\", ""):
        d.untrusted.append(f"invoice number '{inv_no}' does not appear on the invoice")
    return lines


def _find_work_orders(name: str, vendor: str, orders: list[WorkOrder]) -> tuple[list[WorkOrder], Optional[str]]:
    """All work orders for one consultant (renewals included). Returns (orders, problem)."""
    want = name_tokens(name)
    exact = [w for w in orders if any(name_tokens(n) == want for n in w.all_names())]
    if not exact:
        exact = [w for w in orders if len(want) >= 2 and any(
            want <= name_tokens(n) or name_tokens(n) <= want for n in w.all_names() if len(name_tokens(n)) >= 2)]
    if not exact:
        return [], None
    people = {w.attuid or f"wo{w.id}" for w in exact}
    if len(people) > 1:                                  # same name, different people
        by_vendor = [w for w in exact if w.vendor and vendors_agree(w.vendor, vendor)]
        people = {w.attuid or f"wo{w.id}" for w in by_vendor}
        if len(people) != 1:
            return [], f"'{name}' matches more than one consultant; link by ATT ID manually"
        exact = by_vendor
    return exact, None


# ---------------------------------------------------------------- decide ---
def decide(ext: dict, md_text: str, has_text: bool, file_sha: str, ts: Timesheet,
           orders: list[WorkOrder], con: Optional[sqlite3.Connection]) -> Decision:
    d = Decision(file=ext.get("file", "?"), vendor=ext.get("vendor") or "",
                 invoice_number=str(ext.get("invoice_number") or ""))
    lines = _check_extraction(ext, md_text, has_text, d)
    p_start, p_end = parse_date(ext.get("period_start")), parse_date(ext.get("period_end"))
    d.period = f"{p_start or '?'} to {p_end or '?'}"

    # --- invoice arithmetic -------------------------------------------------
    for i, ln in enumerate(lines, start=1):
        if None not in (ln["hours"], ln["rate"], ln["amount"]) and not same_money(ln["hours"] * ln["rate"], ln["amount"]):
            d.failures.append(f"line {i}: {fmt_num(ln['hours'])} hrs x {fmt_money(ln['rate'])} = "
                              f"{fmt_money(ln['hours'] * ln['rate'])}, but the line says {fmt_money(ln['amount'])}")
            d.misread_risk.append(d.failures[-1])
    amounts = [ln["amount"] for ln in lines if ln["amount"] is not None]
    billed = d.subtotal if d.subtotal is not None else d.total     # hours x rate is compared with this
    label = "subtotal" if d.subtotal is not None else "total (no separate subtotal printed)"
    if billed is not None and amounts and not same_money(sum(amounts), billed):
        d.failures.append(f"invoice {label} {fmt_money(billed)} does not equal the sum of its hours lines "
                          f"{fmt_money(sum(amounts))} (difference {fmt_money(billed - sum(amounts))}: "
                          f"extra charges, discounts or a missing line)")
        d.misread_risk.append(d.failures[-1])
    if d.subtotal is not None and d.total is not None and not same_money(d.subtotal, d.total):
        d.warnings.append(f"payable total {fmt_money(d.total)} differs from the subtotal {fmt_money(d.subtotal)} "
                          f"by {fmt_money(d.total - d.subtotal)} (tax or adjustments; not part of the hours check)")

    # --- period -------------------------------------------------------------
    if p_start and p_end and (p_start, p_end) != (ts.period_start, ts.period_end):
        d.reviews.append(f"invoice period {p_start} to {p_end} is not the timesheet month checked "
                         f"({ts.period_start} to {ts.period_end}); check it against that month's timesheet")
        d.misread_risk.append(d.reviews[-1])

    # --- per consultant (several lines for one person are added together) ----
    groups: dict[str, list[dict]] = {}
    for ln in lines:
        key = " ".join(sorted(name_tokens(ln["consultant"]))) or "?"
        groups.setdefault(key, []).append(ln)

    for key, glines in groups.items():
        name = glines[0]["consultant"] or "?"
        r = ConsultantResult(name=name)
        d.consultants.append(r)
        if any(ln[k] is None for ln in glines for k in ("hours", "rate", "amount")):
            continue
        r.invoice_hours = sum(ln["hours"] for ln in glines)
        r.invoice_amount = sum(ln["amount"] for ln in glines)
        rates = {ln["rate"] for ln in glines}
        r.invoice_rate = rates.pop() if len(rates) == 1 else None

        wos, problem = _find_work_orders(name, d.vendor, orders)
        if problem:
            d.reviews.append(problem)
            d.misread_risk.append(problem)
            continue
        if not wos:
            d.reviews.append(f"no work order on record for {name}, so the rate cannot be confirmed against a contract")
            d.misread_risk.append(d.reviews[-1])
            continue
        r.attuid = wos[0].attuid
        if wos[0].vendor and d.vendor and not vendors_agree(wos[0].vendor, d.vendor):
            d.warnings.append(f"invoice vendor '{d.vendor}' differs from work order vendor '{wos[0].vendor}'")

        # work order valid for the whole invoiced period (renewals: any covering one)
        wo = None
        if p_start and p_end:
            covering = [w for w in wos if w.covers(p_start, p_end)]
            wo = covering[-1] if covering else None
            if wo is None:
                latest = max(wos, key=lambda w: w.end or date.min)
                when = (f"expired on {latest.end}" if latest.end and latest.end < p_end
                        else f"runs {latest.start} to {latest.end}")
                d.reviews.append(f"work order for {name} {when}; no work order is in force for {p_start} to "
                                 f"{p_end}, so the billed rate cannot be confirmed against a contract")
        wo = wo or max(wos, key=lambda w: w.end or date.min)
        r.work_order, r.wo_rate = wo.source_file, wo.rate
        r.wo_valid = f"{wo.start} to {wo.end}"
        if wo.status and wo.status.lower() != "verified":
            d.warnings.append(f"work order for {name} has status '{wo.status}', not 'Verified'")
        elif not wo.status:
            d.warnings.append(f"work order for {name} has no status")
        if not wo.confirmed:
            d.warnings.append(f"work order record for {name} has not been confirmed by a person yet")

        # rate
        if wo.rate is None:
            d.reviews.append(f"work order for {name} has no readable rate")
        elif wo.rate_basis and wo.rate_basis not in ("hourly", "hour", "hr"):
            d.reviews.append(f"work order rate for {name} is {wo.rate_basis}, not hourly")
        else:
            for ln in glines:
                if not same_money(ln["rate"], wo.rate):
                    d.failures.append(f"{name}: billed rate {fmt_money(ln['rate'])} is not the work order rate "
                                      f"{fmt_money(wo.rate)}")
                    d.misread_risk.append(d.failures[-1])

        # timesheet
        if not wo.attuid:
            d.reviews.append(f"{name} is not linked to an ATT ID in the work order database")
            continue
        tc = ts.contractors.get(wo.attuid)
        if tc is None:
            d.reviews.append(f"{name} ({wo.attuid}) is not in the master timesheet, so the billed hours "
                             f"cannot be verified")
            continue
        if tc.total == 0:
            d.failures.append(f"no hours recorded in the timesheet for {name} ({wo.attuid}) in this period")
            d.misread_risk.append(d.failures[-1])
            continue
        r.timesheet_hours = tc.total
        if tc.ot > 0:
            d.reviews.append(f"{name} has {fmt_num(tc.ot)} overtime hours; the overtime rate is not in the work order")
        if r.invoice_hours != tc.total:
            d.failures.append(f"{name}: invoice bills {fmt_num(r.invoice_hours)} hrs, timesheet shows "
                              f"{fmt_num(tc.total)} hrs (difference {fmt_num(r.invoice_hours - tc.total)})")
            d.misread_risk.append(d.failures[-1])
        if wo.rate is not None:
            r.expected_amount = tc.total * wo.rate
            if not same_money(r.invoice_amount, r.expected_amount):
                d.failures.append(f"{name}: invoice amount {fmt_money(r.invoice_amount)} is not timesheet hours x "
                                  f"work order rate = {fmt_num(tc.total)} x {fmt_money(wo.rate)} = "
                                  f"{fmt_money(r.expected_amount)}")
                d.misread_risk.append(d.failures[-1])
        if tc.duplicate_days:
            d.warnings.append(f"{name} has more than one timesheet row on {len(tc.duplicate_days)} day(s): "
                              + ", ".join(x.strftime("%d %b") for x in sorted(tc.duplicate_days)))
        missing = ts.missing_weekdays(wo.attuid)
        if missing:
            d.warnings.append(f"{name} has no time recorded on {len(missing)} weekday(s): "
                              + ", ".join(x.strftime("%d %b") for x in missing))

    # --- duplicate invoice ---------------------------------------------------
    if con is not None and d.invoice_number:
        prev = con.execute(
            "SELECT decided_at, file_sha256 FROM processed_invoices WHERE vendor_key=? AND invoice_number=? "
            "AND decision IN (?, ?) ORDER BY decided_at DESC LIMIT 1",
            (vendor_key(d.vendor), d.invoice_number, *APPROVED)).fetchone()
        if prev:
            same = " (same file)" if prev["file_sha256"] == file_sha else " (different file)"
            d.failures.append(f"invoice {d.invoice_number} from {d.vendor} was already approved for payment on "
                              f"{prev['decided_at'][:10]}{same}")

    # --- decision ------------------------------------------------------------
    if d.untrusted:
        d.decision = REVIEW
    elif d.failures:
        d.decision = NO
    elif d.reviews:
        d.decision = REVIEW
    else:
        d.decision = GO
    return d


def record(con: sqlite3.Connection, d: Decision, file_sha: str) -> None:
    con.execute(
        "INSERT INTO processed_invoices (vendor_key, invoice_number, file_sha256, decision, total, decided_at) "
        "VALUES (?,?,?,?,?,?)",
        (vendor_key(d.vendor), d.invoice_number or f"(no number) {d.file}", file_sha, d.decision,
         str(d.total) if d.total is not None else None, datetime.now().isoformat(timespec="seconds")))
    con.commit()


def load_extraction(path: Path) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path.name}: expected a JSON object")
    return data
