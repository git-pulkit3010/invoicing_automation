# invoice-check

Checks vendor invoices before payment against the ATT Time Report and the work orders, and gives
each invoice one of three answers:

- **GO**: go ahead with payment
- **NO**: do not pay, with every reason listed
- **REVIEW**: a person must check it (a scanned invoice, values that could not be verified, overtime,
  a consultant who matches two people)

AI only reads invoices it has to; code makes every decision. On real data, a monthly batch takes
about 13 s, and a batch of 200 clean invoices takes 3.3 s with no AI at all. See [TIMINGS.md](TIMINGS.md).

## How it works

```
                          ┌───────────────── database (SQLite, data/recon.db) ─────────────────┐
ATT Time Report .xlsx ──► │ ts-load:   timesheet_days + monthly_hours, per ATT ID per month   │
work order PDFs ────────► │ wo-ingest: work_orders (rate, dates, status, linked to an ATT ID)  │
                          └────────────────────────────────▲───────────────────────────────────┘
                                                           │ every check queries the database
invoice PDFs ─► 1 convert ─► 2 rules read ─► 3 verify ─────┴─► settled ───────────────► 5 decide ─► report
               (Markdown)   (Pulkit's      (numbers on the                                  GO / NO / REVIEW
                             extractor)     invoice? sums?    misread risk / scan
                                            known person?) ─► 4 agent reads Markdown ─► (same checks)
```

1. **Convert**: each PDF becomes Markdown. PDFs with no text layer are marked as scanned.
2. **Rules read**: Pulkit's rule-based extractor (bundled in `vendor/pulkit/`) reads every text
   invoice from the PDF layout, in milliseconds.
3. **Verify**: every value it read must appear on the invoice, hours x rate must equal each line,
   the lines must add up to the total, and the consultant must match one work order. An invoice
   is settled here only if none of its results could come from a misread. An expired work order
   or a duplicate is a fact, not a misread, so those settle too.
4. **Agent** (only for the rest): the agent reads the invoice Markdown and writes the printed values
   ([`prompts/EXTRACTION.md`](prompts/EXTRACTION.md)). Run one small, fast model per invoice, in parallel.
   Scanned invoices are read from page images and always end as REVIEW.
5. **Decide**: code only, against the database.

| Check | Fails as |
| --- | --- |
| hours x rate = line amount on every line | NO |
| invoice total = sum of the hours lines (extra charges, discounts) | NO |
| invoice period = timesheet month | NO |
| a work order exists for the consultant | NO |
| a work order covers the whole invoiced period (renewals supported) | NO |
| billed rate = work order rate | NO |
| billed hours = timesheet hours for the consultant's ATT ID (several lines for one person are added together) | NO |
| invoice amount = timesheet hours x work order rate | NO |
| same vendor + invoice number already approved (`--record`) | NO |
| overtime in the timesheet, rate not hourly, the name matches two people | REVIEW |

Warnings that don't change the answer: work order status not "Verified", work order record
not yet confirmed by a person, weekdays with no recorded time.

## Setup

```bash
python -m venv .venv && .venv/bin/pip install -r requirements.txt
```

## Commands

```bash
DB="--db data/recon.db"

# reference data: when a new month's timesheet or a new / renewed work order arrives
python -m invoice_check $DB ts-load "data/CMPM Report_1001-103126_Final.xlsx"
python -m invoice_check $DB wo-ingest data/work_orders
python -m invoice_check $DB wo-list
python -m invoice_check $DB wo-set 3 --confirm          # after a person checks the record
python -m invoice_check $DB wo-set 4 --attuid ab1234    # link by hand (e.g. two people with one name)

# monthly run
python -m invoice_check $DB fast data/invoices --work work/2026-10 --month 2026-10
#   agent: for each entry in work/2026-10/agent_queue.json, write work/2026-10/extracted/<name>.json
python -m invoice_check $DB decide --work work/2026-10 --month 2026-10 [--record]

# lookups (add --json for machine-readable output)
python -m invoice_check $DB who "Name On Invoice" --vendor "Vendor"
python -m invoice_check $DB hours ab1234 --month 2026-10
python -m invoice_check $DB expected ab1234 --month 2026-10
```

`decide` exits 0 only when every invoice is GO, 1 when any is NO or REVIEW, and 2 on a data
error such as a timesheet month that hasn't been loaded. `--record` saves the decisions, so an
approved invoice can't be approved again.

## Database

| Table / view | Holds |
| --- | --- |
| `work_orders` | consultant, other names, vendor, rate, start / end date, status, source file, ATT ID link, confirmed flag |
| `timesheet_loads` | one row per loaded month: file, period, row counts |
| `timesheet_days` | every daily row of each month's report (rows dated outside the month kept, flagged, not counted) |
| `monthly_hours` (view) | regular, overtime and total hours and days recorded per ATT ID per month |
| `processed_invoices` | decisions saved with `--record` (the duplicate check) |

## Data

Real invoices, work orders, timesheets and the database stay in `data/` and `work/`, which are
git-ignored. Tests use invented data only:

```bash
pip install -r requirements-dev.txt
python -m pytest            # 55 tests, about 3 s
```

`vendor/pulkit/` is the extractor from Pulkit's invoice-reconciliation skill, copied unchanged
apart from example values in its docstrings.
