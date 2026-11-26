"""Folder watcher: drop files in, get a payment report out.

Layout under the watched ROOT (created on start):

    ROOT/inbox/invoices/      drop invoice PDFs here
    ROOT/inbox/timesheets/    drop the monthly ATT Time Report (.xlsx) here
    ROOT/inbox/work_orders/   drop new or renewed work order PDFs here
    ROOT/runs/<run id>/       one folder per invoice batch (Markdown, extractions, report.*)
    ROOT/reports/             a copy of every batch's Excel report: <run id>_<month>.xlsx
    ROOT/processed/<kind>/<run id>/   originals are moved here once handled
    ROOT/watcher.log

What happens when files arrive:
  1. A file counts as arrived once its size and time stamp haven't changed for SETTLE seconds
     (so a file still being copied is never read half-written). Temporary files are ignored.
  2. Timesheets are loaded into the database first, then work orders, then invoices, so a batch
     is always checked against the newest reference data.
  3. Invoices are handled as one batch once no invoice has arrived for SETTLE seconds:
     rules pass -> the agent command for each queued invoice (in parallel) -> decide -> report.
  4. If the agent command fails or isn't set up, that invoice is reported as not checked.
     Nothing is ever guessed.

The watcher polls the folders (default every 5 s) instead of using OS file events, so it behaves
the same on Windows, macOS, Linux and network drives, and needs no extra package.
"""

from __future__ import annotations

import contextlib
import io
import json
import logging
import shlex
import shutil
import sqlite3
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

PROJECT = Path(__file__).resolve().parent.parent
EXTRACTION_RULES = PROJECT / "prompts" / "EXTRACTION.md"

# Claude Code, headless, small fast model, may only read and write files
DEFAULT_AGENT_CMD = "claude -p --model haiku --add-dir {work} --allowedTools Read,Write -- {prompt}"

KINDS = {"timesheets": (".xlsx",), "work_orders": (".pdf",), "invoices": (".pdf",)}
IGNORE_PREFIX = ("~$", ".", "._")
IGNORE_SUFFIX = (".tmp", ".part", ".crdownload", ".download", ".partial")

log = logging.getLogger("invoice_check.watch")


@dataclass
class Seen:
    size: int
    mtime: float
    stable_since: float


@dataclass
class WatchConfig:
    root: Path
    db: Path
    interval: float = 5.0
    settle: float = 10.0
    month: Optional[str] = None                 # default: newest timesheet month in the database
    agent_cmd: Optional[str] = DEFAULT_AGENT_CMD  # None = no agent; queued invoices are reported unchecked
    agent_workers: int = 4
    agent_timeout: float = 600.0
    record: bool = False
    _seen: dict = field(default_factory=dict)
    _warned: set = field(default_factory=set)


# ------------------------------------------------------------------ folders --
def ensure_layout(root: Path) -> None:
    for kind in KINDS:
        (root / "inbox" / kind).mkdir(parents=True, exist_ok=True)
        (root / "processed" / kind).mkdir(parents=True, exist_ok=True)
    (root / "runs").mkdir(exist_ok=True)
    (root / "reports").mkdir(exist_ok=True)


def _wanted(p: Path, kind: str) -> bool:
    n = p.name.lower()
    return (p.is_file() and not n.startswith(IGNORE_PREFIX) and not n.endswith(IGNORE_SUFFIX)
            and p.suffix.lower() in KINDS[kind])


def ready_files(cfg: WatchConfig, kind: str, now: float) -> tuple[list[Path], bool]:
    """(files that have stopped changing, whether any file of this kind is still arriving).

    A file is ready when it was last written or added to the folder at least SETTLE seconds ago
    and hasn't changed size or time stamp since the previous scan. Using the time it was added
    (not just its modification time) matters because copying often keeps the original date.
    """
    ready, busy = [], False
    for p in sorted((cfg.root / "inbox" / kind).iterdir()):
        if not _wanted(p, kind):
            continue
        try:
            st = p.stat()
        except FileNotFoundError:
            continue
        prev = cfg._seen.get(p)
        changed = prev is not None and (prev.size, prev.mtime) != (st.st_size, st.st_mtime)
        if prev is None or changed:
            cfg._seen[p] = Seen(st.st_size, st.st_mtime, now)
        age = now - max(st.st_mtime, st.st_ctime)
        if not changed and age >= cfg.settle and st.st_size > 0 and _readable(p):
            ready.append(p)
        else:
            busy = True
    return ready, busy


def _readable(p: Path) -> bool:
    try:
        with open(p, "rb") as f:          # fails on Windows while another program still has it locked
            f.read(1)
        return True
    except OSError:
        return False


def _move(files: list[Path], dest: Path, cfg: WatchConfig) -> list[Path]:
    dest.mkdir(parents=True, exist_ok=True)
    out = []
    for f in files:
        target = dest / f.name
        if target.exists():
            target = dest / f"{f.stem}_{int(time.time())}{f.suffix}"
        shutil.move(str(f), target)
        cfg._seen.pop(f, None)
        out.append(target)
    return out


# --------------------------------------------------------------------- steps --
def _cli(*args) -> tuple[int, str]:
    """Run one invoice_check command in-process; returns (exit code, printed output)."""
    from .__main__ import main
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
        code = main([str(a) for a in args])
    return code, buf.getvalue()


def newest_month(db: Path) -> Optional[str]:
    if not db.exists():
        return None
    con = sqlite3.connect(db)
    try:
        con.execute("SELECT 1 FROM timesheet_loads LIMIT 1")
        row = con.execute("SELECT MAX(month) FROM timesheet_loads").fetchone()
        return row[0] if row else None
    except sqlite3.OperationalError:
        return None
    finally:
        con.close()


def agent_prompt(name: str, entry: dict, work: Path, images: list[Path]) -> str:
    out = work / "extracted" / (Path(name).stem + ".json")
    if images:
        source = "The PDF is scanned. View these page images with your Read tool: " + ", ".join(str(i) for i in images)
    else:
        source = f"Read the invoice (Markdown only, not the PDF): {entry['markdown']}"
    return (f"Copy values from one invoice into a JSON file. Do not judge or check anything. "
            f"Use only the Read and Write tools.\n"
            f"1. Read the rules: {EXTRACTION_RULES}\n"
            f"2. {source}\n"
            f"3. Write the JSON exactly as the rules say to: {out}\n"
            f"   with \"file\": \"{name}\".\n"
            f"Copy what is printed; never compute.")


def run_agents(queue: dict, work: Path, cfg: WatchConfig) -> dict[str, str]:
    """Run the agent command for every queued invoice, in parallel. Returns {name: outcome}."""
    from .convert import render_pages
    if not queue:
        return {}
    if not cfg.agent_cmd:
        return {n: "no agent configured" for n in queue}
    logs = work / "agent_logs"
    logs.mkdir(exist_ok=True)
    template = shlex.split(cfg.agent_cmd)

    def one(name: str) -> tuple[str, str]:
        entry = queue[name]
        images = [] if entry["has_text"] else render_pages(Path(entry["pdf"]), work / "images")
        prompt = agent_prompt(name, entry, work, images)
        argv = [a.replace("{prompt}", prompt).replace("{work}", str(work)).replace("{project}", str(PROJECT))
                for a in template]
        out_json = work / "extracted" / (Path(name).stem + ".json")
        started = time.time()
        try:
            proc = subprocess.run(argv, capture_output=True, text=True, timeout=cfg.agent_timeout, cwd=work)
            (logs / (Path(name).stem + ".log")).write_text(
                f"$ {' '.join(argv[:-1])} <prompt>\nexit {proc.returncode}\n\n{proc.stdout}\n{proc.stderr}",
                encoding="utf-8")
            ok = out_json.exists()
            return name, (f"extracted in {time.time() - started:.1f}s" if ok
                          else f"agent wrote no extraction (exit {proc.returncode})")
        except FileNotFoundError:
            return name, f"agent command not found: {argv[0]}"
        except subprocess.TimeoutExpired:
            return name, f"agent timed out after {cfg.agent_timeout:.0f}s"

    with ThreadPoolExecutor(max_workers=max(1, cfg.agent_workers)) as pool:
        return dict(pool.map(one, list(queue)))


def process_reference(cfg: WatchConfig, now: float) -> None:
    run_id = datetime.now().strftime("%Y%m%d-%H%M%S")
    ts, _ = ready_files(cfg, "timesheets", now)
    for f in ts:
        code, out = _cli("--db", cfg.db, "ts-load", f)
        if code == 0:
            log.info("timesheet loaded: %s | %s", f.name, out.strip().splitlines()[-1] if out.strip() else "")
            _move([f], cfg.root / "processed" / "timesheets" / run_id, cfg)
        elif f not in cfg._warned:
            cfg._warned.add(f)
            log.error("timesheet NOT loaded, left in the inbox: %s | %s", f.name, out.strip())
    wos, _ = ready_files(cfg, "work_orders", now)
    if wos:
        staging = cfg.root / "processed" / "work_orders" / run_id
        moved = _move(wos, staging, cfg)
        code, out = _cli("--db", cfg.db, "wo-ingest", *moved)
        added = sum(1 for line in out.splitlines() if line.startswith("added"))
        log.info("work orders: %d received, %d new | details in %s", len(moved), added, staging)
        for line in out.splitlines():
            if line.startswith(("added", "unreadable")):
                log.info("  %s", line)


def process_invoices(cfg: WatchConfig, now: float) -> Optional[Path]:
    ready, busy = ready_files(cfg, "invoices", now)
    if not ready or busy:
        return None                                   # wait until the whole batch has landed
    month = cfg.month or newest_month(cfg.db)
    if not month:
        key = ("no-month", tuple(ready))
        if key not in cfg._warned:
            cfg._warned.add(key)
            log.error("%d invoice(s) waiting: no timesheet loaded yet. Drop the ATT Time Report in "
                      "inbox/timesheets.", len(ready))
        return None

    run_id = datetime.now().strftime("%Y%m%d-%H%M%S")
    work = cfg.root / "runs" / run_id
    t0 = time.time()
    batch = _move(ready, work / "invoices", cfg)
    log.info("batch %s: %d invoice(s), checking against timesheet month %s", run_id, len(batch), month)

    code, out = _cli("--db", cfg.db, "fast", work / "invoices", "--work", work, "--month", month)
    if code != 0:
        log.error("batch %s: rules pass failed: %s", run_id, out.strip())
        return work
    queue = json.loads((work / "agent_queue.json").read_text(encoding="utf-8"))
    t1 = time.time()
    log.info("batch %s: %d settled by rules, %d sent to the agent (%.1fs)",
             run_id, len(batch) - len(queue), len(queue), t1 - t0)
    for name, outcome in run_agents(queue, work, cfg).items():
        log.info("  agent | %s | %s", name, outcome)
    t2 = time.time()

    args = ["--db", cfg.db, "decide", "--work", work, "--month", month]
    if cfg.record:
        args.append("--record")
    _, out = _cli(*args)
    report = cfg.root / "reports" / f"{run_id}_{month}.xlsx"
    if (work / "report.xlsx").exists():
        shutil.copy2(work / "report.xlsx", report)
    for line in out.splitlines():
        if line.split(" ", 1)[0] in ("MATCH", "MISMATCH", "REVIEW_REQUIRED", "ERROR"):
            log.info("  %s", " ".join(line.split()))
    log.info("batch %s done in %.1fs (agent %.1fs): report %s", run_id, time.time() - t0, t2 - t1, report)
    shutil.copytree(work / "invoices", cfg.root / "processed" / "invoices" / run_id, dirs_exist_ok=True)
    return work


# ---------------------------------------------------------------------- loop --
def scan_once(cfg: WatchConfig, now: Optional[float] = None) -> Optional[Path]:
    now = time.time() if now is None else now
    process_reference(cfg, now)
    return process_invoices(cfg, now)


def watch(cfg: WatchConfig, stop: Optional[threading.Event] = None, once: bool = False,
          on_batch: Optional[Callable[[Path], None]] = None) -> None:
    ensure_layout(cfg.root)
    log.info("watching %s (every %ss, files must be unchanged for %ss); database %s; agent: %s",
             cfg.root / "inbox", cfg.interval, cfg.settle, cfg.db, cfg.agent_cmd or "off")
    stop = stop or threading.Event()
    while not stop.is_set():
        try:
            batch = scan_once(cfg)
            if batch and on_batch:
                on_batch(batch)
        except Exception:                               # keep watching whatever happens to one batch
            log.exception("unexpected error; the watcher keeps running")
        if once:
            return
        stop.wait(cfg.interval)
