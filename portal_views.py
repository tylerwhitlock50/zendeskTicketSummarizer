"""Scan portal: the repair queue, Incoming list, and the per-ticket stage screen.

Zendesk keeps the customer conversation and sends the emails: every stage
button posts a public comment (plus tags and, optionally, the "RMA Stage"
field) in one ticket update, and Zendesk's notify trigger emails the
customer. The shop-side record (who, when, what was found) is saved in the
``repair`` schema via ``repair_db``.
"""

import logging
import os
import re
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from flask import Blueprint, Response, abort, flash, redirect, render_template, request, url_for

import auth
import db
import delivery
import files
import repair_db
import repair_settings
import tracking
from zendesk_client import ZendeskClient, ZendeskError

logger = logging.getLogger(__name__)

portal_bp = Blueprint("portal", __name__)

NOTE_EXCERPT_CHARS = 200
QUEUED_TAG = "rp-queued"  # added in Zendesk when a ticket joins the queue (keeps Incoming short)
DUPLICATE_WINDOW_SECONDS = 120  # a double-tap must not email the customer twice
TRACKING_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 \-]{4,63}$")
SERIAL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 \-/.]{2,39}$")
TICKET_ID_MAX_DIGITS = 9  # longer all-digit scans are tracking numbers, not ticket IDs

_client = None


def _get_zendesk():
    """Lazy ZendeskClient singleton (kept local to avoid importing app)."""
    global _client
    if _client is None:
        _client = ZendeskClient(
            subdomain=os.environ["ZENDESK_SUBDOMAIN"],
            email=os.environ["ZENDESK_EMAIL"],
            api_token=os.environ["ZENDESK_API_TOKEN"],
        )
    return _client


def portal_tag():
    """The base tag stamped on every ticket the portal posts to."""
    return os.environ.get("PORTAL_TAG", "repair-portal")


def intake_tag():
    """The tag CS adds in Zendesk to put a ticket on the Incoming list."""
    return os.environ.get("INTAKE_TAG", "repair_process")


def stage_field_id():
    """Zendesk custom field id of the optional "RMA Stage" drop-down."""
    raw = os.environ.get("ZENDESK_STAGE_FIELD_ID", "").strip()
    return int(raw) if raw.isdigit() else None


def _agent_url(ticket_id):
    subdomain = os.environ.get("ZENDESK_SUBDOMAIN", "")
    return f"https://{subdomain}.zendesk.com/agent/tickets/{ticket_id}"


def render_message(template, values):
    """Fill {placeholders} in a stage's customer message; unknown ones stay as typed."""

    def sub(match):
        key = match.group(1)
        return str(values[key]) if key in values else match.group(0)

    text = re.sub(r"\{(\w+)\}", sub, template)
    return re.sub(r"(?<=\S) {2,}(?=\S)", " ", text)  # an empty placeholder leaves no double space


def _need_tech():
    """Redirect to sign-in if no tech is signed in on this device."""
    tech = auth.current_tech()
    if tech is None:
        flash("Sign in with your name and PIN first.", "error")
    return tech


def _back(ticket_id):
    return redirect(url_for("portal.action_page", ticket_id=ticket_id))


# ---------------------------------------------------------------- scan


@portal_bp.route("/scan")
def scan():
    return render_template("portal/scan.html", error=request.args.get("error"))


_CODE_FIELDS = (
    ("serial_number", "serial number"), ("ship_serial", "serial number"), ("zd_serial", "serial number"),
    ("tracking_number", "return tracking number"), ("inbound_tracking", "inbound tracking number"),
    ("zd_inbound_tracking", "inbound tracking number"),
)


def _matched_by(rma, code):
    norm = repair_db.normalize_code(code)
    for col, words in _CODE_FIELDS:
        if repair_db.normalize_code(rma.get(col)) == norm:
            return words
    return "scan"


@portal_bp.route("/scan/go")
def scan_go():
    """Open a ticket from whatever was scanned or typed.

    The one-pager barcode (ticket ID) or QR/agent URL opens the ticket. A rifle
    serial or a box's tracking label finds its RMA; anything else is searched
    in Zendesk (for a box that arrived before its ticket was added).
    """
    raw = " ".join((request.args.get("ticket_id") or "").split())
    if not raw:
        return redirect(url_for("portal.scan", error="Scan a barcode or type a ticket number."))
    url = re.search(r"/(?:tickets|t)/(\d+)\b", raw)
    if url:
        return redirect(url_for("portal.action_page", ticket_id=int(url.group(1))))
    short_id = raw.lstrip("#")
    is_ticket_id = short_id.isdigit() and len(short_id) <= TICKET_ID_MAX_DIGITS
    if is_ticket_id and (not db.available() or repair_db.rma_ids([int(short_id)])):
        return redirect(url_for("portal.action_page", ticket_id=int(short_id)))

    if db.available():
        found = repair_db.find_by_code(raw)
        if len(found) == 1:
            flash(f"Found ticket #{found[0]['ticket_id']} by its {_matched_by(found[0], raw)}.", "success")
            return redirect(url_for("portal.action_page", ticket_id=found[0]["ticket_id"]))
        if found:
            return render_template("portal/scan.html", code=raw, rmas=found, tickets=None,
                                   error=None)
    if is_ticket_id:
        return redirect(url_for("portal.action_page", ticket_id=int(short_id)))

    # Not in the queue: maybe a box for a ticket that hasn't been added yet.
    try:
        phrase = raw.replace('"', "")
        tickets = list(_get_zendesk().search_export(f'"{phrase}"', max_pages=1))[:20]
    except ZendeskError as exc:
        return redirect(url_for("portal.scan", error=f"No RMA matches “{raw}”, and Zendesk search failed: {exc}"))
    if len(tickets) == 1:
        flash(f"Found ticket #{tickets[0]['id']} in Zendesk by “{raw}”.", "success")
        return redirect(url_for("portal.action_page", ticket_id=tickets[0]["id"]))
    if not tickets:
        return redirect(url_for(
            "portal.scan",
            error=f"Nothing matches “{raw}”. Scan the barcode on the one-pager, the rifle's serial, "
                  "or the box's tracking label, or type the ticket number.",
        ))
    return render_template("portal/scan.html", code=raw, rmas=None, tickets=tickets, error=None)


# ---------------------------------------------------------------- tech sign-in


@portal_bp.route("/tech", methods=["GET", "POST"])
def tech_signin():
    if not db.available():
        return render_template("portal/tech.html", techs=None, next_url=None), 503
    next_url = request.values.get("next") or url_for("portal.queue")
    if not next_url.startswith("/") or next_url.startswith("//"):
        next_url = url_for("portal.queue")  # only local redirects

    if request.method == "POST":
        try:
            tech_id = int(request.form.get("tech_id") or 0)
        except ValueError:
            tech_id = 0
        tech, locked = repair_db.check_tech_pin(tech_id, (request.form.get("pin") or "").strip())
        if tech:
            auth.sign_in_tech(tech)
            flash(f"Signed in as {tech['name']}.", "success")
            return redirect(next_url)
        if locked:
            flash(
                f"Too many wrong PINs. Sign-in for that name is paused for "
                f"{repair_db.PIN_LOCK_MINUTES} minutes; ask a manager to reset it in Settings.",
                "error",
            )
        else:
            flash("That name and PIN don't match.", "error")

    return render_template(
        "portal/tech.html", techs=repair_db.list_techs(), next_url=next_url
    )


@portal_bp.route("/tech/signout", methods=["POST"])
def tech_signout():
    auth.sign_out_tech()
    flash("Signed out.", "success")
    return redirect(url_for("portal.tech_signin"))


# ---------------------------------------------------------------- queue + incoming


@portal_bp.route("/queue")
def queue():
    if not db.available():
        return render_template("portal/queue.html", columns=None, stages={}), 503
    stages = {s["key"]: s for s in repair_db.list_stages()}
    return render_template(
        "portal/queue.html",
        columns=repair_db.board(),
        stages=stages,
        board_stages=repair_db.BOARD_STAGES,
        status_words=tracking.STATUS_WORDS,
        tracking_service=tracking.provider_ready(),
    )


@portal_bp.route("/incoming")
def incoming():
    tag = intake_tag()
    if not db.available():
        return render_template("portal/incoming.html", rows=None, tag=tag), 503
    try:
        found = list(
            _get_zendesk().search_export(f"tags:{tag} -tags:{QUEUED_TAG} status<closed", max_pages=10)
        )
    except ZendeskError as exc:
        return render_template("error.html", ticket_id=None, message=str(exc)), 502
    added = repair_db.rma_ids([t["id"] for t in found if t.get("id")])
    rows = sorted(
        (t for t in found if t.get("id") and t["id"] not in added),
        key=lambda t: t.get("created_at") or "",
    )
    return render_template("portal/incoming.html", rows=rows, tag=tag)


@portal_bp.route("/t/<int:ticket_id>/add", methods=["POST"])
def add_to_queue(ticket_id):
    if not db.available():
        flash("Database not configured.", "error")
        return _back(ticket_id)
    tech = _need_tech()
    if tech is None:
        return redirect(url_for("portal.tech_signin", next=url_for("portal.action_page", ticket_id=ticket_id)))
    try:
        ticket, requester, _org = _get_zendesk().get_ticket(ticket_id)
    except ZendeskError as exc:
        flash(f"Could not read ticket #{ticket_id} from Zendesk: {exc}", "error")
        return redirect(url_for("portal.incoming"))
    if ticket.get("status") == "closed":
        flash(f"Ticket #{ticket_id} is closed in Zendesk, so the customer can't be emailed on it.", "error")
        return _back(ticket_id)

    added = repair_db.add_rma(
        ticket_id,
        ticket.get("subject"),
        (requester or {}).get("name"),
        (requester or {}).get("email"),
        tech,
        snapshot=_snapshot(ticket),
    )
    if added:
        try:  # tags only, no comment: Zendesk sends no email for this
            _get_zendesk().add_tags(ticket_id, [portal_tag(), QUEUED_TAG])
        except ZendeskError:
            logger.warning("Could not tag ticket %s %s; it may linger on Incoming", ticket_id, QUEUED_TAG)
        flash(f"Added ticket #{ticket_id} to the repair queue. Print its sheet to go with the rifle.", "success")
    else:
        flash(f"Ticket #{ticket_id} was already in the repair queue.", "success")
    if request.form.get("next") == "incoming":
        return redirect(url_for("portal.incoming"))
    return _back(ticket_id)


# ---------------------------------------------------------------- ticket page


@portal_bp.route("/t/<int:ticket_id>")
def action_page(ticket_id):
    zendesk = _get_zendesk()
    try:
        ticket, requester, _org = zendesk.get_ticket(ticket_id)
    except ZendeskError as exc:
        status = 404 if exc.status == 404 else 502
        return render_template("error.html", ticket_id=ticket_id, message=str(exc)), status

    tech = auth.current_tech()
    ctx = {
        "ticket": ticket,
        "requester": requester,
        "complaint": (ticket.get("description") or "").strip(),
        "agent_url": _agent_url(ticket_id),
        "base_tag": portal_tag(),
        "tech": tech,
        "db_error": None,
        "rma": None,
        "stages": [],
        "lists": {},
        "actions": [],
        "history": [],
        "legacy_history": [],
        "gallery": [],
        "gallery_error": None,
        "accept": files.ACCEPT_ATTR,
        "stage_by": {},
    }
    if not db.available():
        ctx["db_error"] = "Database not configured. Stage buttons are unavailable."
        return render_template("portal/action.html", **ctx)

    # Photos and files on the ticket (signed-in staff only: they can include customer documents).
    if tech:
        try:
            comments, users = zendesk.get_comments(ticket_id)
            files.remember(ticket_id, comments)
            ctx["gallery"] = files.gallery(comments, users)
        except ZendeskError as exc:
            ctx["gallery_error"] = f"Couldn't load the ticket's files from Zendesk: {exc}"

    rma = repair_db.get_rma(ticket_id)
    snap = _snapshot(ticket)
    if rma and snap is not None and any(
        rma.get(k) != snap.get(k) for k in (*repair_db.SNAPSHOT_COLUMNS, "ticket_fields")
    ):
        repair_db.update_snapshot(ticket_id, snap)  # CS corrected the ticket: keep the copy current
        rma = repair_db.get_rma(ticket_id)
    history = repair_db.events_for(ticket_id) if rma else []
    stage_by = {}
    for e in reversed(history):  # who last posted each stage
        if e["kind"] == "stage":
            stage_by[e["stage"]] = e["tech_name"]
    ctx.update(
        rma=rma,
        stages=repair_db.list_stages(),
        lists={k: repair_db.list_choices(k) for k in repair_db.CHOICE_KINDS},
        actions=db.list_quick_actions(),
        history=history,
        stage_by=stage_by,
        legacy_history=db.recent_actions(ticket_id),
        next_stage=_next_stage(rma["stage"]) if rma else None,
        current_index=repair_db.stage_index(rma["stage"]) if rma else -1,
        rifle=_rifle(rma, snap),
        tracking_url=tracking.tracking_url,
        carrier_name=tracking.carrier_name,
        status_words=tracking.STATUS_WORDS,
        tracking_service=tracking.provider_ready(),
        final_check_ok=_final_check_ok(rma) if rma else True,
    )
    return render_template("portal/action.html", **ctx)


def _snapshot(ticket):
    """Product data from the Zendesk ticket's fields, or None if they can't be read."""
    if not db.available():
        return None
    try:
        fields_map = _get_zendesk().get_ticket_fields()
    except ZendeskError:
        logger.warning("Could not read Zendesk ticket fields; product data not refreshed")
        return None
    return repair_settings.ticket_snapshot(ticket, fields_map, repair_settings.load())


def _rifle(rma, snap):
    """What we know about the rifle: verified on the bench, else from the ticket."""
    src = rma or {}
    zd = snap or {}

    def pick(verified, role):
        return src.get(verified) or src.get(f"zd_{role}") or zd.get(f"zd_{role}")

    return {
        "model": pick("model", "model"),
        "caliber": pick("caliber", "caliber"),
        "part_number": pick("part_number", "part_number"),
        "serial": pick("serial_number", "serial"),
        "ticket_serial": src.get("zd_serial") or zd.get("zd_serial"),
        "inbound": src.get("inbound_tracking") or src.get("zd_inbound_tracking") or zd.get("zd_inbound_tracking"),
        "ticket_inbound": src.get("zd_inbound_tracking") or zd.get("zd_inbound_tracking"),
        "fields": src.get("ticket_fields") or zd.get("ticket_fields") or [],
    }


def _final_check_ok(rma):
    """Passed the final safety/function check, and the accuracy test if one was needed."""
    if rma.get("function_check") != "pass":
        return False
    return not (rma.get("test_required") and rma.get("test_result") != "pass")
@portal_bp.route("/t/<int:ticket_id>/report")
def repair_report(ticket_id):
    """Printable repair report: every stage form, cleaned up, one page to print or save as PDF.

    Written for the customer: internal-only fields (inspection notes) are left out.
    """
    if not db.available():
        return render_template("error.html", ticket_id=ticket_id, message="Database not configured."), 503
    rma = repair_db.get_rma(ticket_id)
    if not rma:
        flash(f"Ticket #{ticket_id} isn't in the repair queue, so there's no repair report yet.", "error")
        return _back(ticket_id)
    try:
        ticket, requester, _org = _get_zendesk().get_ticket(ticket_id)
    except ZendeskError as exc:
        status = 404 if exc.status == 404 else 502
        return render_template("error.html", ticket_id=ticket_id, message=str(exc)), status

    history = repair_db.events_for(ticket_id)
    stage_by = {}
    for e in reversed(history):  # who last posted each stage
        if e["kind"] == "stage":
            stage_by[e["stage"]] = e["tech_name"]
    return render_template(
        "portal/repair_report.html",
        ticket=ticket,
        requester=requester,
        complaint=(ticket.get("description") or "").strip(),
        rma=rma,
        stages=repair_db.list_stages(),
        stage_by=stage_by,
        issues=[p for p in (rma.get("inspection") or []) if p.get("result") == "issue"],
        rifle=_rifle(rma, None),
        now=datetime.now(timezone.utc),
    )


def _next_stage(current):
    """The stage a tech most likely presses next (its form opens by default)."""
    order = repair_db.BOARD_STAGES
    if current not in order:
        return None
    i = order.index(current)
    return order[i + 1] if i + 1 < len(order) else None


@portal_bp.route("/t/<int:ticket_id>/file/<int:file_id>")
def ticket_file(ticket_id, file_id):
    """Serve one of the ticket's Zendesk attachments (or a thumbnail) to signed-in staff."""
    if auth.current_tech() is None:
        abort(403)
    zendesk = _get_zendesk()
    try:
        att = files.cached_attachment(ticket_id, file_id)
        if att is None:  # not seen recently: list the ticket's comments once and remember them
            comments, _users = zendesk.get_comments(ticket_id)
            files.remember(ticket_id, comments)
            att = files.find_attachment(comments, file_id)
        if att is None:
            abort(404)
        data, ctype = zendesk.download(att["content_url"])
    except ZendeskError as exc:
        return Response(str(exc), status=502, mimetype="text/plain")
    ctype = (att.get("content_type") or ctype or "application/octet-stream").split(";")[0].lower()
    name = re.sub(r'[^A-Za-z0-9._ -]+', "_", att.get("file_name") or "file")
    inline = ctype in files.INLINE_IMAGE_TYPES  # PDFs and the rest download (a sandboxed PDF won't render)
    resp = Response(data, mimetype=ctype if inline else "application/octet-stream")
    resp.headers["Content-Disposition"] = f'{"inline" if inline else "attachment"}; filename="{name}"'
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Content-Security-Policy"] = "sandbox"  # never run anything a customer uploaded
    resp.headers["Cache-Control"] = "no-store"  # shared shop devices: nothing stays after sign-out
    return resp


def _base_values(rma):
    """Placeholders from earlier stages, so any message can use them."""
    group = ""
    if rma.get("group_size") is not None:
        size = f"{float(rma['group_size']):g}"
        group = f"{size} MOA" if rma.get("group_unit") == "moa" else f'{size}"'
    return {
        "accessories": rma.get("accessory_summary") or "none",
        "arrival_note": "",
        "problem": rma.get("problem_summary") or "",
        "solution": rma.get("solution_summary") or "",
        "work_done": (rma.get("work_done") or rma.get("solution_summary") or "").rstrip(". "),  # messages add their own period
        "inspection": _inspection_text(rma.get("inspection") or []),
        "points": str(len(rma.get("inspection") or [])),
        "group": group,
        "distance": str(rma.get("test_distance") or ""),
        "ammo": rma.get("test_ammo") or "",
        "tracking": rma.get("tracking_number") or "",
        "tracking_url": tracking.tracking_url(rma.get("tracking_number"), rma.get("return_carrier")),
        "carrier": tracking.carrier_name(rma.get("return_carrier")),
        "tracking_link": _tracking_link(rma.get("tracking_number"), rma.get("return_carrier")),
        "serial": rma.get("serial_number") or rma.get("zd_serial") or "",
        "model": rma.get("model") or rma.get("zd_model") or "",
        "caliber": rma.get("caliber") or rma.get("zd_caliber") or "",
    }


def _tracking_link(number, carrier):
    url = tracking.tracking_url(number, carrier)
    return f"Track it here: {url}\n" if url else ""


def _clean(form, name, limit=120):
    return " ".join((form.get(name) or "").split())[:limit] or None


def _read_number(form, name, low, high, label):
    """An optional measurement: None if blank, a float in range, or an error string."""
    raw = (form.get(name) or "").strip()
    if not raw:
        return None
    try:
        value = float(raw)
    except ValueError:
        return f"{label} must be a number."
    if not low < value <= high:
        return f"{label} looks wrong ({raw}). Enter it in pounds, e.g. 3.5."
    return value


RESULT_WORDS = {"ok": "OK", "issue": "Issue found", "na": "Not applicable"}


def _inspection_text(points):
    lines = []
    for p in points:
        line = f"- {p['label']}: {RESULT_WORDS.get(p['result'], p['result'])}"
        if p.get("note"):
            line += f" ({p['note']})"
        lines.append(line)
    return "\n".join(lines)


def _read_inspection(form):
    """Every active checklist point must be marked OK, Issue or N/A."""
    points = []
    active = repair_db.list_choices("inspection_point")
    if not active:
        return "The inspection checklist is empty. Add points under Settings first."
    for p in active:
        result = form.get(f"point_{p['id']}")
        if result not in RESULT_WORDS:
            return f"Mark every inspection point (OK, Issue or N/A). “{p['label']}” is missing."
        note = " ".join((form.get(f"point_note_{p['id']}") or "").split())[:200]
        points.append({"id": p["id"], "label": p["label"], "result": result, "note": note or None})
    return points


def _read_stage_form(key, form, rma):
    """Validate one stage's form. Returns (fields, values, mode, internal message) or an error string.

    mode: "public" emails the customer; "internal" posts a Zendesk internal note;
    "none" records the stage without a Zendesk post.
    """
    fields, values, mode, internal = {}, {}, "public", None
    if key == "received":
        picked = _read_picks(form, "accessory", required=False)
        if isinstance(picked, str):
            return picked
        ids, other, summary, add = picked
        clear = form.get("arrival_clear")
        if clear not in ("clear", "loaded"):
            return "Safety first: check the chamber and magazine, then answer whether it arrived unloaded."
        serial = (_clean(form, "serial_number", 40) or "").upper().replace(" ", "")  # as stamped, scanner-style
        if not serial or not SERIAL_RE.match(serial):
            return "Scan or type the serial number from the rifle itself."
        expected = rma.get("zd_serial")
        serial_match = None
        if expected:
            serial_match = repair_db.normalize_code(serial) == repair_db.normalize_code(expected)
            if not serial_match and form.get("serial_confirm") != "1":
                return (f"The rifle's serial ({serial}) doesn't match the ticket ({expected}). Check it again; "
                        "if it really is different, tick “Serial is different from the ticket”.")
        inbound = _clean(form, "inbound_tracking", 64)
        if inbound and not TRACKING_RE.match(inbound):
            return "That inbound tracking number doesn't look right. Scan the label on the box."
        inbound_match = None
        if inbound and rma.get("zd_inbound_tracking"):
            inbound_match = repair_db.normalize_code(inbound) == repair_db.normalize_code(rma["zd_inbound_tracking"])
        model = _clean(form, "model")
        caliber = _clean(form, "caliber", 60)
        if not model or not caliber:
            return "Confirm the model and caliber (as marked on the rifle)."
        damage = form.get("arrival_damage")
        if damage not in ("yes", "no"):
            return "Answer: any damage or shipping issues?"
        notes = " ".join((form.get("arrival_notes") or "").split())[:500] or None
        if damage == "yes" and not notes:
            return "Describe the damage or shipping issue."
        fields = {
            "picks": ("accessory", ids, add),
            "accessory_other": other,
            "arrival_damage": damage == "yes",
            "arrival_notes": notes,
            "arrived_loaded": clear == "loaded",
            "serial_number": serial,
            "serial_match": serial_match,
            "inbound_tracking": inbound,
            "inbound_match": inbound_match,
            "model": model,
            "caliber": caliber,
            "part_number": _clean(form, "part_number", 60),
        }
        if clear == "loaded":
            fields["flag"] = "Arrived loaded"
        if serial_match is False:
            # Never email the customer about a rifle that may not be theirs: CS checks first.
            mode = "internal"
            fields["flag"] = "Serial mismatch"
            internal = (
                f"Received, but the serial on the rifle ({serial}) does not match the ticket ({expected}). "
                "Customer not emailed. CS: please confirm with the customer before work starts."
            )
        values = {
            "accessories": summary or "none",
            "arrival_note": (
                f" We noted the following on arrival: {notes}. Our team will reach out if anything needs your attention."
                if damage == "yes"
                else ""
            ),
            "serial": serial,
            "model": model,
            "caliber": caliber,
        }
    elif key == "inspected":
        points = _read_inspection(form)
        if isinstance(points, str):
            return points
        match = form.get("complaint_match")
        if match not in ("yes", "partly", "no"):
            return "Answer: does what you found match the customer's complaint?"
        picked = _read_picks(form, "problem")
        if isinstance(picked, str):
            return picked
        ids, other, summary, add = picked
        headspace = form.get("headspace")
        if headspace not in ("pass", "fail", "not_checked"):
            return "Answer the headspace check (Pass, Fail or Not checked)."
        pull = _read_number(form, "trigger_pull_found", 0, 15, "Trigger pull (as found)")
        if isinstance(pull, str):
            return pull
        fields = {
            "picks": ("problem", ids, add),
            "problem_other": other,
            "problem_notes": (form.get("problem_notes") or "").strip() or None,
            "inspection": points,
            "complaint_match": match,
            "headspace": headspace,
            "trigger_pull_found": pull,
        }
        values = {"problem": summary, "inspection": _inspection_text(points), "points": str(len(points))}
    elif key == "repaired":
        picked = _read_picks(form, "solution")
        if isinstance(picked, str):
            return picked
        ids, other, summary, add = picked
        work_done = (form.get("work_done") or "").strip()
        try:
            cause_id = int(form.get("root_cause_id") or 0)
        except ValueError:
            cause_id = 0
        if not repair_db.get_active_choices([cause_id], "root_cause"):
            return "Pick the root cause (for warranty and quality tracking)."
        coverage = form.get("coverage")
        if coverage not in ("warranty", "customer_pay", "goodwill"):
            return "Pick how the repair is covered: Warranty, Customer pay or Goodwill."
        pull = _read_number(form, "trigger_pull_after", 0, 15, "Trigger pull (after repair)")
        if isinstance(pull, str):
            return pull
        parts = " ".join((form.get("parts_used") or "").split())[:500] or None
        fields = {
            "picks": ("solution", ids, add), "solution_other": other, "work_done": work_done or None,
            "root_cause_id": cause_id, "coverage": coverage, "parts_used": parts, "trigger_pull_after": pull,
        }
        values = {"solution": summary, "work_done": (work_done or summary).rstrip(". ")}
    elif key == "tested":
        check = form.get("function_check")
        if check not in ("pass", "fail"):
            return "Answer the final safety and function check (Pass or Fail)."
        required = form.get("test_required")
        if required not in ("yes", "no"):
            return "Answer: is an accuracy test required for this rifle?"
        if check == "fail":
            cleared = {"function_check": "fail", "flag": "Failed final check"}
            if required == "no":
                cleared.update(test_required=False, test_result=None, group_size=None,
                               group_unit=None, test_distance=None, test_ammo=None)
            note = _clean(form, "function_notes", 300)
            return cleared, {}, "internal", (
                "Final safety / function check FAILED" + (f": {note}" if note else "")
                + ". Back to the bench. Customer not emailed."
            )
        if required == "no":
            cleared = {"function_check": "pass", "test_required": False, "test_result": None,
                       "group_size": None, "group_unit": None, "test_distance": None, "test_ammo": None}
            return cleared, {}, "internal", (
                "Final safety / function check passed. Accuracy test: not required for this rifle. "
                "Customer not emailed."
            )
        result = form.get("test_result")
        if result not in ("pass", "fail"):
            return "Pick the test result: pass or fail."
        try:
            group = float(form.get("group_size") or "")
        except ValueError:
            group = 0.0
        if not 0 < group <= 20:
            return "Enter the group size (a number, e.g. 0.75)."
        unit = form.get("group_unit") if form.get("group_unit") in ("moa", "in") else "moa"
        try:
            distance = int(form.get("test_distance") or 100)
        except ValueError:
            distance = 0
        if not 10 <= distance <= 2000:
            return "Enter the test distance in yards."
        ammo = " ".join((form.get("test_ammo") or "").split())[:120]
        if not ammo:
            return "Enter the ammunition used."
        size = f"{group:g}"
        group_text = f"{size} MOA" if unit == "moa" else f'{size}"'
        fields = {
            "function_check": "pass",
            "test_required": True,
            "test_result": result,
            "group_size": group,
            "group_unit": unit,
            "test_distance": distance,
            "test_ammo": ammo,
        }
        values = {"group": group_text, "distance": str(distance), "ammo": ammo}
        if result == "fail":
            mode = "internal"
            fields["flag"] = "Failed accuracy test"
            internal = (
                f"Accuracy test FAILED: {group_text} at {distance} yards with {ammo}. "
                "Customer not emailed."
            )
    elif key == "shipped":
        override = False
        if not _final_check_ok(rma):
            if form.get("ship_failed") != "1":
                if rma.get("function_check") is None and not rma.get("tested_at"):
                    why = "hasn't had its final safety / function check"
                elif rma.get("function_check") == "fail":
                    why = "failed its final safety / function check"
                elif rma.get("test_result") == "fail":
                    why = "failed its accuracy test"
                else:
                    why = "hasn't passed its final check"
                return (f"This rifle {why}. Post “Final test” first, or tick "
                        "“Ship anyway” if a manager approved shipping it as is.")
            override = True
        ship_serial = (_clean(form, "ship_serial", 40) or "").upper().replace(" ", "") or None
        if not ship_serial:
            return "Scan the serial number on the rifle going in the box."
        expected = rma.get("serial_number") or rma.get("zd_serial")
        if expected and repair_db.normalize_code(ship_serial) != repair_db.normalize_code(expected):
            return (f"Wrong rifle? The serial you scanned ({ship_serial}) is not this ticket's rifle "
                    f"({expected}). Nothing was sent.")
        if form.get("ship_unloaded") != "1":
            return "Confirm the rifle is unloaded and there is no ammunition in the box."
        number = (form.get("tracking_number") or "").strip()
        if not TRACKING_RE.match(number):
            return "Scan or type the return tracking number."
        carrier = form.get("return_carrier")
        if carrier not in tracking.CARRIER_NAMES:
            carrier = tracking.detect_carrier(number)
        fields = {"tracking_number": number, "ship_serial": ship_serial, "return_carrier": carrier,
                  "ship_override": override}
        values = {"tracking": number, "tracking_url": tracking.tracking_url(number, carrier),
                  "tracking_link": _tracking_link(number, carrier), "carrier": tracking.carrier_name(carrier)}
    return fields, values, mode, internal


def _with_video(body, video):
    return f"{body}\n\nVideo: {video}" if video else body


class _Stop(Exception):
    """A stage or update can't be posted; the message says why."""


def _read_attachments(form):
    """Uploaded files and the video link, or raise _Stop."""
    uploads, upload_error = files.read_uploads(request.files.getlist("files"))
    video = files.clean_video_url(form.get("video_url"))
    if upload_error or video is False:
        raise _Stop(upload_error or "That video link doesn't look like a web address (https://…).")
    return uploads, video


def _preview_attachments(form):
    """The preview is sent without the files themselves, only their names."""
    names = [n.strip() for n in form.getlist("file_names") if n.strip()]
    if len(names) > files.MAX_FILES:
        raise _Stop(f"Attach at most {files.MAX_FILES} files at a time.")
    for name in names:
        ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
        if ext not in files.ALLOWED:
            raise _Stop(f"“{name}” isn't a supported file type (photos, PDF or Excel).")
    video = files.clean_video_url(form.get("video_url"))
    if video is False:
        raise _Stop("That video link doesn't look like a web address (https://…).")
    return names, video


def _plan_stage(ticket_id, key, form, tech, has_files, video):
    """Work out exactly what a stage button would post, without posting it.

    The real post and its confirm pop-up both use this, so the pop-up shows the
    very message the customer gets. Raises _Stop with a message for the tech.
    """
    stage = repair_db.get_stage(key)
    if stage is None:
        raise _Stop("Unknown stage.")
    rma = repair_db.get_rma(ticket_id)
    if rma is None:
        raise _Stop("Add this ticket to the repair queue first.")

    moving_back = repair_db.stage_index(key) < repair_db.stage_index(rma["stage"])
    if moving_back and form.get("move_back") != "1":
        raise _Stop(
            f"This RMA is already past “{stage['label']}”. Tick “Move back to this stage” "
            "if you really mean to go back (the customer is emailed again)."
        )
    record_only = form.get("record_only") == "1"

    parsed = _read_stage_form(key, form, rma)
    if isinstance(parsed, str):
        raise _Stop(parsed)
    fields, values, mode, internal = parsed
    values = {**_base_values(rma), **values}

    if record_only:
        # Only for tickets Zendesk has closed (no comments allowed): keep the
        # repair record moving and tell CS to contact the customer another way.
        if has_files or video:
            raise _Stop("Zendesk won't take files on a closed ticket. Save the stage without them.")
        try:
            ticket, _req, _org = _get_zendesk().get_ticket(ticket_id)
        except ZendeskError as exc:
            raise _Stop(f"Could not read ticket #{ticket_id} from Zendesk: {exc}") from exc
        if ticket.get("status") != "closed":
            raise _Stop("This ticket is open in Zendesk, so post the stage normally.")
        mode = "none"

    if mode == "public":
        body = _with_video(render_message(stage["comment_body"], values), video)
    elif mode == "internal":
        body = _with_video(internal, video)
    elif has_files or video:  # nothing to tell the customer, but keep the files on the ticket
        mode, body = "internal", _with_video(f"{stage['label']}: recorded by {tech['name']}.", video)
    else:
        body = None
    return {"stage": stage, "rma": rma, "fields": fields, "mode": mode, "body": body, "record_only": record_only}


def _preview_guard():
    """Error response for a preview request that can't run, else None."""
    if not db.available():
        return {"ok": False, "error": "Database not configured."}, 503
    if auth.current_tech() is None:
        return {"ok": False, "error": "Sign in with your name and PIN first."}, 403
    return None


@portal_bp.route("/t/<int:ticket_id>/stage/<key>/preview", methods=["POST"])
def preview_stage(ticket_id, key):
    """What a stage button will post, for the confirm pop-up. Sends nothing."""
    blocked = _preview_guard()
    if blocked:
        return blocked
    try:
        names, video = _preview_attachments(request.form)
        plan = _plan_stage(ticket_id, key, request.form, auth.current_tech(), bool(names), video)
    except _Stop as exc:
        return {"ok": False, "error": str(exc)}
    label = plan["stage"]["label"]
    if plan["body"] is None:
        return {"ok": True, "title": label, "posts": [],
                "summary": "Saved to the repair record only. Nothing is posted to Zendesk and the customer is not emailed."}
    return {"ok": True, "title": label,
            "posts": [{"public": plan["mode"] == "public", "body": plan["body"], "files": names}]}


@portal_bp.route("/t/<int:ticket_id>/stage/<key>", methods=["POST"])
def post_stage(ticket_id, key):
    if not db.available():
        flash("Database not configured.", "error")
        return _back(ticket_id)
    tech = _need_tech()
    if tech is None:
        return redirect(url_for("portal.tech_signin", next=url_for("portal.action_page", ticket_id=ticket_id)))
    try:
        uploads, video = _read_attachments(request.form)
        plan = _plan_stage(ticket_id, key, request.form, tech, bool(uploads), video)
    except _Stop as exc:
        flash(str(exc), "error")
        return _back(ticket_id)
    stage, rma, fields, mode, body, record_only = (
        plan[k] for k in ("stage", "rma", "fields", "mode", "body", "record_only")
    )
    zendesk = _get_zendesk()

    custom_fields = {}
    if stage_field_id() and stage.get("zendesk_value"):
        custom_fields[stage_field_id()] = stage["zendesk_value"]
    extra = {"files": [u.filename for u in uploads]} if uploads else {}
    if video:
        extra["video_url"] = video

    posted = False
    try:
        # One ticket at a time: a double tap waits here, then finds the first
        # post's event below and stops, so the customer is emailed once.
        with repair_db.ticket_lock(ticket_id) as conn:
            if repair_db.recent_stage_event(conn, ticket_id, key, DUPLICATE_WINDOW_SECONDS):
                flash(f"“{stage['label']}” was just posted for this ticket. Not sent again.", "error")
                return _back(ticket_id)
            if body is not None:
                try:
                    tokens = [zendesk.upload_file(u.filename, u.data, u.content_type) for u in uploads]
                    zendesk.post_update(
                        ticket_id,
                        body,
                        public=(mode == "public"),
                        tags=[portal_tag(), f"rp-{key}"],
                        custom_fields=custom_fields,
                        uploads=tokens,
                    )
                except ZendeskError as exc:
                    flash(f"Zendesk rejected the update, so nothing was sent or saved: {exc}", "error")
                    return _back(ticket_id)
                posted = True
            if fields.get("picks") and fields["picks"][2]:
                kind, ids, add = fields["picks"]
                added = repair_db.ensure_choice(kind, add, conn=conn)
                fields["picks"] = (kind, ids if added["id"] in ids else [*ids, added["id"]], None)
            repair_db.record_stage(
                conn,
                ticket_id,
                stage,
                tech,
                fields,
                body if mode == "public" else None,
                rma["stage"],
                extra=extra,
            )
    except Exception:
        if not posted:
            raise
        logger.exception("Stage %s posted to Zendesk but not saved for ticket %s", key, ticket_id)
        flash(
            f"“{stage['label']}” was posted to Zendesk, but saving the repair record failed. "
            "Tell IT; do not press it again.",
            "error",
        )
        return _back(ticket_id)

    if record_only:
        flash(
            f"Saved “{stage['label']}” without emailing (ticket is closed in Zendesk). "
            "Let CS know so they can contact the customer.",
            "success",
        )
    elif mode == "public":
        flash(f"Posted “{stage['label']}” on ticket #{ticket_id}. Zendesk will email the customer.", "success")
    elif key == "tested" and (fields.get("test_result") == "fail" or fields.get("function_check") == "fail"):
        flash("Saved the failed test as an internal note and flagged the rifle. The customer was not emailed.",
              "success")
    elif key == "received" and fields.get("serial_match") is False:
        flash("Saved as an internal note and flagged “Serial mismatch”. The customer was not emailed; "
              "let CS know so they can confirm with the customer.", "success")
    else:
        flash(f"Saved “{stage['label']}”. The customer was not emailed.", "success")
    return _back(ticket_id)


OTHER_MAX = 200  # free-text "Other" on the checklists
LIST_LABEL_MAX = 80  # longest "Other" text that can be added to a list
_NOUNS = {"problem": "problem found", "solution": "repair done", "accessory": "accessory"}


def _read_picks(form, kind, required=True):
    """Read a form's checkboxes + "Other" text for one of the lists.

    Returns (choice ids, other text or None, customer-facing summary), or an
    error message. With "add to the list" ticked, the Other text becomes a
    list item (or re-uses a matching one) and is picked instead.
    """
    raw_ids = []
    for raw in form.getlist(f"{kind}_ids"):
        try:
            raw_ids.append(int(raw))
        except ValueError:
            continue
    choices = repair_db.get_active_choices(raw_ids, kind)
    other = " ".join((form.get(f"{kind}_other") or "").split())[:OTHER_MAX] or None
    if required and not choices and not other:
        return f"Tick at least one {_NOUNS[kind]}, or describe it under Other."
    labels = [c["label"] for c in choices] + ([other] if other else [])
    if other and form.get(f"{kind}_add") == "1":
        if len(other) > LIST_LABEL_MAX:
            return f"That Other text is too long to add to the list (max {LIST_LABEL_MAX} characters)."
        # Added to the list only once the post succeeds (see post_stage), as a tick, not Other.
        return [c["id"] for c in choices], None, ", ".join(labels), other
    return [c["id"] for c in choices], other, ", ".join(labels), None


def _plan_action(form, has_files, video):
    """Work out what an "Other update" button or a note would post. Raises _Stop."""
    action_id = form.get("action_id")
    note = (form.get("note") or "").strip()
    action = None
    if action_id:
        try:
            action = db.get_quick_action(int(action_id))
        except ValueError:
            action = None
        if not action or not action["active"]:
            raise _Stop("That update button no longer exists.")
        public, kind = bool(action.get("public", True)), "action"
        body = action["comment_body"]
        tags = [portal_tag(), action["tag"]]
        label = action["label"]
    elif note or has_files or video:
        public = form.get("note_visibility") == "public"
        body, kind = note or ("Photos/files attached." if has_files else "Video link:"), "note"
        tags = [portal_tag()]
        label = "Public note" if public else "Internal note"
    else:
        raise _Stop("Pick an update, or write a note / attach a file first.")
    note_public = form.get("note_visibility") == "public"
    # A note, files or video typed alongside a button go in a SEPARATE post that
    # follows the note's own Internal/Public choice, never the button's.
    follow_up = None
    if action is not None and (note or has_files or video):
        follow_up = _with_video(note or "Photos/files attached.", video)
    else:
        body = _with_video(body, video)
    return {
        "action": action, "note": note, "public": public, "kind": kind, "body": body, "tags": tags,
        "label": label, "note_public": note_public, "follow_up": follow_up,
    }


@portal_bp.route("/t/<int:ticket_id>/action/preview", methods=["POST"])
def preview_action(ticket_id):
    """What an "Other update" button or note will post, for the confirm pop-up. Sends nothing."""
    blocked = _preview_guard()
    if blocked:
        return blocked
    try:
        names, video = _preview_attachments(request.form)
        plan = _plan_action(request.form, bool(names), video)
    except _Stop as exc:
        return {"ok": False, "error": str(exc)}
    if plan["follow_up"] is None:
        posts = [{"public": plan["public"], "body": plan["body"], "files": names}]
    else:  # the button's message, then the note in its own post
        posts = [
            {"public": plan["public"], "body": plan["body"], "files": []},
            {"public": plan["note_public"], "body": plan["follow_up"], "files": names},
        ]
    return {"ok": True, "title": plan["label"], "posts": posts}


@portal_bp.route("/t/<int:ticket_id>/action", methods=["POST"])
def post_action(ticket_id):
    """Other updates (Awaiting parts, On hold...) and notes, with optional files (no stage change)."""
    if not db.available():
        flash("Database not configured. Cannot post actions.", "error")
        return _back(ticket_id)
    tech = _need_tech()
    if tech is None:
        return redirect(url_for("portal.tech_signin", next=url_for("portal.action_page", ticket_id=ticket_id)))

    try:
        uploads, video = _read_attachments(request.form)
        plan = _plan_action(request.form, bool(uploads), video)
    except _Stop as exc:
        flash(str(exc), "error")
        return _back(ticket_id)
    action, note, public, kind, body, tags, label, note_public, follow_up = (
        plan[k] for k in ("action", "note", "public", "kind", "body", "tags", "label", "note_public", "follow_up")
    )
    excerpt = note[:NOTE_EXCERPT_CHARS] if note else None

    zendesk = _get_zendesk()
    try:
        tokens = [zendesk.upload_file(u.filename, u.data, u.content_type) for u in uploads]
        if follow_up is None:
            zendesk.post_update(ticket_id, body, public=public, tags=tags, uploads=tokens)
        else:
            zendesk.post_update(ticket_id, body, public=public, tags=tags)
            zendesk.post_update(ticket_id, follow_up, public=note_public, tags=[portal_tag()], uploads=tokens)
    except ZendeskError as exc:
        flash(f"Zendesk rejected the update: {exc}", "error")
        return _back(ticket_id)

    try:
        if repair_db.get_rma(ticket_id):
            detail = {}
            if excerpt:
                detail["note"] = excerpt
            if uploads:
                detail["files"] = [u.filename for u in uploads]
            if video:
                detail["video_url"] = video
            repair_db.record_event(
                ticket_id, kind, label, tech, detail=detail or None, customer_message=body if public else None
            )
            if action is not None:
                repair_db.set_flag(ticket_id, label)
        else:
            db.log_action(ticket_id, label, excerpt, tech["name"])
    except Exception:  # the Zendesk write succeeded; a lost log line is not fatal
        logger.exception("Could not log portal action for ticket %s", ticket_id)

    what = "a public reply (the customer is emailed)" if public else "an internal note"
    extra = ""
    if follow_up is not None:
        extra = " Your note was posted separately as " + ("a public reply." if note_public else "an internal note.")
    flash(f"Posted “{label}” as {what} on ticket #{ticket_id}.{extra}", "success")
    return _back(ticket_id)


@portal_bp.route("/t/<int:ticket_id>/clear-flag", methods=["POST"])
def clear_flag(ticket_id):
    """Take an "Other update" (e.g. On hold) off the queue card without posting anything."""
    if not db.available() or _need_tech() is None:
        return _back(ticket_id)
    repair_db.set_flag(ticket_id, None)
    flash("Cleared the status flag.", "success")
    return _back(ticket_id)


# ---------------------------------------------------------------- delivery


@portal_bp.route("/t/<int:ticket_id>/delivery/check", methods=["POST"])
def check_delivery(ticket_id):
    """Ask the tracking service now (the background checker does this hourly)."""
    if not db.available() or _need_tech() is None:
        return _back(ticket_id)
    rma = repair_db.get_rma(ticket_id)
    if not rma or rma["stage"] != "shipped" or not rma.get("tracking_number"):
        flash("This RMA hasn't shipped yet.", "error")
        return _back(ticket_id)
    if not tracking.provider_ready():
        flash("No tracking service is set up, so delivery can't be checked automatically. "
              "Open the tracking link, then use “Mark delivered”.", "error")
        return _back(ticket_id)
    try:
        status = delivery.check_one(_get_zendesk(), rma, repair_settings.load())
    except (tracking.TrackingError, ZendeskError) as exc:
        flash(f"Couldn't check tracking: {exc}", "error")
        return _back(ticket_id)
    if status == "delivered":
        flash("Delivered. The customer was emailed (if delivered emails are on in Settings).", "success")
    else:
        flash(f"Tracking says: {tracking.STATUS_WORDS.get(status, status)}.", "success")
    return _back(ticket_id)


def _delivered_body(ticket_id):
    rma = repair_db.get_rma(ticket_id)
    if not rma or rma["stage"] != "shipped":
        raise _Stop("This RMA hasn't shipped yet.")
    if rma.get("delivered_at"):
        raise _Stop("This RMA is already marked delivered.")
    return delivery.delivered_message(rma, None, repair_settings.load())


@portal_bp.route("/t/<int:ticket_id>/delivery/preview", methods=["POST"])
def preview_delivered(ticket_id):
    blocked = _preview_guard()
    if blocked:
        return blocked
    try:
        body = _delivered_body(ticket_id)
    except _Stop as exc:
        return {"ok": False, "error": str(exc)}
    if request.form.get("email") != "1":
        return {"ok": True, "title": "Delivered", "posts": [],
                "summary": "Saved to the repair record only. The customer is not emailed."}
    return {"ok": True, "title": "Delivered", "posts": [{"public": True, "body": body, "files": []}]}


@portal_bp.route("/t/<int:ticket_id>/delivery/mark", methods=["POST"])
def mark_delivered(ticket_id):
    """A tech confirms delivery by hand (no tracking service, or it's behind)."""
    if not db.available():
        return _back(ticket_id)
    tech = _need_tech()
    if tech is None:
        return redirect(url_for("portal.tech_signin", next=url_for("portal.action_page", ticket_id=ticket_id)))
    try:
        _delivered_body(ticket_id)
        outcome = delivery.mark_delivered(
            _get_zendesk(), ticket_id, tech=tech, how="manual", email=request.form.get("email") == "1",
            detail="Marked delivered by " + tech["name"],
        )
    except _Stop as exc:
        flash(str(exc), "error")
        return _back(ticket_id)
    except ZendeskError as exc:
        flash(f"Zendesk rejected the update, so nothing was sent or saved: {exc}", "error")
        return _back(ticket_id)
    flash({
        "emailed": "Marked delivered. Zendesk will email the customer.",
        "recorded": "Marked delivered. The customer was not emailed.",
        "already": "It was already marked delivered.",
        "not_shipped": "This RMA hasn't shipped yet.",
    }[outcome], "success" if outcome in ("emailed", "recorded") else "error")
    return _back(ticket_id)


@portal_bp.route("/deliveries/check", methods=["POST"])
def check_all_deliveries():
    if not db.available() or _need_tech() is None:
        return redirect(url_for("portal.queue"))
    if not tracking.provider_ready():
        flash("No tracking service is set up (TRACKING_PROVIDER).", "error")
        return redirect(url_for("portal.queue"))
    counts = delivery.check_all(_get_zendesk(), recheck_minutes=0)
    if not counts:
        flash("No shipments waiting on delivery.", "success")
    else:
        parts = [f"{n} {tracking.STATUS_WORDS.get(k, k).lower()}" for k, n in sorted(counts.items())]
        flash("Checked tracking: " + ", ".join(parts) + ".", "success")
    return redirect(url_for("portal.queue"))


# ---------------------------------------------------------------- final reports


COVERAGE_WORDS = {"warranty": "Warranty", "customer_pay": "Customer pay", "goodwill": "Goodwill"}


def _report_values(rma, ticket_id):
    """Placeholders for the final repair report (customer-facing: no internal notes)."""
    base = _base_values(rma)

    def day(ts):
        return ts.astimezone(ZoneInfo(repair_db.REPORT_TZ)).strftime("%B %d, %Y") if ts else "not yet"

    if rma.get("arrival_damage"):
        arrival = f"Noted on arrival: {rma.get('arrival_notes') or 'damage'}"
    elif rma.get("received_at"):
        arrival = "No damage noted"
    else:
        arrival = "not recorded"
    if not rma.get("tested_at"):
        test = "Not yet tested."
    else:
        check = {"pass": "passed", "fail": "FAILED"}.get(rma.get("function_check"), "not recorded")
        test = f"Safety and function check: {check}."
        if rma.get("test_required") is False:
            test += " Accuracy test: not required for this repair."
        elif rma.get("test_result"):
            test += (f" Accuracy test: {'passed' if rma['test_result'] == 'pass' else 'did not pass'}, "
                     f"{base['group']} group at {base['distance']} yards with {base['ammo']}.")
    return {
        **base,
        "ticket_id": str(ticket_id),
        "model": base["model"] or "rifle",
        "serial": base["serial"] or "not recorded",
        "received_date": day(rma.get("received_at")),
        "shipped_date": day(rma.get("shipped_at")),
        "arrival": arrival,
        "inspection": base["inspection"] or "(inspection not recorded)",
        "problem": base["problem"] or "not recorded",
        "work_done": base["work_done"] or "not recorded",
        "parts_line": f"Parts replaced: {rma['parts_used']}\n" if rma.get("parts_used") else "",
        "test": test,
        "tracking": base["tracking"] or "(not shipped yet)",
        "tracking_url": base["tracking_url"],
    }


def _report_draft(rma, saved):
    """The report text: the saved edit if there is one, else built from the record."""
    if saved and saved.get("body"):
        return saved["body"]
    template = repair_settings.load()["final_report_template"]
    text = render_message(template, _report_values(rma, rma["ticket_id"]))
    return re.sub(r"\n{3,}", "\n\n", text).strip()


@portal_bp.route("/final-reports")
def final_reports():
    if not db.available():
        return render_template("portal/final_reports.html", queue=None), 503
    return render_template("portal/final_reports.html", queue=repair_db.final_report_queue())


@portal_bp.route("/t/<int:ticket_id>/final-report")
def final_report(ticket_id):
    if not db.available():
        return render_template("error.html", ticket_id=ticket_id, message="Database not configured."), 503
    rma = repair_db.get_rma(ticket_id)
    if not rma:
        flash(f"Ticket #{ticket_id} isn't in the repair queue.", "error")
        return _back(ticket_id)
    try:
        ticket, requester, _org = _get_zendesk().get_ticket(ticket_id)
    except ZendeskError as exc:
        return render_template("error.html", ticket_id=ticket_id, message=str(exc)), 502
    saved = repair_db.get_final_report(ticket_id)
    return render_template(
        "portal/final_report.html",
        ticket=ticket, requester=requester, rma=rma, saved=saved,
        body=_report_draft(rma, saved),
        closed=ticket.get("status") == "closed",
        ready=rma["stage"] == "shipped",
        tech=auth.current_tech(),
        coverage_words=COVERAGE_WORDS,
    )


def _report_body(form):
    body = (form.get("body") or "").replace("\r\n", "\n").strip()
    if len(body) < 20:
        raise _Stop("The report is empty. Rebuild it from the repair record or write it first.")
    if len(body) > 20000:
        raise _Stop("The report is too long for one Zendesk comment.")
    return body


@portal_bp.route("/t/<int:ticket_id>/final-report/save", methods=["POST"])
def save_final_report(ticket_id):
    if not db.available():
        return _back(ticket_id)
    tech = _need_tech()
    if tech is None:
        return redirect(url_for("portal.tech_signin", next=url_for("portal.final_report", ticket_id=ticket_id)))
    if not repair_db.get_rma(ticket_id):
        return _back(ticket_id)
    here = redirect(url_for("portal.final_report", ticket_id=ticket_id))
    if request.form.get("op") == "rebuild":
        repair_db.save_final_report(ticket_id, None, tech)
        flash("Rebuilt the report from the repair record (your edits were discarded).", "success")
        return here
    if request.form.get("op") == "reopen":
        repair_db.reopen_final_report(ticket_id)
        flash("You can edit and send a corrected report.", "success")
        return here
    try:
        repair_db.save_final_report(ticket_id, _report_body(request.form), tech)
    except _Stop as exc:
        flash(str(exc), "error")
        return here
    flash("Draft saved. Nothing was sent.", "success")
    return here


@portal_bp.route("/t/<int:ticket_id>/final-report/preview", methods=["POST"])
def preview_final_report(ticket_id):
    blocked = _preview_guard()
    if blocked:
        return blocked
    try:
        body = _report_body(request.form)
        saved = repair_db.get_final_report(ticket_id)
        if saved and saved.get("sent_at"):
            raise _Stop("This report was already sent. Use “Send a corrected report” first.")
    except _Stop as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": True, "title": "Final repair report", "posts": [{"public": True, "body": body, "files": []}]}


@portal_bp.route("/t/<int:ticket_id>/final-report/send", methods=["POST"])
def send_final_report(ticket_id):
    if not db.available():
        return _back(ticket_id)
    tech = _need_tech()
    if tech is None:
        return redirect(url_for("portal.tech_signin", next=url_for("portal.final_report", ticket_id=ticket_id)))
    here = redirect(url_for("portal.final_report", ticket_id=ticket_id))
    if not repair_db.get_rma(ticket_id):
        return _back(ticket_id)
    try:
        body = _report_body(request.form)
    except _Stop as exc:
        flash(str(exc), "error")
        return here
    posted = False
    try:
        with repair_db.ticket_lock(ticket_id) as conn:
            if repair_db.report_sent(conn, ticket_id):
                flash("This report was already sent. Not sent again.", "error")
                return here
            repair_db.save_final_report(ticket_id, body, tech)  # keep the text even if Zendesk refuses it
            try:
                _get_zendesk().post_update(ticket_id, body, public=True, tags=[portal_tag(), "rp-final-report"])
            except ZendeskError as exc:
                flash(f"Zendesk rejected the report, so it was not sent (your edits are saved): {exc}", "error")
                return here
            posted = True
            repair_db.record_report_sent(conn, ticket_id, body, tech)
    except Exception:
        if not posted:
            raise
        logger.exception("Final report posted to Zendesk but not saved for ticket %s", ticket_id)
        flash("The report was posted to Zendesk, but saving that failed. Tell IT; do not send it again.", "error")
        return here
    flash(f"Sent the final repair report on ticket #{ticket_id}. Zendesk will email the customer.", "success")
    return redirect(url_for("portal.final_reports"))
