---
name: invoice-payment-check
description: Check a month's vendor invoices against the ATT timesheet and work orders, and give each one a verdict (MATCH = go ahead with payment, MISMATCH = do not pay, with reasons, REVIEW_REQUIRED = a person must check it). Use when given new vendor invoices to verify before payment, or asked about a consultant's hours, rate or work order.
---

# Invoice payment check

Code reads and decides; you only read the invoices code couldn't settle, and you relay
the report. Never compute totals, compare against the timesheet, decide payment
yourself, or edit an extraction to make an invoice pass.

Run every command from the project folder with its environment's Python, for example
`.venv/bin/python -m invoice_check --db data/recon.db ...`. Below, `IC` stands for that
command prefix.

If a folder watcher is already running (`python -m invoice_check watch ROOT`), don't run the
steps below for files in `ROOT/inbox`: the watcher handles them, and its reports are in
`ROOT/reports/`. To start one for a user, see "Watch a folder" in README.md.

## 0. Reference data (only when new data arrives)

- New month's ATT Time Report: `IC ts-load REPORT.xlsx`
- New or renewed work orders: `IC wo-ingest WO_DIR_OR_PDFS`. Then show the user `IC wo-list`
  and ask them to confirm new records (`IC wo-set ID --confirm`).

## 1. Rules first (about a second)

```bash
IC fast INVOICES_DIR --work W --month YYYY-MM
```

This prints `tier1` for each invoice it settled and `AGENT` for each one it couldn't, with
the reason. The queue is in `W/agent_queue.json`.

## 2. Agent only for the queue

If the queue is empty, go to step 3. Otherwise, start one agent per queued invoice, all at
once, on a small fast model. Give each one:

- the rules in `prompts/EXTRACTION.md`
- the invoice's `markdown` path from the queue
- the output path `W/extracted/<pdf name without .pdf>.json`
- a limit: read and write files only

For a queued invoice with `has_text: false`, first run `IC render PDF --out W/images` and
give the agent the image paths instead.

## 3. Decide (under a second)

```bash
IC decide --work W --month YYYY-MM
```

Add `--record` only when the user confirms this is the real payment run.

If the report says a value "does not appear on the invoice", that invoice's extraction was
wrong. Re-run that one agent once. If the value really is unclear, leave it: REVIEW is the
right outcome.

## 4. Report

Send `W/report.xlsx` (Results, Summary and Details sheets; each invoice has Pay? Yes/No and a
short message) and `W/report.md`. Reply in a few lines:
- how many invoices are MATCH, MISMATCH and REVIEW_REQUIRED, with amounts
- each invoice's message, as the report writes it

For REVIEW_REQUIRED, say what a person has to supply, for example a renewed work order
covering the period or a readable copy of a scanned invoice. Don't guess the outcome.

## Questions about people, hours and rates

Use the lookups. Don't open the timesheet or work-order files.

```bash
IC who "Name" --vendor "Vendor" --json
IC hours ATTUID --month YYYY-MM --json
IC expected ATTUID --month YYYY-MM --json
```
