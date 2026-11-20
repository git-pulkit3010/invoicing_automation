"""Database layer: timesheet loading, work-order linking, and the lookup scripts."""

from datetime import date
from decimal import Decimal

import pytest

from fixtures import make_timesheet, make_work_order, weekdays
from invoice_check import queries, timesheet, workorders
from invoice_check.db import connect
from invoice_check.timesheet import TimesheetError

OCT = weekdays(2026, 10)          # 22 weekdays


@pytest.fixture
def con(tmp_path):
    c = connect(tmp_path / "recon.db")
    yield c
    c.close()


def people():
    return [
        {"attuid": "jl001", "first": "JORDAN", "last": "LEE", "days": OCT[:19]},                    # 152 h
        {"attuid": "mr002", "first": "MAYA LAKSHMI", "last": "RAO KRISHNAN", "days": OCT[:17]},     # 136 h
        {"attuid": "ak003", "first": "ALEX", "last": "KIM", "days": OCT, "ot": 2},                  # 176 + 44 OT
        {"attuid": "ak999", "first": "ALEX", "last": "KIM", "days": OCT[:10]},                      # same name
    ]


def load(con, tmp_path, **kw):
    p = make_timesheet(tmp_path / "ts.xlsx", 2026, 10, kw.pop("ppl", people()), **kw)
    return timesheet.store(con, p)


# ------------------------------------------------------------------ timesheet --
def test_store_and_read_back_match_the_workbook(con, tmp_path):
    r = load(con, tmp_path)
    assert (r["month"], r["contractors"], r["rows_excluded"]) == ("2026-10", 4, 0)
    from_db = timesheet.from_db(con, "2026-10")
    direct = timesheet.load_timesheet(tmp_path / "ts.xlsx")
    for attuid, c in direct.contractors.items():
        d = from_db.contractors[attuid]
        assert (d.regular, d.ot, d.days) == (c.regular, c.ot, c.days)
    assert from_db.contractors["jl001"].total == Decimal(152)
    assert from_db.contractors["ak003"].ot == Decimal(44)


def test_monthly_hours_view(con, tmp_path):
    load(con, tmp_path)
    row = con.execute("SELECT * FROM monthly_hours WHERE attuid='mr002'").fetchone()
    assert (row["total_hours"], row["days_recorded"]) == (136.0, 17)


def test_reloading_a_month_replaces_it(con, tmp_path):
    load(con, tmp_path)
    load(con, tmp_path, ppl=[{"attuid": "jl001", "first": "JORDAN", "last": "LEE", "days": OCT[:5]}])
    assert con.execute("SELECT COUNT(*) FROM timesheet_days").fetchone()[0] == 5
    assert con.execute("SELECT COUNT(*) FROM timesheet_loads").fetchone()[0] == 1


def test_rows_outside_the_month_are_kept_but_not_counted(con, tmp_path):
    stray = [2026, 9, "1", 30, None, None, "V1", "CompNova LLC", "s", 1, "Seg", "zz100", "ZOE", "PARK", "N", 8, 0, 40, 0]
    r = load(con, tmp_path, extra_rows=[stray])
    assert r["rows_excluded"] == 1
    ts = timesheet.from_db(con, "2026-10")
    assert ts.contractors["zz100"].total == 0 and ts.contractors["zz100"].excluded_rows == 1
    assert con.execute("SELECT in_period FROM timesheet_days WHERE attuid='zz100'").fetchone()[0] == 0


def test_missing_banner_is_refused(con, tmp_path):
    with pytest.raises(TimesheetError, match="refusing to guess"):
        load(con, tmp_path, banner=None)


def test_period_spanning_two_months_is_refused(con, tmp_path):
    with pytest.raises(TimesheetError, match="not a single month"):
        load(con, tmp_path, banner="HOURS: 10/15/2026 - 11/14/2026")


def test_unknown_month_is_an_error(con, tmp_path):
    load(con, tmp_path)
    with pytest.raises(TimesheetError, match="no timesheet loaded for 2026-11"):
        timesheet.from_db(con, "2026-11")


# ------------------------------------------------------------- work orders ---
def ingest(con, tmp_path, name, vendor, consultant, rate="90", start="01/01/26", end="12/31/26", **kw):
    p = make_work_order(tmp_path / name, vendor, consultant, rate, start, end, **kw)
    return workorders.ingest(con, p, timesheet.all_contractors(con))


def test_work_order_links_by_exact_name(con, tmp_path):
    load(con, tmp_path)
    action, wo = ingest(con, tmp_path, "wo1.pdf", "Northwind Systems LLC", "Jordan Lee")
    assert action == "added" and (wo["attuid"], wo["link_method"]) == ("jl001", "exact")
    assert (wo["rate"], wo["start"], wo["end"], wo["status"]) == (Decimal(90), date(2026, 1, 1), date(2026, 12, 31), "Verified")


def test_work_order_links_by_shorter_name_and_stores_alias(con, tmp_path):
    load(con, tmp_path)
    _, wo = ingest(con, tmp_path, "wo2.pdf", "Bluepeak Inc", "Maya Rao")
    assert (wo["attuid"], wo["link_method"]) == ("mr002", "subset")
    stored = workorders.load_all(con)[0]
    assert stored.aliases == ["MAYA LAKSHMI RAO KRISHNAN"]


def test_two_people_with_the_same_name_are_not_linked(con, tmp_path):
    load(con, tmp_path)
    _, wo = ingest(con, tmp_path, "wo3.pdf", "Acme LLC", "Alex Kim")
    assert wo["attuid"] is None


def test_same_file_is_not_ingested_twice(con, tmp_path):
    load(con, tmp_path)
    ingest(con, tmp_path, "wo1.pdf", "Northwind Systems LLC", "Jordan Lee")
    p = tmp_path / "wo1.pdf"
    assert workorders.ingest(con, p, timesheet.all_contractors(con))[0] == "unchanged"
    assert len(workorders.load_all(con)) == 1


def test_manual_link_confirm_and_alias(con, tmp_path):
    load(con, tmp_path)
    ingest(con, tmp_path, "wo3.pdf", "Acme LLC", "Alex Kim")
    wo_id = workorders.load_all(con)[0].id
    workorders.update(con, wo_id, attuid="ak003", confirmed=1, add_alias="Alexander Kim")
    w = workorders.load_all(con)[0]
    assert (w.attuid, w.link_method, w.confirmed, w.aliases) == ("ak003", "manual", True, ["Alexander Kim"])


# ----------------------------------------------------------------- lookups ---
@pytest.fixture
def loaded(con, tmp_path):
    load(con, tmp_path)
    ingest(con, tmp_path, "wo1.pdf", "Northwind Systems LLC", "Jordan Lee", rate="90")
    ingest(con, tmp_path, "wo2.pdf", "Bluepeak Inc", "Maya Rao", rate="85", end="03/31/26")    # expired
    ingest(con, tmp_path, "wo3.pdf", "Acme LLC", "Alex Kim", rate="100")
    ingest(con, tmp_path, "wo4.pdf", "Gamma Inc", "Alex Kim", rate="95")
    a, b = [w for w in workorders.load_all(con) if w.consultant_name == "Alex Kim"]
    workorders.update(con, a.id, attuid="ak003"); workorders.update(con, b.id, attuid="ak999")
    return con


def test_who_exact_alias_vendor_and_ambiguous(loaded):
    assert queries.who(loaded, "Jordan Lee")["attuid"] == "jl001"
    assert queries.who(loaded, "Maya Lakshmi Rao Krishnan")["attuid"] == "mr002"      # via alias
    amb = queries.who(loaded, "Alex Kim")
    assert amb["result"] == "ambiguous" and set(amb["candidates"]) == {"ak003", "ak999"}
    assert queries.who(loaded, "Alex Kim", vendor="Gamma Inc LLC")["attuid"] == "ak999"
    none = queries.who(loaded, "Nobody Here")
    assert none["result"] == "none"


def test_hours_lookup(loaded):
    h = queries.hours(loaded, "MR002", "2026-10")
    assert (h["total_hours"], h["days_recorded"], len(h["missing_weekdays"])) == (Decimal(136), 17, 5)
    assert queries.hours(loaded, "xx000", "2026-10")["found"] is False


def test_expected_lookup(loaded):
    ok = queries.expected(loaded, "jl001", "2026-10")
    assert (ok["expected_amount"], ok["result"]) == (Decimal(13680), "ok")
    expired = queries.expected(loaded, "mr002", "2026-10")
    assert expired["work_order_covers_month"] is False and "does not cover" in expired["result"]
    ot = queries.expected(loaded, "ak003", "2026-10")
    assert "overtime" in ot["result"] and ot["expected_amount"] == Decimal(220) * 100
    assert queries.expected(loaded, "zz000", "2026-10")["result"] == "no work order"
