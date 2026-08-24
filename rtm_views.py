"""RTM workflow blueprint: worklist, intake, detail hub, findings, close."""

import os
import re
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

from flask import Blueprint, flash, redirect, render_template, request, url_for

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


# ---------------------------------------------------------------- worklist


@rtm_bp.route("/")
def worklist():
    rows = rtm_db.list_open_rtms()
    groups = []
    by_status = {}
    for row in rows:
        row["days_open"] = row.get("days_open", _days_open(row))
        by_status.setdefault(row.get("status"), []).append(row)
    for status in ALLOWED_TRANSITIONS:
        if by_status.get(status):
            groups.append((status, STATUS_LABELS.get(status, status), by_status[status]))
    return render_template("rtm/worklist.html", groups=groups, total=len(rows))


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
        created_by=_tech_id_from_form(),
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


@rtm_bp.route("/<int:rtm_id>")
def detail(rtm_id):
    rtm = _load_rtm_or_404(rtm_id)
    if rtm is None:
        return render_template("error.html", ticket_id=rtm_id, message="RTM not found."), 404
    lookups = rtm_db.get_lookups()
    status = rtm.get("status")
    allowed = ALLOWED_TRANSITIONS.get(status, [])
    open_sessions = [s for s in rtm.get("sessions", []) if s.get("open")]
    return render_template(
        "rtm/detail.html",
        rtm=rtm,
        lookups=lookups,
        allowed=allowed,
        status_labels=STATUS_LABELS,
        open_sessions=open_sessions,
    )


@rtm_bp.route("/<int:rtm_id>/status", methods=["POST"])
def status(rtm_id):
    to_status = (request.form.get("to_status") or "").strip()
    try:
        rtm_db.transition(rtm_id, to_status, tech_id=_tech_id_from_form())
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
    tech_id = _tech_id_from_form()
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
    tech_id = _tech_id_from_form()
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
        _tech_id_from_form(),
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
            rtm_db.close_rtm(rtm_id, _tech_id_from_form())
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
