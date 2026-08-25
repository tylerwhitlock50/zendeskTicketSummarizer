"""RTM workflow blueprint: worklist, intake, detail hub, findings, close."""

import os
import re
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

from flask import Blueprint, flash, redirect, render_template, request, session, url_for

import rtm_db
import visual_client
from rtm_db import InvalidTransition, OpenSessionError
from summarizer import build_transcript, summarize_ticket
from visual_client import VisualError
from zendesk_client import ZendeskClient, ZendeskError, resolve_custom_fields

rtm_bp = Blueprint("rtm", __name__, url_prefix="/rtm")

LOCAL_TZ = ZoneInfo("America/Denver")

# Allowed forward transitions from each status (server-side mirror of rtm_db.transition rules).
ALLOWED_TRANSITIONS = {
    "received": ["in_inspection"],
    "in_inspection": ["in_repair", "qc_test"],
    "in_repair": ["qc_test"],
    "qc_test": ["ready", "shipped", "in_repair"],
    "ready": ["shipped", "in_repair"],
    "shipped": ["closed"],
    "closed": [],
}

STATUS_LABELS = {
    "received": "Received",
    "in_inspection": "In Inspection",
    "in_repair": "In Repair",
    "qc_test": "QC / Test",
    "ready": "Ready",
    "shipped": "Shipped",
    "closed": "Closed",
}

# The stepper across the top of the detail page: the happy path, in order,
# with labels short enough to sit under a 6px bar.
STATUS_ORDER = ["received", "in_inspection", "in_repair", "qc_test", "ready", "shipped", "closed"]
STEP_LABELS = {
    "received": "Received",
    "in_inspection": "Inspection",
    "in_repair": "Repair",
    "qc_test": "QC",
    "ready": "Ready",
    "shipped": "Shipped",
    "closed": "Closed",
}

# A rifle nobody has touched in this many days is stale; past this many days in
# the shop it is at risk. Both drive the bench banner and the "untouched" filter.
STALE_DAYS = 3
AT_RISK_DAYS = 14

# Status board ("Domino's tracker") columns: name -> statuses it collects.
# Any pre-shipment RTM with on_hold=true lands in Waiting instead of its
# status column.
BOARD_COLUMNS = [
    ("received", "Received", ["received"]),
    ("in_process", "In Process", ["in_inspection", "in_repair", "qc_test"]),
    ("waiting", "Waiting", []),
    ("ready", "Ready", ["ready"]),
    ("shipped", "Shipped", ["shipped"]),
]

TICKET_ID_RE = re.compile(r"(\d+)\s*/?\s*$")  # bare ID or Zendesk agent URL ending in the ID

_zendesk = None


def _get_zendesk():
    global _zendesk
    if _zendesk is None:
        _zendesk = ZendeskClient(
            subdomain=os.environ["ZENDESK_SUBDOMAIN"],
            email=os.environ["ZENDESK_EMAIL"],
            api_token=os.environ["ZENDESK_API_TOKEN"],
        )
    return _zendesk


@rtm_bp.record_once
def _on_register(state):
    # flash() needs a secret key; keep any key the app already has.
    if not state.app.secret_key:
        state.app.secret_key = os.environ.get("FLASK_SECRET_KEY", "rtm-dev-secret")


@rtm_bp.app_template_filter("localdt")
def localdt(value):
    """Render a UTC timestamptz as America/Denver, e.g. 'Aug 24, 2026 2:05 PM'."""
    if not value:
        return "—"
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return value
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    local = value.astimezone(LOCAL_TZ)
    return f"{local.strftime('%b %d, %Y')} {local.strftime('%I:%M %p').lstrip('0')}"


@rtm_bp.app_template_filter("money")
def money(value):
    """Dollars, two places. VISUAL stores unit costs at numeric(12,4) and a line
    total multiplies that out, so raw Decimals reach the page as $487.500000."""
    if value is None:
        return "—"
    try:
        return f"{Decimal(str(value)):,.2f}"
    except (InvalidOperation, ValueError):
        return str(value)


@rtm_bp.app_template_filter("hours")
def hours(value):
    """Decimal hours to two places, the way the labor line on an invoice reads."""
    if value is None:
        return "0.00"
    try:
        return f"{Decimal(str(value)):.2f}"
    except (InvalidOperation, ValueError):
        return str(value)


@rtm_bp.app_template_filter("logwhen")
def logwhen(value):
    """Compact local stamp for the work log, e.g. 'Aug 24 8:02 AM'."""
    value = _as_utc(value)
    if value is None:
        return "—"
    local = value.astimezone(LOCAL_TZ)
    return f"{local.strftime('%b %d')} {local.strftime('%I:%M %p').lstrip('0')}"


@rtm_bp.app_template_filter("localdate")
def localdate(value):
    if not value:
        return "—"
    if isinstance(value, datetime):
        value = value.astimezone(LOCAL_TZ).date()
    if isinstance(value, date):
        return value.strftime("%b %d, %Y")
    return str(value)


def _parse_ticket_id(raw):
    match = TICKET_ID_RE.search((raw or "").strip())
    return int(match.group(1)) if match else None


def _tech_id_from_form(field="tech_id"):
    raw = (request.form.get(field) or "").strip()
    return int(raw) if raw.isdigit() else None


def _days_open(row):
    started = row.get("received_at") or row.get("created_at")
    if not started:
        return None
    if isinstance(started, str):
        try:
            started = datetime.fromisoformat(started.replace("Z", "+00:00"))
        except ValueError:
            return None
    if started.tzinfo is None:
        started = started.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - started).days


def _as_utc(value):
    """Coerce a timestamp column (or ISO string) to an aware UTC datetime."""
    if not value:
        return None
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value


def _days_since(value):
    value = _as_utc(value)
    return None if value is None else (datetime.now(timezone.utc) - value).days


def _localtime(value):
    """'8:02 AM' in shop-local time, for the sentence on a bench row."""
    value = _as_utc(value)
    if value is None:
        return ""
    return value.astimezone(LOCAL_TZ).strftime("%I:%M %p").lstrip("0")


def _hm(hours):
    """Decimal hours as '1:20' — how a clock reads, not how a spreadsheet stores it."""
    if hours is None:
        return "0:00"
    total = int(round(float(hours) * 60))
    return f"{total // 60}:{total % 60:02d}"


# ---------------------------------------------------------------- station identity


def _current_tech():
    """Who is at this station, from the session cookie.

    The app has no login: a tech taps their name once in the header and every
    clock-in, status change, and close on this tablet is attributed to them.
    Returns None when nobody has claimed the station or the DB is unavailable.
    """
    tech_id = session.get("tech_id")
    if not tech_id:
        return None
    try:
        return rtm_db.get_tech(int(tech_id))
    except (RuntimeError, ValueError, TypeError):
        return None


@rtm_bp.app_context_processor
def _inject_station():
    """Station identity for the header, on RTM pages only.

    The ticket-print routes share the app but not this layout, and must keep
    rendering when the RTM database is not configured — so skip the queries
    entirely outside the RTM blueprints.
    """
    if request.blueprint not in ("rtm", "rtm_reports"):
        return {}
    tech = _current_tech()
    clock = None
    techs = []
    try:
        techs = rtm_db.get_lookups()["techs"]
        if tech is not None:
            clock = next(
                (s for s in rtm_db.list_open_sessions() if s["tech_id"] == tech["id"]), None
            )
    except RuntimeError:
        pass
    return {
        "station_tech": tech,
        "station_techs": techs,
        "station_clock": clock,
        "station_clock_hm": _hm(clock["hours"]) if clock else None,
    }


@rtm_bp.route("/station", methods=["POST"])
def station():
    """Claim (or release) this tablet for a tech."""
    raw = (request.form.get("tech_id") or "").strip()
    if raw.isdigit() and rtm_db.get_tech(int(raw)):
        session["tech_id"] = int(raw)
    else:
        session.pop("tech_id", None)
    return redirect(request.form.get("next") or url_for("rtm.worklist"))


def _station_tech_id():
    tech = _current_tech()
    return tech["id"] if tech else None


def _acting_tech_id(field="tech_id"):
    """The tech a POST should be attributed to: the form's pick, else the station."""
    return _tech_id_from_form(field) or _station_tech_id()


# ---------------------------------------------------------------- worklist


def _bench_line(row):
    """The one sentence a row gets: why this rifle is sitting where it is.

    Returns (tone, text); tone picks the colour, and is never red-on-grey.
    """
    idle = row.get("idle_days")
    if row.get("on_hold"):
        reason = (row.get("hold_reason") or "something").rstrip(".")
        tail = f" — no movement in {idle} days" if idle and idle >= STALE_DAYS else ""
        return "warn", f"Waiting on {reason}{tail}"
    if row.get("open_session_tech"):
        since = _localtime(row.get("open_session_started_at"))
        return "live", f"{row['open_session_tech']} on the clock since {since}"
    if row.get("status") == "ready":
        return "plain", "Cleared QC — pack out and print the label"
    if row.get("status") == "shipped":
        return "plain", "Shipped — needs a review and close"
    if row.get("repeat_return"):
        return "danger", "Second return on this serial"
    if idle is not None and idle >= STALE_DAYS:
        return "warn", f"{STATUS_LABELS.get(row.get('status'))} — untouched for {idle} days"
    return "plain", STATUS_LABELS.get(row.get("status"), row.get("status"))


def _bench_action(row, can_clock_in):
    """The verb for this row. One per rifle, derived from status — never ambiguous.

    kind drives the button's weight: 'chase' is the loud one, 'quiet' just navigates.
    """
    if row.get("on_hold"):
        return {"label": "Chase it", "kind": "chase"}
    if row.get("status") == "ready":
        return {"label": "Ship it", "kind": "ship"}
    if row.get("open_sessions"):
        return {"label": "Open", "kind": "quiet"}
    if can_clock_in:
        return {"label": "Clock in", "kind": "clock"}
    # Nobody at the station, or they are already on the clock elsewhere — a
    # "Clock in" button here would only ever produce an error.
    return {"label": "Open", "kind": "quiet"}


@rtm_bp.route("/")
def worklist():
    """The bench: every open rifle, oldest first, each with one thing to do next.

    Replaces the status-grouped worklist — status is a colour here, not an
    ordering. ``view`` filters to the station tech's rifles or to stale ones.
    """
    station_tech_id = _station_tech_id()
    rows = rtm_db.list_open_rtms(tech_id=station_tech_id)

    try:
        open_sessions = rtm_db.list_open_sessions()
    except RuntimeError:
        open_sessions = []
    # One open session per tech is a hard rule in the schema, so a station tech
    # who is already on the clock cannot start another.
    can_clock_in = bool(station_tech_id) and not any(
        s["tech_id"] == station_tech_id for s in open_sessions
    )

    for row in rows:
        row["days_open"] = _days_open(row)
        row["idle_days"] = _days_since(row.get("last_activity_at"))
        row["at_risk"] = (row["days_open"] or 0) >= AT_RISK_DAYS
        row["stale"] = (row["idle_days"] or 0) >= STALE_DAYS
        row["line_tone"], row["line"] = _bench_line(row)
        row["action"] = _bench_action(row, can_clock_in)

    views = [
        ("all", "Everything", rows),
        ("mine", "Mine", [r for r in rows if r.get("mine")]),
        ("stale", f"Untouched {STALE_DAYS}+ days", [r for r in rows if r["stale"]]),
    ]
    view = request.args.get("view") or "all"
    if view not in {key for key, _, _ in views}:
        view = "all"
    visible = next(rows_ for key, _, rows_ in views if key == view)

    waiting = [r for r in rows if r.get("on_hold")]
    ready = [r for r in rows if r.get("status") == "ready"]
    # The banner calls out one rifle — the oldest that is both old and ignored.
    at_risk = [r for r in rows if r["at_risk"] and r["stale"]] or [r for r in rows if r["at_risk"]]

    for s in open_sessions:
        s["elapsed"] = _hm(s.get("hours"))
        s["since"] = _localtime(s.get("started_at"))
    busy_ids = {s["tech_id"] for s in open_sessions}
    idle_techs = [t for t in rtm_db.get_lookups()["techs"] if t["id"] not in busy_ids]

    return render_template(
        "rtm/worklist.html",
        rows=visible,
        total=len(rows),
        views=[(key, label, len(rows_)) for key, label, rows_ in views],
        view=view,
        at_risk=at_risk[0] if at_risk else None,
        at_risk_count=sum(1 for r in rows if r["at_risk"]),
        waiting=waiting,
        ready=ready,
        open_sessions=open_sessions,
        idle_techs=idle_techs,
        today=datetime.now(LOCAL_TZ).strftime("%A, %b %-d"),
        status_labels=STATUS_LABELS,
        at_risk_days=AT_RISK_DAYS,
    )


# ---------------------------------------------------------------- status board


@rtm_bp.route("/board")
def board():
    """Wall-display status board: Received / In Process / Waiting / Ready / Shipped.

    Auto-refreshes via meta tag; pre-shipment RTMs with on_hold=true show in
    Waiting regardless of their underlying status.
    """
    rows = rtm_db.list_open_rtms()
    columns = {key: [] for key, _, _ in BOARD_COLUMNS}
    for row in rows:
        row["days_open"] = _days_open(row)
        if row.get("on_hold") and row.get("status") not in ("shipped", "closed"):
            columns["waiting"].append(row)
            continue
        for key, _, statuses in BOARD_COLUMNS:
            if row.get("status") in statuses:
                columns[key].append(row)
                break
    board_columns = [(key, label, columns[key]) for key, label, _ in BOARD_COLUMNS]
    return render_template(
        "rtm/board.html", board_columns=board_columns, status_labels=STATUS_LABELS
    )


# ---------------------------------------------------------------- intake


@rtm_bp.route("/intake", methods=["GET", "POST"])
def intake():
    lookups = rtm_db.get_lookups()
    if request.method == "GET":
        return render_template("rtm/intake.html", lookups=lookups)

    serial = (request.form.get("serial_no") or "").strip().upper()
    if not serial:
        flash("Scan or type a serial number.", "warning")
        return render_template("rtm/intake.html", lookups=lookups), 400

    ticket_id = None
    raw_ticket = (request.form.get("zendesk_ticket") or "").strip()
    if raw_ticket:
        ticket_id = _parse_ticket_id(raw_ticket)
        if ticket_id is None:
            flash("Zendesk ticket must be a numeric ID or a ticket URL — RTM created without it.", "warning")

    manual_fields = {
        key: value.strip()
        for key in ("model", "caliber", "barrel_length")
        if (value := request.form.get(key) or "").strip()
    }

    visual = None
    try:
        visual = visual_client.lookup_serial(serial)
        if visual.part_id is None:
            flash(f"Serial {serial} not found in VISUAL — using manually entered rifle info.", "warning")
    except VisualError as exc:
        flash(f"VISUAL unavailable ({exc}) — using manually entered rifle info.", "warning")

    rtm_id = rtm_db.create_rtm(
        serial_no=serial,
        visual=visual,
        zendesk_ticket_id=ticket_id,
        reason_for_return=(request.form.get("reason_for_return") or "").strip() or None,
        created_by=_acting_tech_id(),
        manual_fields=manual_fields or None,
    )
    flash(f"RTM created for serial {serial}.", "success")
    return redirect(url_for("rtm.detail", rtm_id=rtm_id))


@rtm_bp.route("/api/ticket/<int:ticket_id>")
def api_ticket(ticket_id):
    """Ticket-first intake: subject, requester, and any serial found in the
    ticket's custom fields (field title containing 'serial')."""
    try:
        client = _get_zendesk()
        ticket, requester, _org = client.get_ticket(ticket_id)
        fields_map = client.get_ticket_fields()
        form = client.get_ticket_form(ticket.get("ticket_form_id"))
    except ZendeskError as exc:
        return {"found": False, "error": str(exc)}, 502
    except KeyError as exc:
        return {"found": False, "error": f"Zendesk is not configured (missing {exc})."}, 502

    serial = None
    for field in resolve_custom_fields(ticket, fields_map, form):
        if "serial" in (field.get("title") or "").lower():
            serial = (field.get("value") or "").strip().upper() or None
            break

    return {
        "found": True,
        "ticket_id": ticket.get("id"),
        "subject": ticket.get("subject"),
        "requester": (requester or {}).get("name"),
        "status": ticket.get("status"),
        "serial": serial,
    }


@rtm_bp.route("/api/serial/<serial>")
def api_serial(serial):
    serial = serial.strip().upper()
    try:
        info = visual_client.lookup_serial(serial)
    except VisualError as exc:
        return {"found": False, "error": str(exc)}, 502

    prior = rtm_db.history_for_serial(serial)
    prior_out = [
        {
            "rtm_id": p.get("id"),
            "rtm_number": p.get("rtm_number"),
            "status": p.get("status"),
            "created_at": p["created_at"].isoformat() if p.get("created_at") else None,
            "resolution": p.get("resolution_label") or p.get("resolution"),
        }
        for p in prior
    ]

    if info.part_id is None:
        return {"found": False, "prior_rtms": prior_out, "repeat": bool(prior_out)}

    product = info.product or {}
    return {
        "found": True,
        "part_id": info.part_id,
        "model": product.get("model"),
        "caliber": product.get("caliber"),
        "barrel_length": product.get("barrel_length"),
        "build_date": info.build_date.isoformat() if info.build_date else None,
        "ship_date": info.ship_date.isoformat() if info.ship_date else None,
        "original_customer_id": info.original_customer_id,
        "prior_rtms": prior_out,
        "repeat": bool(prior_out),
    }


# ---------------------------------------------------------------- detail hub


def _load_rtm_or_404(rtm_id):
    rtm = rtm_db.get_rtm(rtm_id)
    if rtm is None:
        return None
    return rtm


def _work_log(rtm):
    """Clock sessions, parts, and status changes as one timeline, newest first.

    Three cards told the same story separately; merged, the total cost explains
    itself line by line.
    """
    entries = []
    for s in rtm.get("sessions") or []:
        who = s.get("tech_name") or f"Tech {s.get('tech_id')}"
        entries.append(
            {
                "when": s.get("started_at"),
                "tone": "live" if s.get("open") else "labor",
                "text": f"{who} clocked in — still running" if s.get("open")
                        else f"{who} worked the rifle",
                "amount": f"{hours(s.get('hours'))} h",
            }
        )
    for line in rtm.get("part_lines") or []:
        desc = line.get("description") or ""
        entries.append(
            {
                "when": line.get("added_at"),
                "tone": "part",
                "text": f"{line.get('part_id')}{f' · {desc}' if desc else ''}",
                "amount": f"${money(line.get('line_cost'))}",
            }
        )
    for ev in rtm.get("status_events") or []:
        who = f" by {ev['tech_name']}" if ev.get("tech_name") else ""
        entries.append(
            {
                "when": ev.get("changed_at"),
                "tone": "status",
                "text": f"Moved to {STATUS_LABELS.get(ev.get('status'), ev.get('status'))}{who}",
                "amount": "",
            }
        )
    entries.sort(key=lambda e: _as_utc(e["when"]) or datetime.min.replace(tzinfo=timezone.utc),
                 reverse=True)
    return entries


def _judgment(label, field, options, selected_id, shown=3):
    """One findings row: the picked option first, the tail collapsed behind '+N more'.

    Findings gate close(), so they belong on the page where the work happens —
    not on a route people only meet as an error message.
    """
    ordered = sorted(options, key=lambda o: o["id"] != selected_id)
    # Hiding a single option behind a disclosure costs more than it saves.
    if len(ordered) - shown <= 1:
        shown = len(ordered)
    return {
        "label": label,
        "field": field,
        "visible": ordered[:shown],
        "hidden": ordered[shown:],
        "selected_id": selected_id,
        "required": selected_id is None,
    }


def _steps(rtm):
    """The status stepper: visited, current, or still ahead."""
    current = rtm.get("status")
    visited = {ev.get("status") for ev in rtm.get("status_events") or []}
    out = []
    for status in STATUS_ORDER:
        if status == current:
            state = "current"
        elif status in visited:
            state = "done"
        else:
            state = "todo"
        out.append({"status": status, "label": STEP_LABELS[status], "state": state})
    return out


@rtm_bp.route("/<int:rtm_id>")
def detail(rtm_id):
    """One rifle, in the order a tech meets it: why it came back, what you found,
    what it has cost, and the single next step."""
    rtm = _load_rtm_or_404(rtm_id)
    if rtm is None:
        return render_template("error.html", ticket_id=rtm_id, message="RTM not found."), 404
    lookups = rtm_db.get_lookups()
    status = rtm.get("status")
    allowed = ALLOWED_TRANSITIONS.get(status, [])
    open_sessions = [s for s in rtm.get("sessions", []) if s.get("open")]

    station_id = _station_tech_id()
    my_session = next((s for s in open_sessions if s.get("tech_id") == station_id), None)

    return render_template(
        "rtm/detail.html",
        rtm=rtm,
        lookups=lookups,
        allowed=allowed,
        status_labels=STATUS_LABELS,
        open_sessions=open_sessions,
        my_session=my_session,
        my_session_hm=_hm(my_session["hours"]) if my_session else None,
        steps=_steps(rtm),
        work_log=_work_log(rtm),
        judgments=[
            _judgment("Root cause", "root_cause_id", lookups["root_cause"],
                      rtm.get("root_cause_id")),
            _judgment("Responsibility", "responsibility_id", lookups["responsibility"],
                      rtm.get("responsibility_id")),
            _judgment("Resolution", "resolution_id", lookups["resolution"],
                      rtm.get("resolution_id")),
        ],
        days_open=_days_open(rtm),
        at_risk_days=AT_RISK_DAYS,
        # Closing goes through close(), which enforces root cause / responsibility /
        # resolution. A raw transition to 'closed' would walk straight past that
        # gate, so it never gets a button of its own.
        forward=[s for s in allowed if s != "closed"],
        close_is_next="closed" in allowed,
        closeable=status in ("qc_test", "ready", "shipped"),
    )


@rtm_bp.route("/<int:rtm_id>/status", methods=["POST"])
def status(rtm_id):
    to_status = (request.form.get("to_status") or "").strip()
    try:
        rtm_db.transition(rtm_id, to_status, tech_id=_acting_tech_id())
        flash(f"Status changed to {STATUS_LABELS.get(to_status, to_status)}.", "success")
    except InvalidTransition as exc:
        flash(str(exc) or f"Cannot move to {to_status} from the current status.", "warning")
    return redirect(url_for("rtm.detail", rtm_id=rtm_id))


@rtm_bp.route("/<int:rtm_id>/hold", methods=["POST"])
def hold(rtm_id):
    """Toggle the Waiting flag (waiting on parts, customer response, etc.)."""
    turning_on = (request.form.get("on_hold") or "") == "1"
    reason = (request.form.get("hold_reason") or "").strip() or None
    rtm_db.set_hold(rtm_id, turning_on, reason)
    if turning_on:
        flash(f"Marked waiting{f': {reason}' if reason else ''}.", "success")
    else:
        flash("Waiting flag cleared.", "success")
    return redirect(url_for("rtm.detail", rtm_id=rtm_id))


@rtm_bp.route("/<int:rtm_id>/clock-in", methods=["POST"])
def clock_in(rtm_id):
    tech_id = _acting_tech_id()
    if tech_id is None:
        flash("Pick a tech before clocking in.", "warning")
        return redirect(url_for("rtm.detail", rtm_id=rtm_id))
    try:
        rtm_db.clock_in(rtm_id, tech_id)
        flash("Clocked in.", "success")
    except OpenSessionError as exc:
        flash(str(exc) or "That tech already has an open session.", "warning")
    return redirect(url_for("rtm.detail", rtm_id=rtm_id))


@rtm_bp.route("/<int:rtm_id>/clock-out", methods=["POST"])
def clock_out(rtm_id):
    tech_id = _acting_tech_id()
    if tech_id is None:
        flash("Pick a tech before clocking out.", "warning")
        return redirect(url_for("rtm.detail", rtm_id=rtm_id))
    rtm_db.clock_out(rtm_id, tech_id)
    flash("Clocked out.", "success")
    return redirect(url_for("rtm.detail", rtm_id=rtm_id))


# ---------------------------------------------------------------- parts


@rtm_bp.route("/api/part/<path:part_id>")
def api_part(part_id):
    try:
        part = visual_client.lookup_part(part_id.strip().upper())
    except VisualError as exc:
        return {"found": False, "error": str(exc)}, 502
    if part is None:
        return {"found": False}
    return {
        "found": True,
        "part_id": part["part_id"],
        "description": part.get("description"),
        "unit_cost": str(part.get("unit_cost") or "0"),
    }


@rtm_bp.route("/<int:rtm_id>/parts", methods=["POST"])
def add_part(rtm_id):
    part_id = (request.form.get("part_id") or "").strip().upper()
    if not part_id:
        flash("Enter a part ID.", "warning")
        return redirect(url_for("rtm.detail", rtm_id=rtm_id))
    try:
        qty = Decimal(request.form.get("qty") or "1")
        if qty <= 0:
            raise InvalidOperation
    except InvalidOperation:
        flash("Quantity must be a positive number.", "warning")
        return redirect(url_for("rtm.detail", rtm_id=rtm_id))

    try:
        part = visual_client.lookup_part(part_id)
    except VisualError as exc:
        flash(f"VISUAL unavailable — part not added ({exc}).", "warning")
        return redirect(url_for("rtm.detail", rtm_id=rtm_id))
    if part is None:
        flash(f"Part {part_id} not found in VISUAL.", "warning")
        return redirect(url_for("rtm.detail", rtm_id=rtm_id))

    rtm_db.add_part_line(
        rtm_id,
        part["part_id"],
        part.get("description") or "",
        qty,
        part.get("unit_cost") or Decimal("0"),
        _acting_tech_id(),
    )
    flash(f"Added {qty} x {part['part_id']}.", "success")
    return redirect(url_for("rtm.detail", rtm_id=rtm_id))


@rtm_bp.route("/<int:rtm_id>/parts/<int:line_id>/delete", methods=["POST"])
def delete_part(rtm_id, line_id):
    rtm_db.delete_part_line(rtm_id, line_id)
    flash("Part line removed.", "success")
    return redirect(url_for("rtm.detail", rtm_id=rtm_id))


# ---------------------------------------------------------------- findings


@rtm_bp.route("/<int:rtm_id>/findings", methods=["GET", "POST"])
def findings(rtm_id):
    rtm = _load_rtm_or_404(rtm_id)
    if rtm is None:
        return render_template("error.html", ticket_id=rtm_id, message="RTM not found."), 404
    lookups = rtm_db.get_lookups()

    if request.method == "POST":
        def _int_or_none(name):
            raw = (request.form.get(name) or "").strip()
            return int(raw) if raw.isdigit() else None

        rtm_db.save_findings(
            rtm_id,
            inspection_notes=(request.form.get("inspection_notes") or "").strip() or None,
            findings=(request.form.get("findings") or "").strip() or None,
            root_cause_id=_int_or_none("root_cause_id"),
            responsibility_id=_int_or_none("responsibility_id"),
            resolution_id=_int_or_none("resolution_id"),
            round_count=_int_or_none("round_count"),
            ammo_used=(request.form.get("ammo_used") or "").strip() or None,
        )
        flash("Findings saved.", "success")
        return redirect(url_for("rtm.detail", rtm_id=rtm_id))

    return render_template("rtm/findings.html", rtm=rtm, lookups=lookups)


# ---------------------------------------------------------------- close


@rtm_bp.route("/<int:rtm_id>/close", methods=["GET", "POST"])
def close(rtm_id):
    rtm = _load_rtm_or_404(rtm_id)
    if rtm is None:
        return render_template("error.html", ticket_id=rtm_id, message="RTM not found."), 404
    lookups = rtm_db.get_lookups()
    missing = [
        label
        for field, label in (
            ("root_cause_id", "Root cause"),
            ("responsibility_id", "Responsibility"),
            ("resolution_id", "Resolution"),
        )
        if not rtm.get(field)
    ]

    if request.method == "POST":
        try:
            rtm_db.close_rtm(rtm_id, _acting_tech_id())
            flash(f"{rtm.get('rtm_number', 'RTM')} closed.", "success")
            return redirect(url_for("rtm.worklist"))
        except (ValueError, InvalidTransition) as exc:
            flash(str(exc) or "Set root cause, responsibility, and resolution before closing.", "warning")
            return redirect(url_for("rtm.close", rtm_id=rtm_id))

    return render_template(
        "rtm/close.html", rtm=rtm, lookups=lookups, missing=missing, status_labels=STATUS_LABELS
    )


# ---------------------------------------------------------------- zendesk


@rtm_bp.route("/<int:rtm_id>/link-ticket", methods=["POST"])
def link_ticket(rtm_id):
    ticket_id = _parse_ticket_id(request.form.get("ticket_id"))
    if ticket_id is None:
        flash("Enter a numeric ticket ID or paste a Zendesk ticket URL.", "warning")
        return redirect(url_for("rtm.detail", rtm_id=rtm_id))
    rtm_db.link_ticket(rtm_id, ticket_id)
    flash(f"Linked Zendesk ticket #{ticket_id}.", "success")
    return redirect(url_for("rtm.detail", rtm_id=rtm_id))


@rtm_bp.route("/<int:rtm_id>/draft-reason", methods=["POST"])
def draft_reason(rtm_id):
    rtm = rtm_db.get_rtm(rtm_id)
    if rtm is None:
        return render_template("error.html", ticket_id=rtm_id, message="RTM not found."), 404
    ticket_id = rtm.get("zendesk_ticket_id")
    if not ticket_id:
        flash("Link a Zendesk ticket before drafting the reason for return.", "warning")
        return redirect(url_for("rtm.detail", rtm_id=rtm_id))

    try:
        client = _get_zendesk()
        ticket, _requester, _org = client.get_ticket(ticket_id)
        comments, users_by_id = client.get_comments(ticket_id)
        fields_map = client.get_ticket_fields()
        form = client.get_ticket_form(ticket.get("ticket_form_id"))
    except ZendeskError as exc:
        flash(f"Could not fetch ticket #{ticket_id}: {exc}", "warning")
        return redirect(url_for("rtm.detail", rtm_id=rtm_id))
    except KeyError as exc:
        flash(f"Zendesk is not configured (missing {exc}).", "warning")
        return redirect(url_for("rtm.detail", rtm_id=rtm_id))

    custom_fields = resolve_custom_fields(ticket, fields_map, form)
    transcript = build_transcript(comments, users_by_id)
    try:
        summary = summarize_ticket(ticket, custom_fields, transcript)
    except Exception as exc:
        flash(f"AI draft failed: {exc}", "warning")
        return redirect(url_for("rtm.detail", rtm_id=rtm_id))

    reason = (summary or {}).get("issue_summary") or ""
    if not reason.strip():
        flash("The AI summary came back empty — reason not changed.", "warning")
        return redirect(url_for("rtm.detail", rtm_id=rtm_id))
    rtm_db.set_reason(rtm_id, reason.strip())
    flash("Reason for return drafted from the Zendesk ticket.", "success")
    return redirect(url_for("rtm.detail", rtm_id=rtm_id))
