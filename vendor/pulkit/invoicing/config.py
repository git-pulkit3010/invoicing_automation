"""Filesystem layout and tunables for the invoice-reconciliation pipeline.

Every path is resolved relative to the project folder so the pipeline can be
copied anywhere.  Environment variables override the defaults, which keeps the
watcher and the CLI usable from a different working directory.
"""

from __future__ import annotations

import os
from pathlib import Path

#: Project root -- the ``agentic_skill`` folder.
BASE_DIR = Path(__file__).resolve().parent.parent


def _env_path(name: str, default: Path) -> Path:
    raw = os.environ.get(name)
    return Path(raw).expanduser().resolve() if raw else default


#: Where the normalised SQLite database lives.
DATA_DIR = _env_path("INVOICING_DATA_DIR", BASE_DIR / "data")
DB_PATH = _env_path("INVOICING_DB", DATA_DIR / "invoicing.db")

#: Cache for intermediate artefacts (PDF -> markdown, extracted JSON).
CACHE_DIR = _env_path("INVOICING_CACHE_DIR", DATA_DIR / "cache")

#: Where reconciliation results are written.
OUTPUT_DIR = _env_path("INVOICING_OUTPUT_DIR", BASE_DIR / "output")

#: Watched input folders.
INVOICES_DIR = _env_path("INVOICICES_DIR", BASE_DIR / "invoices")
WORK_ORDERS_DIR = _env_path("WORK_ORDERS_DIR", BASE_DIR / "work_orders")

#: The master timesheet workbook.
EXCEL_PATH = _env_path(
    "MASTER_EXCEL",
    BASE_DIR / "CMPM Report_1001-103126_Final.xlsx",
)

#: Sibling Flask project that owns the authoritative hours calculation.
SIBLING_APP_DIR = BASE_DIR.parent / "invoicing_automation"

#: Standard hours per weekday, used for the full-month benchmark.
STANDARD_DAY_HOURS = 8.0

#: Default billing period inferred from the source data when a document does
#: not state one explicitly.
DEFAULT_PERIOD_YEAR = 2026
DEFAULT_PERIOD_MONTH = 10

#: Tolerance, in PDF points, when matching a form label to its value.
FORM_FIELD_TOLERANCE_PT = 12.0

#: Tolerance, in PDF points, when clustering words into a single text row.
#: Rows of an invoice are typically 13pt or more apart, while the words
#: inside one row never differ by more than a couple of points.
LINE_CLUSTER_TOLERANCE_PT = 4.0

#: Horizontal gap, in PDF points, that separates two cells on the same row.
#: Words inside one cell are never more than a few points apart.
CELL_GAP_PT = 8.0

#: Absolute money tolerance (USD) below which two amounts are considered equal.
MONEY_EPSILON = 0.005

#: Absolute hours tolerance below which two hour counts are considered equal.
HOURS_EPSILON = 0.005


def ensure_dirs() -> None:
    """Create the output/cache directories if they do not exist yet."""
    for path in (DATA_DIR, CACHE_DIR, OUTPUT_DIR):
        path.mkdir(parents=True, exist_ok=True)
