"""Blueprint: RTM monthly reporting — HTML dashboard and CSV exports."""

import csv
import io
from datetime import date, datetime
from zoneinfo import ZoneInfo

from flask import Blueprint, Response, flash, render_template, request

import rtm_db
import visual_client
from visual_client import VisualError

reports_bp = Blueprint("rtm_reports", __name__, url_prefix="/rtm")

LOCAL_TZ = ZoneInfo("America/Denver")


def _parse_month(raw):
    """Parse a ?month=YYYY-MM value; fall back to the current month in America/Denver."""
    if raw:
        try:
            year_s, month_s = raw.strip().split("-")
            year, month = int(year_s), int(month_s)
            if 1 <= month <= 12 and 2000 <= year <= 2100:
                return year, month
        except (ValueError, AttributeError):
            pass
    today = datetime.now(LOCAL_TZ).date()
    return today.year, today.month


def _month_bounds(year, month):
    start = date(year, month, 1)
    next_start = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
    return start, next_start


def _shift_month(year, month, delta):
    idx = year * 12 + (month - 1) + delta
    return idx // 12, idx % 12 + 1


def _row_count(row):
    """Tolerate either 'rtms' or 'count' as the count key on a monthly_report row."""
    for key in ("rtms", "count", "rtm_count"):
        if key in row and row[key] is not None:
            return row[key]
    return 0


def _build_rate_rows(rtm_counts, shipped):
    """Join RTM counts to shipped units on (model, caliber); compute rate %."""
    shipped_by_key = {}
    for s in shipped:
        shipped_by_key[(s.get("model"), s.get("caliber"))] = s.get("units_shipped") or 0

    rows = []
    seen = set()
    for r in rtm_counts:
        key = (r.get("model"), r.get("caliber"))
        seen.add(key)
        units = shipped_by_key.get(key, 0)
        count = _row_count(r)
        rate = round(count / units * 100, 2) if units else None
        rows.append({"model": key[0], "caliber": key[1], "rtms": count,
                     "units_shipped": units, "rate_pct": rate})
    for key, units in shipped_by_key.items():
        if key not in seen:
            rows.append({"model": key[0], "caliber": key[1], "rtms": 0,
                         "units_shipped": units,
                         "rate_pct": 0.0 if units else None})
    rows.sort(key=lambda r: (-(r["rate_pct"] or 0), str(r["model"] or ""), str(r["caliber"] or "")))
    return rows


def _gather(year, month):
    """Assemble everything both the HTML page and the CSVs need."""
    report = rtm_db.monthly_report(year, month)
    start, next_start = _month_bounds(year, month)

    rates_unavailable = False
    shipped = []
    try:
        shipped = visual_client.shipped_units(start, next_start)
    except VisualError:
        rates_unavailable = True

    # Add cumulative % to the root-cause pareto rows (rtm_db returns raw counts).
    pareto = report.get("root_cause_pareto") or []
    total = sum(_row_count(r) for r in pareto)
    running = 0
    for r in pareto:
        running += _row_count(r)
        r["cumulative_pct"] = round(running / total * 100, 1) if total else 0.0

    rate_rows = _build_rate_rows(report.get("rtm_counts") or [], shipped)
    return report, rate_rows, rates_unavailable


@reports_bp.route("/reports")
def reports():
    year, month = _parse_month(request.args.get("month"))
    report, rate_rows, rates_unavailable = _gather(year, month)
    if rates_unavailable:
        flash("VISUAL is unreachable — shipped-unit denominators and RTM rates are unavailable for now.")

    prev_y, prev_m = _shift_month(year, month, -1)
    next_y, next_m = _shift_month(year, month, +1)
    return render_template(
        "rtm/reports.html",
        report=report,
        rate_rows=rate_rows,
        rates_unavailable=rates_unavailable,
        month_value=f"{year:04d}-{month:02d}",
        month_label=date(year, month, 1).strftime("%B %Y"),
        prev_month=f"{prev_y:04d}-{prev_m:02d}",
        next_month=f"{next_y:04d}-{next_m:02d}",
    )


@reports_bp.route("/reports.csv")
def reports_csv():
    year, month = _parse_month(request.args.get("month"))
    which = (request.args.get("report") or "rate").strip().lower()
    if which not in ("rate", "pareto", "detail"):
        which = "rate"

    report, rate_rows, rates_unavailable = _gather(year, month)

    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")

    if which == "rate":
        writer.writerow(["model", "caliber", "rtms", "units_shipped", "rate_pct"])
        for r in rate_rows:
            rate = "" if r["rate_pct"] is None else r["rate_pct"]
            if rates_unavailable:
                rate = "unavailable"
            writer.writerow([r["model"], r["caliber"], r["rtms"],
                             "" if rates_unavailable else r["units_shipped"], rate])
    elif which == "pareto":
        writer.writerow(["root_cause", "count", "cumulative_pct"])
        for r in report.get("root_cause_pareto") or []:
            writer.writerow([r.get("label"), _row_count(r),
                             r.get("cumulative_pct", r.get("cum_pct", ""))])
    else:  # detail: summary stats, splits, repeat returns in one flat file
        writer.writerow(["section", "field", "value"])
        summary = [
            ("received", report.get("received_count")),
            ("closed", report.get("closed_count")),
            ("total_cost", report.get("total_cost")),
            ("avg_cost", report.get("avg_total_cost")),
            ("avg_touch_hours", report.get("avg_touch_hours")),
            ("avg_calendar_days", report.get("avg_calendar_days")),
        ]
        for field, value in summary:
            writer.writerow(["summary", field, value])
        for r in report.get("responsibility_split") or []:
            writer.writerow(["responsibility", r.get("label"), _row_count(r)])
        for r in report.get("resolution_split") or []:
            writer.writerow(["resolution", r.get("label"), _row_count(r)])
        for r in report.get("repeat_returns") or []:
            writer.writerow(["repeat_return", r.get("rtm_number"), r.get("serial_no")])

    month_tag = f"{year:04d}-{month:02d}"
    return Response(
        buf.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition":
                 f"attachment; filename=rtm_report_{month_tag}_{which}.csv"},
    )
