"""Runtime configuration for a reconciliation run."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

DEFAULT_EXCEL = "CMPM Report_1001-103126_Final.xlsx"
DEFAULT_INVOICES = "invoices"
DEFAULT_WORK_ORDERS = "work_orders"


@dataclass
class ReconConfig:
    """Paths, tolerances and policy switches for one reconciliation run."""

    excel: Path
    invoices_dir: Path
    work_orders_dir: Path

    #: Absolute money tolerance when comparing two amounts (USD).
    amount_tolerance: Decimal = Decimal("0.01")
    #: Absolute tolerance when comparing two hourly rates.
    rate_tolerance: Decimal = Decimal("0.01")
    #: Absolute tolerance when comparing two hour counts.
    hours_tolerance: Decimal = Decimal("0.01")
    #: How many days of drift between invoice period and report period are tolerated.
    period_tolerance_days: int = 0
    #: Include timesheet rows whose own period falls outside the report period.
    include_out_of_period_rows: bool = False
    #: Treat a missing work order as a FAIL rather than a SKIP.
    missing_work_order_is_failure: bool = True
    #: Minimum plausible hours on an invoice line (guards against bad parses).
    min_plausible_hours: Decimal = Decimal("0")
    #: Maximum plausible hours on an invoice line for a single person-month.
    max_plausible_hours: Decimal = Decimal("744")

    @classmethod
    def from_root(
        cls,
        root: Path,
        excel: Path | None = None,
        invoices_dir: Path | None = None,
        work_orders_dir: Path | None = None,
        **overrides,
    ) -> "ReconConfig":
        cfg = cls(
            excel=(excel or (root / DEFAULT_EXCEL)).resolve(),
            invoices_dir=(invoices_dir or (root / DEFAULT_INVOICES)).resolve(),
            work_orders_dir=(work_orders_dir or (root / DEFAULT_WORK_ORDERS)).resolve(),
        )
        for key, value in overrides.items():
            if value is not None and hasattr(cfg, key):
                setattr(cfg, key, value)
        return cfg
