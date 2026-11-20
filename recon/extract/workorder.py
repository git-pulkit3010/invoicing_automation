"""Vendor Work Order parser.

The work order is the authoritative statement of the contracted hourly rate
and of the authorised engagement window. Every work order in this workflow is
the same CompNova form, so values are recovered by anchoring them to their
form label:

    Name of Vendor : Edge Centric LLC
    Name of Consultant : Chaitanya Motupalli
    Billing Rate : USD 100 hourly
    Client Name where services to be provided : AT&T
    Auth. Start Date : 04/07/26
    Est. End Date : 12/31/26

Page 2 (or 3) carries the document report: created date and verification
status.
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Optional

from ..models import WorkOrder
from ..numeric import currency_of, merge_digit_runs, parse_date, parse_number
from ..pdf_layout import Line, extract_lines, find_line, page_text

LABEL_VENDOR = r"name\s+of\s+vendor\s*:"
LABEL_CONSULTANT = r"name\s+of\s+consultant\s*:"
LABEL_RATE = r"billing\s+rate\s*:"
LABEL_CLIENT = r"client\s+name.*:"
LABEL_AUTH_START = r"auth\.?\s*start\s*date\s*:"
LABEL_EST_END = r"est\.?\s*end\s*date\s*:"
LABEL_WO_REF = r"work\s*order\s+ref(?:erence)?\.?\s*number"


def parse_work_order(path: Path) -> WorkOrder:
    """Parse a vendor work order PDF into a WorkOrder."""
    lines = extract_lines(path)
    flags: list[str] = []

    vendor = _field(lines, LABEL_VENDOR)
    consultant = _field(lines, LABEL_CONSULTANT)
    client = _field(lines, LABEL_CLIENT)
    auth_start = parse_date(_field(lines, LABEL_AUTH_START))
    est_end = parse_date(_field(lines, LABEL_EST_END))

    currency, rate, basis = _parse_rate(_field(lines, LABEL_RATE))

    work_order_ref = _work_order_ref(lines)
    work_order_number = _work_order_number(lines)
    status, created = _document_status(lines)

    if not consultant:
        flags.append("consultant name not found on the work order")
    if rate is None:
        flags.append("billing rate not found on the work order")
    if not auth_start or not est_end:
        flags.append("work order validity window could not be read")
    if status and status.lower() != "verified":
        flags.append(f"work order status is '{status}' rather than 'Verified'")

    return WorkOrder(
        path=path,
        vendor_name=vendor or None,
        consultant_name=consultant or None,
        currency=currency,
        rate=rate,
        rate_basis=basis,
        client_name=client or None,
        auth_start=auth_start,
        est_end=est_end,
        work_order_number=work_order_number,
        work_order_ref=work_order_ref,
        status=status,
        created=created,
        flags=flags,
    )


def _field(lines: list[Line], label_regex: str) -> str:
    """Text of the form value that follows a label."""
    for line in lines:
        if not re.search(label_regex, line.text, re.IGNORECASE):
            continue
        value = line.value_after_colon() or line.value_after_label(label_regex)
        value = re.sub(r"\s+", " ", value).strip()
        if value:
            return value
    return ""


def _parse_rate(raw: str) -> tuple[Optional[str], Optional[Decimal], Optional[str]]:
    """Split 'USD 100 hourly' into currency, rate and basis."""
    if not raw:
        return None, None, None
    tokens = merge_digit_runs(raw.split()).split()
    currency = None
    rate = None
    basis = None
    for token in tokens:
        code = currency_of(token)
        if code and currency is None and rate is None:
            currency = code
            continue
        value = parse_number(token)
        if value is not None and rate is None:
            rate = value
            continue
        if rate is not None and basis is None and token.lower() in {
            "hourly", "hour", "per", "hr", "monthly", "weekly", "daily", "flat",
        }:
            basis = token.lower()
    return currency, rate, basis


def _work_order_ref(lines: list[Line]) -> Optional[str]:
    """The work order reference number ('PLC-3482', or 'C2C' when unlabelled)."""
    for line in lines:
        if re.search(LABEL_WO_REF, line.text, re.IGNORECASE):
            value = line.value_after_label(LABEL_WO_REF)
            if value:
                return value
    # Unlabelled reference: a short uppercase token in the left column,
    # below the form title and above the terms paragraphs.
    for line in lines:
        if not (200 <= line.top <= 430):
            continue
        if line.x0 > 200:
            continue
        token = line.text.strip()
        if re.fullmatch(r"[A-Z0-9][A-Z0-9\-/]{1,11}", token):
            return token
    return None


def _work_order_number(lines: list[Line]) -> Optional[str]:
    """The 'WO - CE 2' style work order number."""
    for line in lines:
        match = re.search(r"\bWO\s*[-\u2013\u2014]\s*[A-Z0-9][A-Z0-9\- ]*", line.text, re.IGNORECASE)
        if match:
            return re.sub(r"\s+", " ", match.group(0)).strip()
    return None


def _document_status(lines: list[Line]) -> tuple[Optional[str], Optional[date]]:
    """Status and created date from the document report page."""
    status = None
    created = None
    for line in lines:
        text = line.text
        if status is None:
            match = re.search(r"status\s*:?\s*([A-Za-z]+)", text, re.IGNORECASE)
            if match:
                status = match.group(1).strip()
        if created is None:
            match = re.search(r"created\s*:?\s*([\d]{1,2}/[\d]{1,2}/[\d]{2,4})", text, re.IGNORECASE)
            if match:
                created = parse_date(match.group(1))
    return status, created
