"""Command line: python -m invoice_check <command> --db recon.db ...

Load data (deterministic, into the database)
  ts-load   XLSX                      load a month's ATT Time Report (replaces that month)
  ts-months                           list loaded months
  wo-ingest PDF_OR_DIR...             add work orders, linked to ATT IDs from loaded timesheets
  wo-list / wo-set ID [...]           view / confirm / correct work orders

Look up (deterministic, what the agent calls instead of reading files; add --json)
  who      NAME [--vendor V]          name on an invoice -> ATT ID + work orders
  hours    ATTUID --month YYYY-MM     timesheet hours, days recorded, missing weekdays
  expected ATTUID --month YYYY-MM     timesheet hours x work order rate

Monthly run
  convert  INVOICES_DIR --work W      PDFs -> Markdown + manifest (step 1)
  render   PDF --out DIR              page images for a scanned invoice
  decide   --work W --month YYYY-MM [--record]   verify extractions, GO / NO / REVIEW (step 3)

Exit codes: decide returns 0 only if every invoice is GO; 1 otherwise; 2 = usage or data error.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from decimal import Decimal
from pathlib import Path

from . import queries, report, timesheet, workorders
from .common import fmt_money, fmt_num
from .convert import convert_folder, render_pages
from .db import connect
from .timesheet import TimesheetError
from .verify import GO, decide, load_extraction, record


def _json(obj) -> str:
    def conv(o):
        if isinstance(o, Decimal):
            return str(o)
        if isinstance(o, date):
            return o.isoformat()
        raise TypeError(type(o))
    return json.dumps(obj, default=conv, indent=2)


# ------------------------------------------------------------------ loading --
def cmd_ts_load(a, con) -> int:
    r = timesheet.store(con, a.xlsx)
    print(f"loaded {r['month']}: {r['rows_loaded']} daily rows for {r['contractors']} contractors "
          f"({r['period_start']} to {r['period_end']}); {r['rows_excluded']} row(s) dated outside the month "
          f"kept but not counted")
    return 0


def cmd_ts_months(a, con) -> int:
    rows = queries.months(con)
    if a.json:
        print(_json(rows)); return 0
    for r in rows:
        print(f"{r['month']}  {r['period_start']} to {r['period_end']}  {r['rows_loaded']:>5} rows  "
              f"{r['source_file']}  (loaded {r['loaded_at']})")
    return 0


def cmd_wo_ingest(a, con) -> int:
    people = timesheet.all_contractors(con)
    if not people:
        print("note: no timesheet loaded yet, so work orders can't be linked to ATT IDs; run ts-load first",
              file=sys.stderr)
    paths = []
    for p in a.paths:
        paths += sorted(x for x in p.iterdir() if x.suffix.lower() == ".pdf") if p.is_dir() else [p]
    for p in paths:
        action, wo = workorders.ingest(con, p, people)
        detail = ""
        if wo:
            detail = (f"{wo.get('consultant_name')} | {wo.get('vendor')} | {wo.get('currency')} {wo.get('rate')} "
                      f"{wo.get('rate_basis')} | {wo.get('start')} to {wo.get('end')} | {wo.get('status')} | "
                      f"ATT ID {wo.get('attuid') or 'NOT LINKED'} ({wo.get('link_method') or '-'})")
        print(f"{action:10} {p.name}  {detail}")
    print("\nCheck these values against the PDFs, then confirm each record: wo-set ID --confirm")
    return 0


def cmd_wo_list(a, con) -> int:
    orders = workorders.load_all(con)
    if getattr(a, "json", False):
        print(_json([queries._wo_dict(w) for w in orders])); return 0
    print(f"{'ID':>3} {'ATT ID':8} {'CONSULTANT':24} {'VENDOR':30} {'RATE':>6} {'START':10} {'END':10} "
          f"{'STATUS':10} OK  ALIASES")
    for w in orders:
        print(f"{w.id:>3} {w.attuid or '-':8} {w.consultant_name[:24]:24} {(w.vendor or '-')[:30]:30} "
              f"{str(w.rate or '-'):>6} {str(w.start or '-'):10} {str(w.end or '-'):10} {w.status or '-':10} "
              f"{'yes' if w.confirmed else 'no ':3} {', '.join(w.aliases)}")
    return 0


def cmd_wo_set(a, con) -> int:
    workorders.update(con, a.id, attuid=a.attuid, vendor=a.vendor, rate=a.rate, start_date=a.start,
                      end_date=a.end, status=a.status, confirmed=1 if a.confirm else None, add_alias=a.alias)
    return cmd_wo_list(a, con)


# ------------------------------------------------------------------ lookups --
def cmd_who(a, con) -> int:
    r = queries.who(con, a.name, a.vendor)
    if a.json:
        print(_json(r)); return 0
    if r["result"] == "none":
        print(f"no work order for '{a.name}'")
        for t in r["timesheet_matches"]:
            print(f"  but the timesheet has {t['name']} ({t['attuid']})")
        return 0
    print(f"{r['result']}: {a.name} -> {r['attuid'] or ' / '.join(r['candidates'])}  (matched by {r['matched_by']})")
    for w in r["work_orders"]:
        print(f"  work order {w['work_order_id']}: {w['vendor']} | {w['currency']} {w['rate']} {w['rate_basis']} | "
              f"{w['start']} to {w['end']} | {w['status']} | {'confirmed' if w['confirmed'] else 'not confirmed'}")
    return 0


def cmd_hours(a, con) -> int:
    r = queries.hours(con, a.attuid, a.month)
    if a.json:
        print(_json(r)); return 0
    if not r["found"]:
        print(f"{r['attuid']}: no timesheet rows in {a.month}"); return 0
    print(f"{r['attuid']} {r['name']} {a.month}: {fmt_num(r['total_hours'])} hrs "
          f"(regular {fmt_num(r['regular_hours'])}, overtime {fmt_num(r['ot_hours'])}), "
          f"{r['days_recorded']} of {r['weekdays_in_month']} weekdays recorded")
    if r["missing_weekdays"]:
        print("  no time on: " + ", ".join(r["missing_weekdays"]))
    return 0


def cmd_expected(a, con) -> int:
    r = queries.expected(con, a.attuid, a.month)
    if a.json:
        print(_json(r)); return 0
    if r["expected_amount"] is None:
        print(f"{r['attuid']} {a.month}: {r['result']}"); return 0
    print(f"{r['attuid']} {a.month}: {fmt_num(r['total_hours'])} hrs x {fmt_money(r['rate'])} = "
          f"{fmt_money(r['expected_amount'])}  ({r['result']})")
    return 0


# -------------------------------------------------------------- monthly run --
def cmd_convert(a, con=None) -> int:
    manifest = convert_folder(a.invoices, a.work)
    for name, m in manifest.items():
        print(f"{'text ' if m['has_text'] else 'SCAN '} {name}")
    (a.work / "extracted").mkdir(exist_ok=True)
    print(f"\n{len(manifest)} invoice(s). Next: one JSON per invoice in {a.work / 'extracted'}")
    return 0


def cmd_render(a, con=None) -> int:
    for p in render_pages(a.pdf, a.out):
        print(p)
    return 0


def cmd_fast(a, con) -> int:
    """Tier 1: convert + rule-based extraction + gate. Writes extractions that verify and a queue
    of invoices that need the agent (tier 2)."""
    from .prefill import prefill
    ts = timesheet.from_db(con, a.month)
    orders = workorders.load_all(con)
    manifest = convert_folder(a.invoices, a.work)
    ext_dir = a.work / "extracted"
    ext_dir.mkdir(exist_ok=True)
    results = prefill(manifest, a.work, a.pulkit, con)
    queue = {}
    for name, m in manifest.items():
        ext = results.get(name)
        if ext is None:
            queue[name] = ["scanned PDF: read the page images"]
            continue
        if "error" in ext:
            queue[name] = [ext["error"]]
            continue
        md_text = Path(m["markdown"]).read_text(encoding="utf-8")
        d = decide(ext, md_text, m["has_text"], m["sha256"], ts, orders, None)
        reasons = d.untrusted + d.misread_risk
        if reasons:
            queue[name] = reasons
        else:
            (ext_dir / (Path(name).stem + ".json")).write_text(json.dumps(ext, indent=2), encoding="utf-8")
            print(f"tier1  {d.decision:15} {name}")
    for name, why in queue.items():
        print(f"AGENT  {name}  ({why[0]}{' +' + str(len(why) - 1) + ' more' if len(why) > 1 else ''})")
    (a.work / "agent_queue.json").write_text(json.dumps(
        {n: {"markdown": manifest[n]["markdown"], "pdf": manifest[n]["pdf"], "has_text": manifest[n]["has_text"],
             "why": why} for n, why in queue.items()}, indent=2), encoding="utf-8")
    print(f"\n{len(manifest) - len(queue)} settled by rules, {len(queue)} queued for the agent "
          f"({a.work / 'agent_queue.json'})")
    return 0


def cmd_decide(a, con) -> int:
    ts = timesheet.from_db(con, a.month)
    manifest_path = a.work / "manifest.json"
    if not manifest_path.exists():
        raise TimesheetError(f"{manifest_path} not found; run 'convert' first")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    orders = workorders.load_all(con)
    if not orders:
        raise TimesheetError("the work order table is empty; run 'wo-ingest' first")

    decisions, shas, errors = [], [], []
    for name, m in manifest.items():
        ext_path = a.work / "extracted" / (Path(name).stem + ".json")
        if not ext_path.exists():
            errors.append(f"{name}: no extraction file ({ext_path.name}); not checked")
            continue
        try:
            ext = load_extraction(ext_path)
        except (ValueError, json.JSONDecodeError) as e:
            errors.append(f"{name}: extraction file is not valid JSON ({e})")
            continue
        ext.setdefault("file", name)
        md_text = Path(m["markdown"]).read_text(encoding="utf-8")
        decisions.append(decide(ext, md_text, m["has_text"], m["sha256"], ts, orders, con))
        shas.append(m["sha256"])

    if a.record:
        for d, sha in zip(decisions, shas):
            record(con, d, sha)

    (a.work / "report.md").write_text(report.markdown(decisions, ts, errors), encoding="utf-8")
    (a.work / "report.csv").write_text(report.to_csv(decisions), encoding="utf-8")
    (a.work / "report.json").write_text(report.to_json(decisions), encoding="utf-8")
    xlsx = a.xlsx or (a.work / "report.xlsx")
    report.to_xlsx(decisions, ts, errors, xlsx)

    for d in decisions:
        print(f"{d.decision:15} {d.vendor[:28]:28} {d.invoice_number:16} {fmt_money(d.total):>12}  {d.summary}")
    for e in errors:
        print(f"ERROR  {e}")
    print(f"\nReport: {a.work / 'report.md'} (+ .csv, .json)\nExcel:  {xlsx}")
    return 0 if decisions and not errors and all(d.decision == GO for d in decisions) else 1


# ------------------------------------------------------------------- parser --
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="invoice_check", description="Check vendor invoices before payment.")
    p.add_argument("--db", type=Path, default=Path("data/recon.db"), help="database file (default data/recon.db)")
    sub = p.add_subparsers(dest="cmd", required=True)

    def add(name, fn, needs_db=True):
        s = sub.add_parser(name); s.set_defaults(fn=fn, needs_db=needs_db); return s

    s = add("ts-load", cmd_ts_load); s.add_argument("xlsx", type=Path)
    s = add("ts-months", cmd_ts_months); s.add_argument("--json", action="store_true")
    s = add("wo-ingest", cmd_wo_ingest); s.add_argument("paths", type=Path, nargs="+")
    s = add("wo-list", cmd_wo_list); s.add_argument("--json", action="store_true")
    s = add("wo-set", cmd_wo_set); s.add_argument("id", type=int)
    for opt in ("attuid", "alias", "vendor", "rate", "start", "end", "status"):
        s.add_argument(f"--{opt}")
    s.add_argument("--confirm", action="store_true")

    s = add("who", cmd_who); s.add_argument("name"); s.add_argument("--vendor"); s.add_argument("--json", action="store_true")
    for name, fn in (("hours", cmd_hours), ("expected", cmd_expected)):
        s = add(name, fn); s.add_argument("attuid"); s.add_argument("--month", required=True)
        s.add_argument("--json", action="store_true")

    s = add("convert", cmd_convert, needs_db=False); s.add_argument("invoices", type=Path)
    s.add_argument("--work", type=Path, required=True)
    s = add("render", cmd_render, needs_db=False); s.add_argument("pdf", type=Path)
    s.add_argument("--out", type=Path, required=True)
    s = add("fast", cmd_fast); s.add_argument("invoices", type=Path)
    s.add_argument("--work", type=Path, required=True); s.add_argument("--month", required=True)
    s.add_argument("--pulkit", type=Path, help="use another copy of Pulkit's extractor (folder containing invoicing/)")
    s = add("decide", cmd_decide); s.add_argument("--work", type=Path, required=True)
    s.add_argument("--month", required=True, help="timesheet month to check against, YYYY-MM")
    s.add_argument("--record", action="store_true", help="save decisions (enables the duplicate-approval check)")
    s.add_argument("--xlsx", type=Path, help="where to write the Excel results (default WORK/report.xlsx)")
    return p


def main(argv=None) -> int:
    a = build_parser().parse_args(argv)
    con = None
    try:
        if a.needs_db:
            a.db.parent.mkdir(parents=True, exist_ok=True)
            con = connect(a.db)
        return a.fn(a, con)
    except TimesheetError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    finally:
        if con is not None:
            con.close()


if __name__ == "__main__":
    sys.exit(main())
