"""Folder watcher, end to end, with invented documents and a stand-in for the agent command."""

import json
import os
import re
import shutil
import sys
import textwrap
import threading
import time
from pathlib import Path

import pytest
from openpyxl import load_workbook

from fixtures import make_invoice, make_scan, make_timesheet, make_work_order, weekdays
from invoice_check.watcher import WatchConfig, ensure_layout, scan_once, watch

OCT = weekdays(2026, 10)
PEOPLE = [{"attuid": "jl001", "first": "JORDAN", "last": "LEE", "days": OCT[:19]},      # 152 h
          {"attuid": "tb006", "first": "TOMAS", "last": "BERG", "days": OCT[:20]}]      # 160 h

# What a careful reader would copy from each invoice the rules can't settle
TRUTH = {
    "Scanned Co_Tomas Berg_1001-103126.pdf": {
        "vendor": "Kestrel Partners LLC", "invoice_number": "KP-0921", "invoice_date": "2026-11-05",
        "period_start": "2026-10-01", "period_end": "2026-10-31", "currency": "USD",
        "lines": [{"consultant": "Tomas Berg", "hours": "160", "rate": "70.00", "amount": "11200.00"}],
        "total": "11200.00", "subtotal": None, "notes": "scanned"},
}

FAKE_AGENT = textwrap.dedent('''
    """Stand-in for Claude Code: parses the prompt, writes the extraction a careful reader would."""
    import json, os, re, sys
    prompt = sys.argv[-1]
    out = re.search(r"Write the JSON exactly as the rules say to: (.+?\\.json)", prompt).group(1)
    name = re.search(r'"file": "(.+?)"', prompt).group(1)
    with open(os.environ["FAKE_AGENT_CALLS"], "a") as f:
        f.write(name + "\\n")
    truth = json.load(open(os.environ["FAKE_AGENT_TRUTH"]))
    if name in truth:
        json.dump({"file": name, **truth[name]}, open(out, "w"))
''')


@pytest.fixture
def root(tmp_path, monkeypatch):
    r = tmp_path / "watched"
    ensure_layout(r)
    agent = tmp_path / "fake_agent.py"
    agent.write_text(FAKE_AGENT)
    (tmp_path / "truth.json").write_text(json.dumps(TRUTH))
    monkeypatch.setenv("FAKE_AGENT_TRUTH", str(tmp_path / "truth.json"))
    monkeypatch.setenv("FAKE_AGENT_CALLS", str(tmp_path / "calls.txt"))
    return r


def cfg_for(root: Path, **kw) -> WatchConfig:
    agent = root.parent / "fake_agent.py"
    defaults = dict(root=root, db=root / "recon.db", interval=0.2, settle=0,
                    agent_cmd=f'"{sys.executable}" "{agent}" {{prompt}}', agent_workers=4, agent_timeout=60)
    defaults.update(kw)
    return WatchConfig(**defaults)


def drop_reference(root: Path, staging: Path):
    """Create files elsewhere, then move them in (how a real drop or download lands)."""
    staging.mkdir(exist_ok=True)
    make_timesheet(staging / "ATT Time Report Oct.xlsx", 2026, 10, PEOPLE)
    make_work_order(staging / "wo_jl.pdf", "Northwind Systems LLC", "Jordan Lee", "90", "01/01/26", "12/31/26")
    make_work_order(staging / "wo_tb.pdf", "Kestrel Partners LLC", "Tomas Berg", "70", "01/01/25", "03/31/26")
    shutil.move(str(staging / "ATT Time Report Oct.xlsx"), root / "inbox" / "timesheets")
    for f in ("wo_jl.pdf", "wo_tb.pdf"):
        shutil.move(str(staging / f), root / "inbox" / "work_orders")


def drop_invoices(root: Path, staging: Path):
    staging.mkdir(exist_ok=True)
    make_invoice(staging / "Northwind Systems_Jordan Lee_1001-103126.pdf", "Northwind Systems LLC", "NW-4417",
                 [("Jordan Lee", "152", "90.00", "$13,680.00")], "$13,680.00")
    make_scan(staging / "Scanned Co_Tomas Berg_1001-103126.pdf")
    for f in staging.glob("*.pdf"):
        shutil.move(str(f), root / "inbox" / "invoices")


def results(xlsx: Path) -> dict:
    ws = load_workbook(xlsx)["Results"]
    hdr = [c.value for c in ws[1]]
    return {r[hdr.index("Invoice #")]: dict(zip(hdr, r)) for r in ws.iter_rows(min_row=2, values_only=True)}


def calls(root: Path) -> list[str]:
    p = root.parent / "calls.txt"
    return p.read_text().splitlines() if p.exists() else []


# ----------------------------------------------------------------------- tests --
def test_drop_everything_and_get_a_report(root, tmp_path):
    drop_reference(root, tmp_path / "staging")
    drop_invoices(root, tmp_path / "staging")
    run = scan_once(cfg_for(root))
    assert run is not None
    reports = list((root / "reports").glob("*_2026-10.xlsx"))
    assert len(reports) == 1
    res = results(reports[0])
    assert (res["NW-4417"]["Verdict"], res["NW-4417"]["Pay?"]) == ("MATCH", "Yes")
    assert res["KP-0921"]["Verdict"] == "REVIEW_REQUIRED"                 # scanned, and work order expired
    # only the invoice the rules couldn't read went to the agent
    assert calls(root) == ["Scanned Co_Tomas Berg_1001-103126.pdf"]
    # inbox emptied; originals kept
    assert not any(list((root / "inbox" / k).iterdir()) for k in ("invoices", "timesheets", "work_orders"))
    assert len(list((root / "processed" / "invoices").rglob("*.pdf"))) == 2
    assert len(list((root / "processed" / "timesheets").rglob("*.xlsx"))) == 1
    assert len(list((root / "processed" / "work_orders").rglob("*.pdf"))) == 2


def test_a_second_drop_is_a_new_batch_and_nothing_runs_twice(root, tmp_path):
    drop_reference(root, tmp_path / "s1")
    drop_invoices(root, tmp_path / "s1")
    first = scan_once(cfg_for(root))
    assert scan_once(cfg_for(root)) is None                               # nothing new: no run
    time.sleep(1.1)                                                       # run ids are per second
    s2 = tmp_path / "s2"; s2.mkdir()
    make_invoice(s2 / "Northwind Systems_Jordan Lee_1101-113026.pdf", "Northwind Systems LLC", "NW-4418",
                 [("Jordan Lee", "152", "90.00", "$13,680.00")], "$13,680.00")
    shutil.move(str(next(s2.glob("*.pdf"))), root / "inbox" / "invoices")
    second = scan_once(cfg_for(root))
    assert second is not None and second != first
    assert set(results(second / "report.xlsx")) == {"NW-4418"}


def test_file_still_being_written_is_not_read(root, tmp_path):
    drop_reference(root, tmp_path / "staging")
    scan_once(cfg_for(root))                                              # load reference data
    make_invoice(root / "inbox" / "invoices" / "Northwind Systems_Jordan Lee_1001-103126.pdf",
                 "Northwind Systems LLC", "NW-4417", [("Jordan Lee", "152", "90.00", "$13,680.00")], "$13,680.00")
    cfg = cfg_for(root, settle=30)                                        # just written: too fresh
    assert scan_once(cfg) is None
    assert list((root / "inbox" / "invoices").glob("*.pdf"))              # still waiting in the inbox
    later = time.time() + 31                                              # once it has stayed unchanged
    assert scan_once(cfg, now=later) is not None


def test_temporary_and_other_files_are_ignored(root, tmp_path):
    drop_reference(root, tmp_path / "staging")
    scan_once(cfg_for(root))
    for name in ("~$draft.pdf", ".hidden.pdf", "download.pdf.crdownload", "notes.txt"):
        (root / "inbox" / "invoices" / name).write_text("x")
    assert scan_once(cfg_for(root)) is None
    assert len(list((root / "inbox" / "invoices").iterdir())) == 4        # untouched


def test_invoices_wait_until_a_timesheet_is_loaded(root, tmp_path, caplog):
    drop_invoices(root, tmp_path / "staging")
    assert scan_once(cfg_for(root)) is None
    assert "no timesheet loaded yet" in caplog.text
    assert len(list((root / "inbox" / "invoices").glob("*.pdf"))) == 2   # kept for later
    drop_reference(root, tmp_path / "staging2")
    assert scan_once(cfg_for(root)) is not None                           # now it runs


def test_failed_agent_is_reported_not_guessed(root, tmp_path):
    drop_reference(root, tmp_path / "staging")
    drop_invoices(root, tmp_path / "staging")
    run = scan_once(cfg_for(root, agent_cmd=f'"{sys.executable}" -c "import sys; sys.exit(3)" {{prompt}}'))
    res = results(run / "report.xlsx")
    assert res["NW-4417"]["Verdict"] == "MATCH"
    unread = res[None]                                                    # the scan: no invoice number read
    assert (unread["Verdict"], unread["Pay?"]) == ("REVIEW_REQUIRED", "No")
    assert "Scanned Co_Tomas Berg" in unread["Message"]
    summary = load_workbook(run / "report.xlsx")["Summary"]
    texts = [c.value for row in summary.iter_rows() for c in row if isinstance(c.value, str)]
    assert any("no extraction file" in t for t in texts)
    assert "exit 3" in next((run / "agent_logs").glob("*.log")).read_text()


def test_no_agent_mode(root, tmp_path):
    drop_reference(root, tmp_path / "staging")
    drop_invoices(root, tmp_path / "staging")
    run = scan_once(cfg_for(root, agent_cmd=None))
    assert set(results(run / "report.xlsx")) == {"NW-4417", None}
    assert calls(root) == []


def test_watch_loop_picks_up_files_as_they_arrive(root, tmp_path):
    batches, stop = [], threading.Event()
    t = threading.Thread(target=watch, args=(cfg_for(root, settle=0.5),),
                         kwargs={"stop": stop, "on_batch": batches.append}, daemon=True)
    t.start()
    try:
        drop_reference(root, tmp_path / "staging")
        drop_invoices(root, tmp_path / "staging")
        deadline = time.time() + 60
        while not batches and time.time() < deadline:
            time.sleep(0.2)
    finally:
        stop.set(); t.join(10)
    assert batches, "the watcher never processed the batch"
    assert (batches[0] / "report.xlsx").exists()
