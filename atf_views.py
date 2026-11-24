"""ATF A&D log pages: what's in the building, acquisitions, dispositions, the
bound book, serial search, CSV downloads, and the compliance officer's edits.
"""

from datetime import date, timedelta

from flask import Blueprint, Response, abort, flash, redirect, render_template, request, url_for

import atf_log
import auth
import db
import repair_db
from report_views import _csv, _parse, _today

atf_bp = Blueprint("atf", __name__, url_prefix="/ad")

VIEWS = (
    ("on_hand", "In the building"),
    ("acquisitions", "Acquisitions"),
    ("dispositions", "Dispositions"),
    ("book", "Bound book"),
    ("review", "Needs review"),
)
MAX_RANGE_DAYS = 3660  # the book is kept for years; allow ten at a time
REASON_MAX = 300


def _range():
    """?start=&end=, default the last 30 days (local)."""
    end = _parse(request.args.get("end")) or _today()
    start = _parse(request.args.get("start")) or end - timedelta(days=29)
    if start > end:
        start, end = end, start
    if (end - start).days > MAX_RANGE_DAYS:
        start = end - timedelta(days=MAX_RANGE_DAYS)
    return start, end


def _rows(view, start, end, q):
    tz = repair_db.REPORT_TZ
    if q:
        return atf_log.search(q)
    if view == "acquisitions":
        return atf_log.acquisitions(start, end, tz)
    if view == "dispositions":
        return atf_log.dispositions(start, end, tz)
    if view == "book":
        return atf_log.bound_book(start, end, tz)
    if view == "review":
        return atf_log.needs_review()
    return atf_log.on_hand()


def _view():
    view = request.args.get("view", "on_hand")
    return view if view in dict(VIEWS) else "on_hand"


@atf_bp.route("/")
def log():
    view = _view()
    start, end = _range()
    q = " ".join((request.args.get("q") or "").split())[:60]
    today = _today()
    presets = [
        ("Last 30 days", today - timedelta(days=29), today),
        ("This month", today.replace(day=1), today),
        ("Last month", (today.replace(day=1) - timedelta(days=1)).replace(day=1), today.replace(day=1) - timedelta(days=1)),
        ("Year to date", date(today.year, 1, 1), today),
    ]
    ctx = dict(views=VIEWS, view=view, start=start, end=end, q=q, presets=presets, today=today)
    if not db.available():
        return render_template("portal/atf_log.html", rows=None, counts=None, **ctx), 503
    return render_template(
        "portal/atf_log.html",
        rows=_rows(view, start, end, q),
        counts=atf_log.counts(start, end, repair_db.REPORT_TZ),
        stages={s["key"]: s["label"] for s in repair_db.list_stages()},
        **ctx,
    )


@atf_bp.route("/log.csv")
def log_csv():
    if not db.available():
        return Response("Database not configured.", status=503)
    view = _view()
    start, end = _range()
    q = " ".join((request.args.get("q") or "").split())[:60]
    rows = _rows(view, start, end, q)
    if q:
        name = "ad_search.csv"
    elif view in ("on_hand", "review"):
        name = f"ad_{view}_{_today()}.csv"
    else:
        name = f"ad_{view}_{start}_{end}.csv"
    return _csv(atf_log.export_rows(rows), name)


@atf_bp.route("/<int:entry_id>")
def entry(entry_id):
    if not db.available():
        abort(503)
    row = atf_log.get_entry(entry_id)
    if row is None:
        abort(404)
    return render_template(
        "portal/atf_entry.html",
        e=row,
        changes=atf_log.changes_for(entry_id),
        labels=atf_log.FIELD_LABELS,
        can_edit=auth.is_admin(),
        tech=auth.current_tech(),
    )


@atf_bp.route("/<int:entry_id>", methods=["POST"])
@auth.admin_required
def edit(entry_id):
    if not db.available():
        abort(503)
    tech = auth.current_tech()
    if tech is None:
        flash("Sign in with your name and PIN first, so the correction is recorded under your name.", "error")
        return redirect(url_for("portal.tech_signin", next=url_for("atf.entry", entry_id=entry_id)))
    reason = " ".join((request.form.get("reason") or "").split())[:REASON_MAX]
    if not reason:
        flash("Say why you are changing the entry (it is kept with the change).", "error")
        return redirect(url_for("atf.entry", entry_id=entry_id))
    values = {}
    for key in atf_log.EDITABLE:
        if key in request.form:
            limit = 500 if key.endswith("address") or key == "notes" else 120
            raw = request.form.get(key) or ""
            text = raw.strip()[:limit] if key in ("notes",) else " ".join(raw.split())[:limit]
            values[key] = text
    if "serial_number" in values:
        values["serial_number"] = values["serial_number"].upper()
    changed = atf_log.edit_entry(
        entry_id, values, reason, tech["name"], clear_review=request.form.get("clear_review") == "1"
    )
    flash("Saved the correction." if changed else "Nothing was different, so nothing changed.", "success")
    return redirect(url_for("atf.entry", entry_id=entry_id))
