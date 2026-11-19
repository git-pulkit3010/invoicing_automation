# invoicing_automation

Tools for the CompNova invoicing workflow:

* **pdf-to-markdown** - deterministic PDF text to Markdown converter.
* **Total Worked Hours** - Flask web app that computes total worked hours
  per contractor and month from the ATT time report.

---

# pdf-to-markdown

Deterministic PDF text to Markdown converter. It uses no AI or ML (PyMuPDF's optional layout model is bypassed), so the same PDF gives the same Markdown on any machine. Images are ignored. Typically well under a second per document.

Format-agnostic: every rule is about layout (font size, weight, alignment, gaps, drawn lines), not about a document type. Tested on invoices, bank statements, forms, CVs, two-column papers, reports and a 33-page font manual.

Handles headings, paragraphs, bold/italic, bullet and numbered lists, ruled tables, borderless tables, tables continued across pages, key/value rows, checkboxes, address blocks, code blocks, repeated headers/footers and page numbers, and two-column pages.

```bash
pip install -r requirements.txt
python pdf_to_markdown.py input.pdf                # writes input.md
python pdf_to_markdown.py input.pdf -o out.md --pages 1-5 --page-breaks
python pdf_to_markdown.py pdfs/ -o md/             # whole folder
```

Scanned (image-only) PDFs have no text layer and need OCR first.

---

## Total Worked Hours (Flask web app)

A small Flask web app that reads an ATT time-report Excel workbook and shows the
total worked hours for a selected contractor and month, exactly as described in
[count_hour_logic.md](count_hour_logic.md).

### What it does

* **Contractor dropdown** — first name and surname combined into one entry
  (e.g. `HARISH GIRIRAJU`), backed by the contractor's ATTUID.
* **Month + year dropdowns** — every month is selectable; the year list comes
  from the workbook, and the UI tells you which periods actually contain data.
* **Total worked hours** — `sum(Regular Hours by Day) + sum(OT Hours by Day)`
  over that person's rows for the selected period.
* **Verified weekly breakdown** — hours grouped by the workbook's own
  `Week Start Date` / `Week End Date` period weeks, cross-checked against the
  workbook's weekly-hour columns.
* **Audit panel** — duplicate entries, aggregate rows, weekend hours,
  daily-vs-weekly totals, overtime, correcting-hours flags and missing
  weekdays, plus the full-month benchmark (8 h × every Mon–Fri).
* **CSV export** of the computed result.
* **Shareable links** — `/?attuid=hg5064&year=2026&month=10` opens straight
  to a computed result (the name also works: `/?name=HARISH GIRIRAJU&…`).

### Setup

    python3 -m venv .venv
    .venv/bin/pip install -r requirements.txt

(Already done in this workspace — `.venv` exists with Flask and openpyxl.)

### Run

    .venv/bin/python app.py

Then open <http://127.0.0.1:5000>.

Useful flags:

    .venv/bin/python app.py --excel path/to/other_report.xlsx --port 8080 --debug

The workbook is auto-detected from the project folder when `--excel` is not
given; it can also be set with the `EXCEL_PATH` environment variable.

> The sample time report and vendor work orders are **not** committed - they
> contain named contractors, ATTUIDs and client documents. Drop your own
> `CMPM Report_*.xlsx` into the project folder, or point at it directly:
> `python app.py --excel path/to/report.xlsx`.

### Tests

    .venv/bin/python tests/test_hours_calculator.py

The test suite reproduces the worked example from `count_hour_logic.md`:
HARISH GIRIRAJU (hg5064), October 2026 → **160 hours**, 20 days recorded,
weekly split 16 / 40 / 24 / 40 / 40, missing weekdays Mon 12 Oct and Tue 13 Oct,
full-month benchmark 176 h.

There is also a browser-driven check that drives the real UI (dropdowns →
Compute hours → rendered table):

    .venv/bin/pip install playwright
    .venv/bin/python app.py --port 5055 &
    .venv/bin/python tests/ui_check.py --url http://127.0.0.1:5055

### Layout

| Path | Purpose |
|---|---|
| `app.py` | Flask routes, CSV export, CLI entry point |
| `hours_calculator.py` | Workbook parsing + the hours logic (no Flask) |
| `templates/index.html` | Page markup |
| `static/app.js` | Front-end logic (dropdowns, fetch, rendering) |
| `static/styles.css` | Styling |
| `tests/test_hours_calculator.py` | Checks against the logic document |
| `tests/ui_check.py` | Drives the UI in a headless browser |

### Notes on the source data

Three contractors in the October workbook have their whole month stored in a
single row (176 h or 80 h in `Regular Hours by Day` rather than 8 h per day).
The app still reports their hours correctly and flags the row under
**Aggregate rows**, but it deliberately does *not* list "missing weekdays" for
them — with no daily rows there is nothing to compare against, and listing
21 missing days for someone who logged a full 176 h month would be misleading.
