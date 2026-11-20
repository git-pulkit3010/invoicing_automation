"""CompNova invoice reconciliation toolkit.

Verifies that each invoice equals (timesheet hours) x (work order hourly rate)
by cross-checking three independent sources:

  1. the CMPM ATT Time Report (Excel)  -> hours worked
  2. the vendor Work Order (PDF)        -> contracted hourly rate
  3. the vendor Invoice (PDF)           -> billed hours, rate and amount
"""

__version__ = "1.0.0"
