# Invoice extraction instructions (step 2, done by the agent)

You read ONE vendor invoice that has been converted to Markdown and write ONE
JSON file with the values printed on it. You do not judge whether the invoice
is correct. Code checks everything after you, including that every number you
write really appears on the invoice. A number you guess, round, compute or
"fix" will be caught and will send the invoice to a person, so copy values
exactly as printed.

## Output

Write `<invoice file name without .pdf>.json` into the `extracted/` folder:

```json
{
  "file": "Northwind_Systems_Jordan_Lee_1001-103126.pdf",
  "vendor": "Northwind Systems LLC",
  "invoice_number": "NW-4417",
  "invoice_date": "2026-11-12",
  "period_start": "2026-10-01",
  "period_end": "2026-10-31",
  "currency": "USD",
  "lines": [
    {"consultant": "Jordan Lee", "hours": "152", "rate": "90.00", "amount": "13680.00"}
  ],
  "subtotal": "13680.00",
  "total": "13680.00",
  "notes": ""
}
```

## Rules

1. **Copy, don't calculate.** `hours`, `rate`, `amount` and `total` are the numbers
   printed on the invoice, written as plain numbers in quotes (drop `$` and `,`,
   keep decimals as printed). Never multiply or add to fill a gap.
2. **One entry in `lines` per billed line** for a person's time. If the invoice
   splits a person's month over several lines, list each line. Do not merge them.
3. **`total` is the amount payable** (Total, Grand Total, Invoice Total, Balance
   Due). **`subtotal`** is the amount before tax or adjustments, if one is printed
   separately (Subtotal, Sub Total, Gross Total); otherwise `null`. The hours check
   uses the subtotal, so tax on the total doesn't cause a mismatch.
4. **Other charges are not hours lines.** Expenses, fees, tax and discounts are not
   entries in `lines`; mention them in `notes`. The checker compares the subtotal
   (or the total, if there is no subtotal) with the sum of `lines` and flags the
   difference.
5. **Ignore attached timesheets.** Pages after the invoice (daily hours tables,
   week grids) are supporting documents, not billed lines.
6. **Column order varies.** Use the column headers (Qty / Quantity / Hours,
   Rate / Unit Price, Amount) to decide which number is which, never position.
7. **Wrapped text.** A name or number split over two lines in the same cell
   ("Jor-" / "dan", "$13,68" / "0.00") is one value: "Jordan", "13680.00".
8. **Dates** as `YYYY-MM-DD`. The service period is the billed work period (e.g.
   "10/01/2026 - 10/31/2026", "Services for the month of October 2026" =
   2026-10-01 to 2026-10-31), not the invoice date or due date.
9. **`invoice_number`** exactly as printed (keep letters, dashes, leading zeros).
10. **Missing or unreadable value:** use `null` and say why in `notes`. Never invent one.
11. **Consultant name** as printed on the invoice line (or in the line description
    if the line names the service, e.g. "Services for Jordan Lee").

## Scanned invoices

If the invoice is scanned, you will be given rendered page images
(`python -m invoice_check render <pdf> --out <work>/images`); read those
instead of the Markdown. Fill the JSON the same way. These invoices always go to a person for
confirmation; your extraction saves them typing, it does not approve anything.
