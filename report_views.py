"""Weekly repair report and CSV downloads, from the repair database."""

import csv
import io
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from flask import Blueprint, Response, render_template, request

import db
import repair_db

report_bp = Blueprint("report", __name__, url_prefix="/report")

MAX_RANGE_DAYS = 366
BOM = chr(0xFEFF)


def _today():
    return datetime.now(ZoneInfo(repair_db.REPORT_TZ)).date()


def _date_range():
    """?start=YYYY-MM-DD&end=YYYY-MM-DD, default the last 7 days (local)."""
    end = _parse(request.args.get("end")) or _today()
    start = _parse(request.args.get("start")) or end - timedelta(days=6)
    if start > end:
        start, end = end, start
    if (end - start).days > MAX_RANGE_DAYS:
        start = end - timedelta(days=MAX_RANGE_DAYS)
    return start, end


def _parse(raw):
    try:
        return date.fromisoformat(raw) if raw else None
    except ValueError:
        return None


@report_bp.route("/")
def weekly():
    start, end = _date_range()
    if not db.available():
        return render_template("portal/report.html", data=None, start=start, end=end), 503
    return render_template(
        "portal/report.html",
        data=repair_db.report(start, end),
        stages={s["key"]: s["label"] for s in repair_db.list_stages()},
        board_stages=repair_db.BOARD_STAGES,
        start=start,
        end=end,
        prev_start=start - timedelta(days=7),
        prev_end=end - timedelta(days=7),
        next_start=start + timedelta(days=7),
        next_end=end + timedelta(days=7),
    )


def _csv(result, filename):
    columns, rows = result
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=columns)
    writer.writeheader()
    for row in rows:
        writer.writerow({k: _cell(v) for k, v in row.items()})
    return Response(
        BOM + buf.getvalue(),  # so Excel reads the file as UTF-8
        mimetype="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def _cell(value):
    if isinstance(value, datetime):
        return value.astimezone(ZoneInfo(repair_db.REPORT_TZ)).strftime("%Y-%m-%d %H:%M")
    if isinstance(value, str) and value[:1] in ("=", "+", "-", "@"):
        return "'" + value  # keep Excel from running typed-in text as a formula
    return value


@report_bp.route("/rmas.csv")
def rmas_csv():
    if not db.available():
        return Response("Database not configured.", status=503)
    start, end = _date_range()
    return _csv(repair_db.export_rmas(start, end), f"rmas_{start}_{end}.csv")


@report_bp.route("/events.csv")
def events_csv():
    if not db.available():
        return Response("Database not configured.", status=503)
    start, end = _date_range()
    return _csv(repair_db.export_events(start, end), f"repair_activity_{start}_{end}.csv")
