"""Flask web UI for computing total worked hours from an ATT time report.

Run it with:

    python app.py

then open http://127.0.0.1:5000 in a browser.

Options:
    --excel PATH     use a specific time-report workbook
    --host HOST      bind address (default 127.0.0.1)
    --port PORT      port (default 5000)
    --debug          enable the Flask reloader
"""

from __future__ import annotations

import argparse
import calendar
import csv
import datetime as dt
import io
import os
from pathlib import Path
from typing import Optional

from flask import Flask, jsonify, render_template, request

from hours_calculator import TimeReportData, find_workbook, load_report

BASE_DIR = Path(__file__).resolve().parent


def _parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="CompNova worked-hours calculator")
    parser.add_argument(
        "--excel",
        default=os.environ.get("EXCEL_PATH"),
        help="Path to the ATT time report .xlsx file (default: auto-detect in the project folder)",
    )
    parser.add_argument("--host", default=os.environ.get("HOST", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", 5000)))
    parser.add_argument("--debug", action="store_true")
    return parser.parse_args(argv)


def create_app(report: TimeReportData) -> Flask:
    app = Flask(__name__)
    app.config["REPORT"] = report
    # Pick up template edits without a restart.
    app.config["TEMPLATES_AUTO_RELOAD"] = True

    @app.route("/")
    def index():
        return render_template(
            "index.html",
            source=report.source.name if report.source else "unknown",
            contractor_count=len(report.contractors),
            row_count=len(report.entries),
        )

    @app.route("/api/meta")
    def api_meta():
        return jsonify(
            {
                "contractors": [
                    {"attuid": c.attuid, "name": c.full_name}
                    for c in report.contractors
                ],
                "years": report.years,
                "periods_with_data": report.months_with_data,
                "source": report.source.name if report.source else None,
            }
        )

    @app.route("/favicon.ico")
    def favicon():
        return app.send_static_file("favicon.svg")

    @app.route("/api/hours")
    def api_hours():
        attuid = (request.args.get("attuid") or "").strip()
        name = (request.args.get("name") or "").strip()
        year = request.args.get("year", type=int)
        month = request.args.get("month", type=int)

        if not attuid and name:
            contractor = report.contractor_by_name(name)
            attuid = contractor.attuid if contractor else ""
        if not attuid:
            return jsonify({"error": "Select a contractor."}), 400
        if year is None or month is None:
            return jsonify({"error": "Select a month and year."}), 400
        if not (1 <= month <= 12):
            return jsonify({"error": "Month must be between 1 and 12."}), 400
        if report.contractor(attuid) is None:
            return jsonify({"error": f"Unknown contractor '{attuid}'."}), 404

        try:
            result = report.compute(attuid, year, month)
        except KeyError as exc:
            return jsonify({"error": str(exc)}), 404
        return jsonify(result.as_dict())

    @app.route("/api/export.csv")
    def api_export_csv():
        """Download the computed result for the selected contractor as CSV."""
        attuid = (request.args.get("attuid") or "").strip()
        year = request.args.get("year", type=int)
        month = request.args.get("month", type=int)
        if not attuid or year is None or month is None:
            return jsonify({"error": "Missing parameters."}), 400
        try:
            result = report.compute(attuid, year, month)
        except KeyError as exc:
            return jsonify({"error": str(exc)}), 404

        rows = []
        rows.append(("Contractor", result.contractor.full_name))
        rows.append(("ATTUID", result.contractor.attuid))
        rows.append(("Period", result.period_label))
        rows.append(("Total worked hours", f"{result.total_hours:g}"))
        rows.append(("Regular hours", f"{result.regular_hours:g}"))
        rows.append(("Overtime hours", f"{result.ot_hours:g}"))
        rows.append(("Days recorded", result.days_recorded))
        rows.append(("Full-month benchmark", f"{result.benchmark_hours:g}"))
        rows.append(("", ""))
        rows.append(("Week", "Days recorded", "Hours", "Workbook weekly total"))
        for w in result.weeks:
            wb = "" if w.workbook_weekly_hours is None else f"{w.workbook_weekly_hours:g}"
            rows.append((w.label, w.days_recorded, f"{w.total_hours:g}", wb))
        rows.append(("Total", result.days_recorded, f"{result.total_hours:g}", ""))
        if result.missing_weekdays:
            rows.append(("", ""))
            rows.append(
                ("Missing weekdays", "; ".join(d.isoformat() for d in result.missing_weekdays))
            )

        buffer = io.StringIO()
        writer = csv.writer(buffer)
        for row in rows:
            writer.writerow(row)
        payload = buffer.getvalue()
        filename = (
            f"{result.contractor.attuid}_{year}-{month:02d}_hours.csv"
        )
        from flask import Response

        return Response(
            payload,
            mimetype="text/csv",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    return app


def load_report_data(excel: Optional[str]) -> TimeReportData:
    explicit = Path(excel).expanduser() if excel else None
    path = find_workbook(BASE_DIR, explicit)
    if path is None:
        raise SystemExit(
            "No Excel workbook found. Place the ATT time report in the project folder "
            "or pass --excel /path/to/report.xlsx"
        )
    return load_report(path)


def main(argv=None) -> None:
    args = _parse_args(argv)
    report = load_report_data(args.excel)
    app = create_app(report)
    source = report.source.name if report.source else "?"
    print(f"Loaded {source}: {len(report.entries)} rows, {len(report.contractors)} contractors")
    print(f"Serving on http://{args.host}:{args.port}")
    app.run(host=args.host, port=args.port, debug=args.debug)


if __name__ == "__main__":
    main()
