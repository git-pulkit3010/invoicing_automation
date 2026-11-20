"""Combined pipeline: rule-based first pass (Pulkit's extractor) + verification gate + agent fallback.

Two levels:
  * a fake extractor forces specific misreads, to prove the gate catches each one;
  * the real bundled extractor runs through the CLI on generated documents, end to end.
Every name, vendor and figure is invented.
"""

import json
from pathlib import Path

import pytest

from fixtures import make_invoice, make_scan, make_timesheet, make_work_order, weekdays
from invoice_check import prefill as prefill_mod
from invoice_check.__main__ import main

OCT = weekdays(2026, 10)
PEOPLE = [
    {"attuid": "jl001", "first": "JORDAN", "last": "LEE", "days": OCT[:19]},          # 152 h
    {"attuid": "mr002", "first": "MAYA", "last": "RAO", "days": OCT[:17]},            # 136 h
    {"attuid": "pn003", "first": "PRIYA", "last": "NAIR", "days": OCT[:20]},          # 160 h
    {"attuid": "so004", "first": "SAM", "last": "ORTIZ", "days": OCT[:20]},           # 160 h
    {"attuid": "lf005", "first": "LENA", "last": "FISCHER", "days": OCT},             # 176 h
    {"attuid": "tb006", "first": "TOMAS", "last": "BERG", "days": OCT[:20]},          # 160 h
    {"attuid": "nw007", "first": "NOOR", "last": "WALI", "days": OCT[:20]},           # 160 h, no work order
]
WORK_ORDERS = [  # file, vendor, consultant, rate, start, end
    ("wo_jl.pdf", "Northwind Systems LLC", "Jordan Lee", "90", "01/01/26", "12/31/26"),
    ("wo_mr.pdf", "Bluepeak Inc", "Maya Rao", "85", "01/01/25", "03/31/26"),          # expired
    ("wo_pn.pdf", "Orion Data Corp", "Priya Nair", "88", "01/01/26", "12/31/26"),
    ("wo_so.pdf", "Delta Works LLC", "Sam Ortiz", "75", "01/01/26", "12/31/26"),
    ("wo_lf.pdf", "Vertex Labs Inc", "Lena Fischer", "95", "01/01/26", "12/31/26"),
    ("wo_tb.pdf", "Kestrel Partners LLC", "Tomas Berg", "70", "01/01/26", "12/31/26"),
]


def run(*args) -> int:
    return main([str(a) for a in args])


@pytest.fixture
def setup(tmp_path):
    """Database with October loaded and six work orders; returns (db, invoices dir, work dir)."""
    db, inv, work = tmp_path / "recon.db", tmp_path / "invoices", tmp_path / "work"
    inv.mkdir()
    wo_dir = tmp_path / "work_orders"; wo_dir.mkdir()
    make_timesheet(tmp_path / "ts.xlsx", 2026, 10, PEOPLE)
    for f, v, c, r, s, e in WORK_ORDERS:
        make_work_order(wo_dir / f, v, c, r, s, e)
    assert run("--db", db, "ts-load", tmp_path / "ts.xlsx") == 0
    assert run("--db", db, "wo-ingest", wo_dir) == 0
    return db, inv, work


def queue_of(work: Path) -> dict:
    return json.loads((work / "agent_queue.json").read_text())


def decisions(work: Path) -> dict:
    return {d["file"]: d for d in json.loads((work / "report.json").read_text())}


def agent_writes(work: Path, pdf: str, vendor: str, number: str, lines: list[tuple], total: str,
                 subtotal: str | None = None):
    """Stand-in for the agent: writes the extraction a careful reader would produce."""
    (work / "extracted").mkdir(exist_ok=True)
    (work / "extracted" / (Path(pdf).stem + ".json")).write_text(json.dumps({
        "file": pdf, "vendor": vendor, "invoice_number": number, "invoice_date": "2026-11-05",
        "period_start": "2026-10-01", "period_end": "2026-10-31", "currency": "USD",
        "lines": [dict(consultant=c, hours=h, rate=r, amount=a) for c, h, r, a in lines],
        "total": total, "subtotal": subtotal, "notes": ""}))


# ===================================================================== end to end
def test_clean_batch_is_settled_by_rules_alone(setup):
    db, inv, work = setup
    make_invoice(inv / "Northwind Systems_Jordan Lee_1001-103126.pdf", "Northwind Systems LLC", "NW-4417",
                 [("Jordan Lee", "152", "90.00", "$13,680.00")], "$13,680.00")
    make_invoice(inv / "Kestrel Partners_Tomas Berg_1001-103126.pdf", "Kestrel Partners LLC", "KP-0921",
                 [("Tomas Berg", "160", "70.00", "$11,200.00")], "$11,200.00")
    assert run("--db", db, "fast", inv, "--work", work, "--month", "2026-10") == 0
    assert queue_of(work) == {}                                      # no agent needed at all
    assert run("--db", db, "decide", "--work", work, "--month", "2026-10") == 0   # exit 0: all GO
    assert {d["decision"] for d in decisions(work).values()} == {"MATCH"}


def test_mixed_batch_routes_each_invoice_correctly(setup):
    db, inv, work = setup
    files = {
        "go": "Northwind Systems_Jordan Lee_1001-103126.pdf",
        "expired": "Bluepeak_Maya Rao_1001-103126.pdf",
        "fee": "Orion Data_Priya Nair_1001-103126.pdf",
        "hours": "Delta Works_Sam Ortiz_1001-103126.pdf",
        "swapped": "Vertex Labs_Lena Fischer_1001-103126.pdf",
        "no_wo": "Wali Consulting_Noor Wali_1001-103126.pdf",
        "scan": "Scanned Co_Tomas Berg_1001-103126.pdf",
    }
    make_invoice(inv / files["go"], "Northwind Systems LLC", "NW-4417",
                 [("Jordan Lee", "152", "90.00", "$13,680.00")], "$13,680.00")
    make_invoice(inv / files["expired"], "Bluepeak Inc", "BP-77",
                 [("Maya Rao", "136", "85.00", "$11,560.00")], "$11,560.00")
    make_invoice(inv / files["fee"], "Orion Data Corp", "OD-5521",
                 [("Priya Nair", "160", "88.00", "$14,080.00")], "$14,580.00",
                 extra=[("Administration fee", "$500.00")])
    make_invoice(inv / files["hours"], "Delta Works LLC", "DW-310",
                 [("Sam Ortiz", "168", "75.00", "$12,600.00")], "$12,600.00")
    make_invoice(inv / files["swapped"], "Vertex Labs Inc", "VX-2026-10",
                 [("Lena Fischer", "176", "95.00", "$16,720.00")], "$16,720.00",
                 columns=("Description", "Rate", "Quantity", "Amount"))
    make_invoice(inv / files["no_wo"], "Wali Consulting LLC", "WC-12",
                 [("Noor Wali", "160", "80.00", "$12,800.00")], "$12,800.00")
    make_scan(inv / files["scan"])

    assert run("--db", db, "fast", inv, "--work", work, "--month", "2026-10") == 0
    q = queue_of(work)
    # settled by rules: clean invoice, and an expired work order (a business fact, not a reading)
    assert files["go"] not in q and files["expired"] not in q
    # must go to the agent: the scan, and every result a misread could have caused
    for k in ("scan", "fee", "hours", "no_wo"):
        assert files[k] in q, k

    # the agent re-reads only the queued invoices (simulated with what is printed on them)
    truth = {
        files["fee"]: ("Orion Data Corp", "OD-5521", [("Priya Nair", "160", "88.00", "14080.00")], "14580.00"),
        files["hours"]: ("Delta Works LLC", "DW-310", [("Sam Ortiz", "168", "75.00", "12600.00")], "12600.00"),
        files["swapped"]: ("Vertex Labs Inc", "VX-2026-10", [("Lena Fischer", "176", "95.00", "16720.00")], "16720.00"),
        files["no_wo"]: ("Wali Consulting LLC", "WC-12", [("Noor Wali", "160", "80.00", "12800.00")], "12800.00"),
        files["scan"]: ("Scanned Co", "SC-1", [("Tomas Berg", "160", "70.00", "11200.00")], "11200.00"),
    }
    for f in q:
        agent_writes(work, f, *truth[f])

    assert run("--db", db, "decide", "--work", work, "--month", "2026-10") == 1
    d = decisions(work)
    assert d[files["go"]]["decision"] == "MATCH"
    assert d[files["swapped"]]["decision"] == "MATCH"                    # column order doesn't matter
    assert d[files["expired"]]["decision"] == "REVIEW_REQUIRED"
    assert any("expired on 2026-03-31" in r for r in d[files["expired"]]["reasons"])
    assert d[files["fee"]]["decision"] == "MISMATCH"
    assert any("sum of its hours lines" in r for r in d[files["fee"]]["reasons"])
    assert d[files["fee"]]["warnings"] == [] or all("subtotal" not in w for w in d[files["fee"]]["warnings"])
    assert d[files["hours"]]["decision"] == "MISMATCH"
    assert any("timesheet shows 160" in r for r in d[files["hours"]]["reasons"])
    assert d[files["no_wo"]]["decision"] == "REVIEW_REQUIRED"
    assert d[files["scan"]]["decision"] == "REVIEW_REQUIRED"


def test_report_is_identical_on_rerun(setup):
    db, inv, work = setup
    make_invoice(inv / "Northwind Systems_Jordan Lee_1001-103126.pdf", "Northwind Systems LLC", "NW-4417",
                 [("Jordan Lee", "152", "90.00", "$13,680.00")], "$13,680.00")
    run("--db", db, "fast", inv, "--work", work, "--month", "2026-10")
    run("--db", db, "decide", "--work", work, "--month", "2026-10")
    first = (work / "report.md").read_bytes(), (work / "report.csv").read_bytes()
    run("--db", db, "fast", inv, "--work", work, "--month", "2026-10")
    run("--db", db, "decide", "--work", work, "--month", "2026-10")
    assert ((work / "report.md").read_bytes(), (work / "report.csv").read_bytes()) == first


def test_recorded_approval_blocks_a_second_payment(setup):
    db, inv, work = setup
    make_invoice(inv / "Northwind Systems_Jordan Lee_1001-103126.pdf", "Northwind Systems LLC", "NW-4417",
                 [("Jordan Lee", "152", "90.00", "$13,680.00")], "$13,680.00")
    run("--db", db, "fast", inv, "--work", work, "--month", "2026-10")
    assert run("--db", db, "decide", "--work", work, "--month", "2026-10", "--record") == 0
    assert run("--db", db, "decide", "--work", work, "--month", "2026-10") == 1
    d = next(iter(decisions(work).values()))
    assert d["decision"] == "MISMATCH" and "already approved" in d["reasons"][0]


def test_decide_without_the_month_loaded_is_a_clear_error(setup, capsys):
    db, inv, work = setup
    make_invoice(inv / "a_b_c.pdf", "X", "1", [("Jordan Lee", "152", "90.00", "$13,680.00")], "$13,680.00")
    run("--db", db, "convert", inv, "--work", work)
    assert run("--db", db, "decide", "--work", work, "--month", "2026-11") == 2
    assert "no timesheet loaded for 2026-11" in capsys.readouterr().err


def test_missing_agent_extraction_is_reported_not_skipped(setup):
    db, inv, work = setup
    make_scan(inv / "Scanned Co_Tomas Berg_1001-103126.pdf")
    run("--db", db, "fast", inv, "--work", work, "--month", "2026-10")
    assert run("--db", db, "decide", "--work", work, "--month", "2026-10") == 1   # never 0 with gaps
    assert "no extraction file" in (work / "report.md").read_text()


# ================================================================ forced misreads
def fake_extractor(monkeypatch, outputs: dict):
    """Replace Pulkit's extractor with one returning the given raw outputs by file name."""
    def extract(path):
        out = outputs[Path(path).name]
        if isinstance(out, Exception):
            raise out
        return out
    monkeypatch.setattr(prefill_mod, "load_extractor", lambda root=None: extract)


def raw(vendor, number, lines, total, contractor=None, year=2026, month=10, warnings=()):
    return {"vendor_name": {"value": vendor}, "invoice_number": {"value": number}, "invoice_date": {"value": "11/05/2026"},
            "total": {"value": total}, "amount_due": {"value": None}, "subtotal": {"value": total},
            "contractor_name": {"value": contractor}, "period_year": year, "period_month": month,
            "currency": {"value": "USD"}, "warnings": list(warnings),
            "line_items": [{"description": d, "quantity": q, "unit_rate": r, "amount": a} for d, q, r, a in lines]}


GOOD = "Northwind Systems_Jordan Lee_1001-103126.pdf"


@pytest.fixture
def one_invoice(setup):
    db, inv, work = setup
    make_invoice(inv / GOOD, "Northwind Systems LLC", "NW-4417",
                 [("Jordan Lee", "152", "90.00", "$13,680.00")], "$13,680.00")
    return db, inv, work


@pytest.mark.parametrize("case,lines,total,expect_reason", [
    ("amount split by a wrapped cell", [("Jordan Lee", "152", "90.00", "13")], "13680.00", "does not appear"),
    ("hours misread", [("Jordan Lee", "125", "90.00", "13680.00")], "13680.00", "does not appear"),
    ("rate and hours swapped", [("Jordan Lee", "90.00", "152", "13680.00")], "13680.00", "not the work order rate"),
    ("total misread", [("Jordan Lee", "152", "90.00", "13680.00")], "1368.00", "does not appear"),
    ("header read as the name", [("Description Quantity Rate", "152", "90.00", "13680.00")], "13680.00", None),
])
def test_forced_misreads_never_settle_wrongly(one_invoice, monkeypatch, case, lines, total, expect_reason):
    db, inv, work = one_invoice
    fake_extractor(monkeypatch, {GOOD: raw("Northwind Systems LLC", "NW-4417", lines, total)})
    run("--db", db, "fast", inv, "--work", work, "--month", "2026-10")
    q = queue_of(work)
    if case == "header read as the name":
        # the filename still names the consultant, and every number checks out: safe to settle
        assert GOOD not in q
        ext = json.loads((work / "extracted" / (Path(GOOD).stem + ".json")).read_text())
        assert ext["lines"][0]["consultant"] == "Jordan Lee"
    else:
        assert GOOD in q, case
        assert any(expect_reason in w for w in q[GOOD]["why"]), q[GOOD]["why"]
        assert not (work / "extracted" / (Path(GOOD).stem + ".json")).exists()


def test_unresolvable_name_goes_to_the_agent(setup, monkeypatch):
    db, inv, work = setup
    name = "invoice-0042.pdf"                              # no consultant in the file name either
    make_invoice(inv / name, "Northwind Systems LLC", "NW-4417",
                 [("Jordan Lee", "152", "90.00", "$13,680.00")], "$13,680.00")
    fake_extractor(monkeypatch, {name: raw("Northwind Systems LLC", "NW-4417",
                                           [("Contact Address", "152", "90.00", "13680.00")], "13680.00")})
    run("--db", db, "fast", inv, "--work", work, "--month", "2026-10")
    assert any("no work order on record" in w for w in queue_of(work)[name]["why"])


def test_extractor_crash_goes_to_the_agent(one_invoice, monkeypatch):
    db, inv, work = one_invoice
    fake_extractor(monkeypatch, {GOOD: RuntimeError("layout not understood")})
    run("--db", db, "fast", inv, "--work", work, "--month", "2026-10")
    assert "layout not understood" in queue_of(work)[GOOD]["why"][0]


def test_currency_codes_and_missing_vendor_are_mapped(one_invoice, monkeypatch):
    db, inv, work = one_invoice
    fake_extractor(monkeypatch, {GOOD: raw(None, "NW-4417", [("Jordan Lee", "152", "90.00", "13680.00")],
                                           "13680.00 USD")})
    run("--db", db, "fast", inv, "--work", work, "--month", "2026-10")
    ext = json.loads((work / "extracted" / (Path(GOOD).stem + ".json")).read_text())
    assert (ext["total"], ext["vendor"]) == ("13680.00", "Northwind Systems")
    assert "vendor taken from the file name" in ext["notes"]
