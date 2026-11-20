"""The one database (SQLite file) every step reads from.

Tables
  work_orders         one row per work order (rate, validity, ATT ID link)
  timesheet_loads     one row per loaded month (which file, when, how many rows)
  timesheet_days      every daily row of the ATT Time Report, per month
  processed_invoices  decisions saved with --record (duplicate-approval check)
View
  monthly_hours       total / regular / overtime hours and days per ATT ID per month

All lookups the agent needs go through queries.py, never through the files.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS work_orders (
    id              INTEGER PRIMARY KEY,
    attuid          TEXT,
    consultant_name TEXT NOT NULL,
    aliases         TEXT NOT NULL DEFAULT '[]',
    vendor          TEXT,
    rate            TEXT,
    currency        TEXT,
    rate_basis      TEXT,
    start_date      TEXT,
    end_date        TEXT,
    status          TEXT,
    source_file     TEXT,
    source_sha256   TEXT UNIQUE,
    link_method     TEXT,
    confirmed       INTEGER NOT NULL DEFAULT 0,
    updated_at      TEXT
);
CREATE INDEX IF NOT EXISTS ix_wo_attuid ON work_orders(attuid);

CREATE TABLE IF NOT EXISTS timesheet_loads (
    month           TEXT PRIMARY KEY,          -- YYYY-MM
    period_start    TEXT NOT NULL,
    period_end      TEXT NOT NULL,
    source_file     TEXT NOT NULL,
    source_sha256   TEXT NOT NULL,
    rows_loaded     INTEGER NOT NULL,
    rows_excluded   INTEGER NOT NULL,          -- rows dated outside the report period
    loaded_at       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS timesheet_days (
    month           TEXT NOT NULL REFERENCES timesheet_loads(month),
    attuid          TEXT NOT NULL,
    name            TEXT NOT NULL,
    day             TEXT,                      -- YYYY-MM-DD (NULL if the row's date is unreadable)
    in_period       INTEGER NOT NULL,          -- 0 = dated outside the report month: kept, not counted
    regular_hours   TEXT NOT NULL,             -- decimals stored as text: exact
    ot_hours        TEXT NOT NULL,
    correcting      INTEGER NOT NULL DEFAULT 0,
    source_row      INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_tsd_month_attuid ON timesheet_days(month, attuid);

CREATE VIEW IF NOT EXISTS monthly_hours AS
SELECT month, attuid, MAX(name) AS name,
       SUM(CAST(regular_hours AS REAL)) AS regular_hours,
       SUM(CAST(ot_hours AS REAL))      AS ot_hours,
       SUM(CAST(regular_hours AS REAL) + CAST(ot_hours AS REAL)) AS total_hours,
       COUNT(DISTINCT day) AS days_recorded
FROM timesheet_days WHERE in_period = 1 GROUP BY month, attuid;

CREATE TABLE IF NOT EXISTS processed_invoices (
    vendor_key      TEXT NOT NULL,
    invoice_number  TEXT NOT NULL,
    file_sha256     TEXT,
    decision        TEXT NOT NULL,
    total           TEXT,
    decided_at      TEXT NOT NULL,
    PRIMARY KEY (vendor_key, invoice_number, decided_at)
);
"""


def connect(db_path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    con.executescript(SCHEMA)
    return con
