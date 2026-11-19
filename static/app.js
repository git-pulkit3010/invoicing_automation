(function () {
  "use strict";

  const MONTHS = [
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December"
  ];

  const els = {
    contractor: document.getElementById("contractor"),
    month: document.getElementById("month"),
    year: document.getElementById("year"),
    compute: document.getElementById("compute"),
    error: document.getElementById("error"),
    nameHint: document.getElementById("name-hint"),
    periodHint: document.getElementById("period-hint"),
    placeholder: document.getElementById("placeholder"),
    body: document.getElementById("result-body")
  };

  let meta = null;

  function showError(msg) {
    els.error.textContent = msg;
    els.error.hidden = !msg;
  }

  function escapeHtml(value) {
    return String(value == null ? "" : value).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }

  function fillMonths() {
    els.month.innerHTML = "";
    MONTHS.forEach(function (name, i) {
      const opt = document.createElement("option");
      opt.value = String(i + 1);
      opt.textContent = name;
      els.month.appendChild(opt);
    });
  }

  function hasData(year, month) {
    return (meta.periods_with_data || []).some(function (p) {
      return p.year === year && p.month === month;
    });
  }

  function updatePeriodHint() {
    const y = parseInt(els.year.value, 10);
    const m = parseInt(els.month.value, 10);
    if (!y || !m) {
      els.periodHint.textContent = " ";
      return;
    }
    const label = MONTHS[m - 1] + " " + y;
    if (hasData(y, m)) {
      els.periodHint.textContent = "Data available for " + label + ".";
    } else {
      els.periodHint.textContent = "No time entries in the workbook for " + label + ".";
    }
  }

  function syncComputeButton() {
    els.compute.disabled = !(els.contractor.value && els.year.value && els.month.value);
  }

  function render(result) {
    const s = result.summary;
    const c = result.contractor;
    const p = result.period;

    document.getElementById("r-name").textContent = c.full_name;
    document.getElementById("r-meta").textContent =
      "ATTUID " + c.attuid + "  ·  " + p.label;

    document.getElementById("r-total").textContent = s.total_hours;
    document.getElementById("r-days").textContent = s.days_recorded;
    document.getElementById("r-ot").textContent = s.ot_hours + " hrs";
    document.getElementById("r-benchmark").textContent = s.benchmark_hours + " hrs";

    const diff = document.getElementById("r-diff");
    const diffBox = document.getElementById("r-diff-box");
    const diffValue = parseFloat(s.difference);
    diff.textContent =
      (diffValue > 0 ? "+" : "") + s.difference + " hrs";
    diffBox.classList.toggle("short", diffValue < 0);
    diffBox.classList.toggle("over", diffValue > 0);

    document.getElementById("r-export").href =
      "/api/export.csv?attuid=" + encodeURIComponent(c.attuid) +
      "&year=" + p.year + "&month=" + p.month;

    const weeks = document.getElementById("r-weeks");
    weeks.innerHTML = "";
    (result.weeks || []).forEach(function (w) {
      const tr = document.createElement("tr");
      tr.innerHTML =
        "<td>" + escapeHtml(w.label) + "</td>" +
        '<td class="num">' + escapeHtml(w.days_recorded) + "</td>" +
        '<td class="num strong">' + escapeHtml(w.hours) + "</td>" +
        '<td class="num muted">' + (w.workbook_hours == null ? "—" : escapeHtml(w.workbook_hours)) + "</td>";
      weeks.appendChild(tr);
    });
    document.getElementById("r-total-days").textContent = s.days_recorded;
    document.getElementById("r-total-hours").textContent = s.total_hours;

    const noticeBox = document.getElementById("r-notice-box");
    const noticeTitle = document.getElementById("r-notice-title");
    const noticeList = document.getElementById("r-notice-list");
    const missing = result.missing_weekdays || [];
    noticeList.innerHTML = "";
    if (!s.has_data) {
      noticeTitle.textContent = "No records for this period";
      const li = document.createElement("li");
      li.textContent = "The workbook holds no time entries for " + p.label + ".";
      noticeList.appendChild(li);
      noticeBox.hidden = false;
    } else if (s.aggregate_rows) {
      noticeTitle.textContent = "Stored as a monthly total";
      const li = document.createElement("li");
      li.textContent =
        "This period is recorded as a single monthly total in the workbook, so the " +
        "hours are exact but individual days cannot be verified.";
      noticeList.appendChild(li);
      noticeBox.hidden = false;
    } else if (missing.length) {
      noticeTitle.textContent = "Missing weekdays";
      missing.forEach(function (m) {
        const li = document.createElement("li");
        li.textContent = m.label;
        noticeList.appendChild(li);
      });
      noticeBox.hidden = false;
    } else {
      noticeBox.hidden = true;
    }

    const audits = document.getElementById("r-audits");
    audits.innerHTML = "";
    (result.audits || []).forEach(function (a) {
      const tr = document.createElement("tr");
      tr.innerHTML =
        '<td><span class="dot ' + escapeHtml(a.level) + '"></span>' + escapeHtml(a.check) + "</td>" +
        "<td>" + escapeHtml(a.finding) + "</td>";
      audits.appendChild(tr);
    });

    els.placeholder.hidden = true;
    els.body.hidden = false;
  }

  async function compute() {
    showError("");
    const attuid = els.contractor.value;
    const year = els.year.value;
    const month = els.month.value;
    if (!attuid || !year || !month) return;

    els.compute.disabled = true;
    els.compute.textContent = "Computing…";
    try {
      const res = await fetch(
        "/api/hours?attuid=" + encodeURIComponent(attuid) +
        "&year=" + encodeURIComponent(year) +
        "&month=" + encodeURIComponent(month)
      );
      const data = await res.json();
      if (!res.ok) {
        showError(data.error || "Something went wrong.");
        return;
      }
      render(data);
    } catch (err) {
      showError("Could not reach the server: " + err.message);
    } finally {
      els.compute.disabled = false;
      els.compute.textContent = "Compute hours";
      syncComputeButton();
    }
  }

  async function loadMeta() {
    const res = await fetch("/api/meta");
    meta = await res.json();

    els.contractor.innerHTML = "";
    const blank = document.createElement("option");
    blank.value = "";
    blank.textContent = "Select a contractor…";
    els.contractor.appendChild(blank);

    (meta.contractors || []).forEach(function (c) {
      const opt = document.createElement("option");
      opt.value = c.attuid;
      opt.textContent = c.name;
      opt.setAttribute("data-name", c.name);
      els.contractor.appendChild(opt);
    });

    fillMonths();

    els.year.innerHTML = "";
    (meta.years || []).forEach(function (y) {
      const opt = document.createElement("option");
      opt.value = String(y);
      opt.textContent = String(y);
      els.year.appendChild(opt);
    });

    // Default to the newest period that actually has data.
    const periods = meta.periods_with_data || [];
    if (periods.length) {
      els.month.value = String(periods[0].month);
      els.year.value = String(periods[0].year);
    }

    updatePeriodHint();
    syncComputeButton();
    await applyDeepLink();
  }

  // Support shareable links: ?attuid=hg5064&year=2026&month=10
  async function applyDeepLink() {
    const params = new URLSearchParams(window.location.search);
    const attuid = params.get("attuid");
    const name = params.get("name");
    const year = params.get("year");
    const month = params.get("month");

    if (year) {
      const opt = Array.prototype.find.call(
        els.year.options,
        function (o) { return o.value === year; }
      );
      if (opt) els.year.value = year;
    }
    if (month) {
      const opt = Array.prototype.find.call(
        els.month.options,
        function (o) { return o.value === month; }
      );
      if (opt) els.month.value = month;
    }
    if (attuid) {
      const opt = Array.prototype.find.call(
        els.contractor.options,
        function (o) { return o.value === attuid; }
      );
      if (opt) els.contractor.value = attuid;
    } else if (name) {
      const match = (meta.contractors || []).find(function (c) {
        return c.name.toUpperCase() === name.toUpperCase();
      });
      if (match) els.contractor.value = match.attuid;
    }

    updatePeriodHint();
    syncComputeButton();
    if (els.contractor.value) compute();
  }

  els.contractor.addEventListener("change", function () {
    const opt = els.contractor.options[els.contractor.selectedIndex];
    const attuid = els.contractor.value;
    const found = (meta.contractors || []).find(function (c) { return c.attuid === attuid; });
    els.nameHint.textContent = attuid ? "ATTUID " + attuid : " ";
    updatePeriodHint();
    syncComputeButton();
  });

  els.month.addEventListener("change", updatePeriodHint);
  els.year.addEventListener("change", updatePeriodHint);
  els.compute.addEventListener("click", compute);

  els.contractor.addEventListener("keydown", function (e) {
    if (e.key === "Enter") compute();
  });

  loadMeta().catch(function (err) {
    showError("Could not load contractor list: " + err.message);
  });
})();
