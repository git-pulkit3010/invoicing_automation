# CompNova Invoice Reconciliation

Verifies that every vendor invoice equals **hours actually worked x the contracted
hourly rate**, by cross-checking three independent sources:

| Source | Document | What it provides |
|---|---|---|
| Timesheet | `CMPM Report_1001-103126_Final.xlsx` -> sheet `ATT Time Report` | hours worked per contractor |
| Work order | `work_orders/*.pdf` | the authorised hourly rate and engagement window |
| Invoice | `invoices/*.pdf` | the billed hours, rate and amount |

The headline assertion is:

```
hours (ATT Time Report) x rate (Work Order) == amount (Invoice)
```

---

## Quick start

The virtual environment is already in this folder.

```powershell
# reconcile every invoice
recon.cmd reconcile

# or directly
.\.venv\Scripts\python.exe -m recon reconcile
```

Rebuild the environment from scratch on another machine:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

Run the tests:

```powershell
.\.venv\Scripts\python.exe -m pytest tests -q
```

---

## CLI reference

```
recon reconcile     reconcile every invoice against the time report and work orders
recon timesheet     show recorded hours per contractor
recon workorders    list the parsed work orders
recon parse FILE    show how one document was parsed (debugging aid)
```

Useful flags for `reconcile`:

| Flag | Purpose |
|---|---|
| `--format table\|json\|csv` | output format (default `table`) |
| `--output FILE` | write the report to a file instead of stdout |
| `--only NAME` | reconcile only invoices matching a contractor or client |
| `--amount-tolerance` / `--rate-tolerance` / `--hours-tolerance` | comparison tolerances (default `0.01`) |
| `--include-out-of-period` | include time report rows outside the report period |
| `--allow-missing-work-order` | downgrade a missing work order from FAIL to WARN |

Exit codes: `0` at least one line reconciled with no failures or processing
errors, `1` at least one failed line, `2` load, parsing or usage error.
An empty batch or a PDF with no recognizable billable line is an error, so the
tool can be used as a CI gate. Other invoices continue processing after a PDF
error.

---

## How hours are calculated

This is the logic from `Total hours count logic.md`, encoded in
`recon/extract/timesheet.py`. One workbook row = one contractor working one day.

| Metric | Rule | Column |
|---|---|---|
| Regular hours | `SUM(Regular Hours by Day)` | **P** |
| Overtime | `SUM(OT Hours by Day)` | **Q** |
| Days recorded | count of daily rows (or distinct `Period Day`) | **D** |
| Weekly buckets | group by `Week Start Date` (Sun-Sat pay week) | **E** |
| Full-month benchmark | 22 October 2026 weekdays x 8 = **176** | - |

### Traps this logic has to avoid

1. **`Period Week Regular Hours` (column R) is a de-duplicated weekly total**
   repeated on every row of that week. Summing R across a contractor's rows
   massively overcounts. The tool only ever uses it as a cross-check, and
   verifies it against the recomputed weekly sum on every row.
2. **`Period Week` (column C) is not the calendar week.** It is an ATT sequence
   counter: one contractor has `C=1` for days 5, 6, 7 and `C=2` for days 8, 9
   inside the *same* week. Grouping by C produces wrong weekly buckets, so the
   tool groups by `Week Start Date` instead.
3. **Rows are not sorted by day.** One contractor has day 2 before day 1 inside
   their own block. The tool always sorts or counts by `Period Day`.
4. **Partial days exist.** 12 rows across 7 contractors carry 3, 4, 5 or 6 hours.
   `days x 8` is therefore *not* the total - the tool sums column P. LILA WARD
   worked 22 days for 167 hours.
5. **Three contractors have one aggregate row instead of daily rows**
   (KUMAR GAURAV ABROL 80h, SAI PRASAD JONNALA 176h, SWETHA KAKKIRENI 176h).
   The sum is still right, but "1 day recorded" is misleading, so they are flagged.
6. **One row is out of period.** SAI PRASAD JONNALA's only row is tagged
   `Period Month = 2026/9` inside a report headed `10/1/2026 - 10/31/2026`.
   It is excluded by default and reported; `--include-out-of-period` keeps it.
7. Clean across the whole file: zero overtime, zero weekend entries, zero
   duplicate (contractor, day) pairs, correcting-hours indicator `N` everywhere.

---

## How the documents are parsed

All PDFs here are flattened - there are no AcroForm fields, so every value has
to be recovered from glyph positions (`recon/pdf_layout.py`).

### Work orders

One CompNova form, so values are anchored to their label:

```
Name of Vendor : Edge Centric LLC
Name of Consultant : Chaitanya Motupalli
Billing Rate : USD 100 hourly
Client Name where services to be provided : AT&T
Auth. Start Date : 04/07/26
Est. End Date : 12/31/26
```

Page 2+ carries the document report, which supplies the verification status
(`Verified` / `Responded`).

### Invoices

Four different billing systems produce these invoices (TCPDF, Bill.com,
Stripe/Zoho and a QuickBooks-style template), so the parser uses a generic,
**self-validating** rule instead of four templates:

> a billable line is a row that carries three numbers where `quantity x rate == amount`

That identity holds on every invoice, and it makes the line item
self-identifying: the parser can *prove* it found the right row rather than
guessing which column is which. The contractor's name is then recovered by
matching the row text against the time report roster on **token-set equality**,
so `Chaitanya Motupalli` can never be resolved to `Chaitanya Kumar Nara`.

Number parsing is deliberately conservative and rejects `1.5%`, `10/01/2026`,
`57977W` and `TX:ITS` while accepting `$15,200.00`, `$ 17,248.00` (currency
symbol in a separate text run) and `83` split across runs.

---

## The checks

| Check | Meaning |
|---|---|
| `invoice_math` | the invoice is internally consistent (quantity x rate = amount) |
| `hours_match` | billed hours equal the hours in the ATT Time Report |
| `missing_weekdays` | *advisory* - days in the period with no recorded time |
| `work_order_found` | a work order exists for the invoiced consultant |
| `rate_match` | billed rate equals the contracted rate |
| `amount_match` | **hours x work order rate = invoice amount** |
| `period_in_work_order` | the invoiced period sits inside the authorised window |
| `work_order_status` | *advisory* - the work order was never marked `Verified` |
| `period_matches_report` | the invoice period equals the time report period |

Only a `FAIL` downgrades a reconciliation. `WARN` findings are advisories - a
gap in the timesheet is a business fact to raise, not a billing discrepancy.
If a work order has no consultant name, a unique vendor match can be used but
gets a `work_order_vendor_fallback` warning for human review. A work order
named for another consultant cannot authorize the invoice.

The CSV has a `record_type` column. `reconciliation` rows include advisory and
failure details; `error` rows identify PDFs that could not be reconciled; and
`unlinked_work_order` rows identify work orders with no matched invoice.

---

## Results for the October 2026 dataset

```
Client                        Contractor              Inv hrs   TS hrs      Rate       Amount     Expected  Status
Edge Centric                  Chaitanya Motupalli         152      152       100    15,200.00    15,200.00    PASS
Indotronix International Corp.Harish Giriraju             160      160        73    11,680.00    11,680.00    FAIL
Itek Info                     Srilasya Samudrala          176      176        83    14,608.00    14,608.00    PASS
Simple CTI Solutions          Vaibhav Bansal              176      176        98    17,248.00            -    FAIL
```

**All four invoices are arithmetically correct and all four billed hours match
the time report exactly.** The two failures are both authorization problems:

- **Harish Giriraju (Indotronix)** - his work order was authorised
  `2025-04-08` to `2026-03-31`, so it had expired **184 days** before the
  invoiced period began. The rate of $73/hour is correct and the 160 hours are
  correct, but there is no work order covering October 2026.
- **Vaibhav Bansal (Simple CTI Solutions)** - there is **no work order at all**
  for this consultant, so the $98/hour rate cannot be verified against
  anything. The invoice is internally consistent and the 176 hours match the
  time report, but the rate is unconfirmed.

Advisory findings:

- Harish has no recorded time on **Oct 12 and Oct 13** (16 hours).
- Chaitanya Motupalli has no recorded time on **Oct 23, 29 and 30** (24 hours).
- Three work orders (Edge Centric, ITEK INFO and this one) are still
  `Responded` rather than `Verified`.
- The work order for **Chaitanya Kumar Nara** (VMC Soft Technologies Inc) has
  no invoice in this batch and is reported as unlinked - it is deliberately
  *not* matched to the Chaitanya Motupalli invoice.

---

## Project layout

```
invoice_automation/
├── recon/
│   ├── cli.py              argparse entry point
│   ├── config.py           paths, tolerances and policy switches
│   ├── models.py           dataclasses for every domain object
│   ├── names.py            name normalisation and roster matching
│   ├── numeric.py          number / currency / date parsing
│   ├── pdf_layout.py       position-aware PDF text extraction
│   ├── reconcile.py        the engine and the checks
│   ├── report.py           table, JSON and CSV rendering
│   └── extract/
│       ├── timesheet.py    ATT Time Report reader
│       ├── invoice.py      invoice parser (all four templates)
│       └── workorder.py    work order parser
├── tests/test_recon.py     52 regression tests
├── requirements.txt
└── recon.cmd               launcher that uses the local venv
```

---

## Extending it

**A new invoice template.** Nothing needs to change. If the new template still
prints quantity, rate and amount on one row, the self-validating rule finds it.
Only add a template-specific rule if the three values are split across rows.

**A new check.** Add a `Check(...)` to `Reconciler._evaluate` in
`recon/reconcile.py` and give it a unique `key`.

**A different report layout.** The column indices live in
`recon/extract/timesheet.py` as `COL_*` constants; the header row is detected
by position (row 3) and the period by the `HOURS:` banner in row 2.

---

## Known limitations

- The time report is read as a single sheet named `ATT Time Report`.
- Only the first PDF page is used for invoice header fields; line items are
  collected from every page.
- Work orders are matched by consultant name; a consultant with two active
  work orders would need an explicit disambiguation rule.
- The `45` / `Net 45` figures in the payment terms block are correctly ignored
  - only the `Billing Rate` field is read as a rate.
