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

from flask import Blueprint, flash, redirect, render_template, request, url_for

import auth
import db
import repair_db
from zendesk_client import ZendeskClient, ZendeskError

logger = logging.getLogger(__name__)

portal_bp = Blueprint("portal", __name__)

NOTE_EXCERPT_CHARS = 200
QUEUED_TAG = "rp-queued"  # added in Zendesk when a ticket joins the queue (keeps Incoming short)
DUPLICATE_WINDOW_SECONDS = 120  # a double-tap must not email the customer twice
TRACKING_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 \-]{4,63}$")

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

    return re.sub(r"\{(\w+)\}", sub, template)


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


@portal_bp.route("/scan/go")
def scan_go():
    raw = (request.args.get("ticket_id") or "").strip()
    match = re.search(r"(\d+)\s*/?\s*$", raw)  # bare ID, scanned digits, or agent URL
    if not match:
        return redirect(url_for("portal.scan", error="Scan or type a numeric ticket ID."))
    return redirect(url_for("portal.action_page", ticket_id=int(match.group(1))))


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
    try:
        ticket, requester, _org = _get_zendesk().get_ticket(ticket_id)
    except ZendeskError as exc:
        status = 404 if exc.status == 404 else 502
        return render_template("error.html", ticket_id=ticket_id, message=str(exc)), status

    ctx = {
        "ticket": ticket,
        "requester": requester,
        "agent_url": _agent_url(ticket_id),
        "base_tag": portal_tag(),
        "tech": auth.current_tech(),
        "db_error": None,
        "rma": None,
        "stages": [],
        "problems": [],
        "solutions": [],
        "actions": [],
        "history": [],
        "legacy_history": [],
    }
    if not db.available():
        ctx["db_error"] = "Database not configured. Stage buttons are unavailable."
        return render_template("portal/action.html", **ctx)

    rma = repair_db.get_rma(ticket_id)
    ctx.update(
        rma=rma,
        stages=repair_db.list_stages(),
        problems=repair_db.list_choices("problem"),
        solutions=repair_db.list_choices("solution"),
        actions=db.list_quick_actions(),
        history=repair_db.events_for(ticket_id) if rma else [],
        legacy_history=db.recent_actions(ticket_id),
        next_stage=_next_stage(rma["stage"]) if rma else None,
        current_index=repair_db.stage_index(rma["stage"]) if rma else -1,
    )
    return render_template("portal/action.html", **ctx)


def _next_stage(current):
    """The stage a tech most likely presses next (its form opens by default)."""
    order = repair_db.BOARD_STAGES
    if current not in order:
        return None
    i = order.index(current)
    return order[i + 1] if i + 1 < len(order) else None


@portal_bp.route("/t/<int:ticket_id>/stage/<key>", methods=["POST"])
def post_stage(ticket_id, key):
    if not db.available():
        flash("Database not configured.", "error")
        return _back(ticket_id)
    tech = _need_tech()
    if tech is None:
        return redirect(url_for("portal.tech_signin", next=url_for("portal.action_page", ticket_id=ticket_id)))
    stage = repair_db.get_stage(key)
    if stage is None:
        flash("Unknown stage.", "error")
        return _back(ticket_id)
    rma = repair_db.get_rma(ticket_id)
    if rma is None:
        flash("Add this ticket to the repair queue first.", "error")
        return _back(ticket_id)

    form = request.form
    moving_back = repair_db.stage_index(key) < repair_db.stage_index(rma["stage"])
    if moving_back and form.get("move_back") != "1":
        flash(
            f"This RMA is already past “{stage['label']}”. Tick “Move back to this stage” "
            "if you really mean to go back (the customer is emailed again).",
            "error",
        )
        return _back(ticket_id)
    record_only = form.get("record_only") == "1"

    fields, values = {}, {}
    if key == "inspected":
        problem = _pick_choice(form.get("problem_id"), "problem")
        if problem is None:
            flash("Pick the problem found.", "error")
            return _back(ticket_id)
        fields = {
            "problem_id": problem["id"],
            "problem_notes": (form.get("problem_notes") or "").strip() or None,
        }
        values = {"problem": problem["label"]}
    elif key == "repaired":
        solution = _pick_choice(form.get("solution_id"), "solution")
        if solution is None:
            flash("Pick the repair done.", "error")
            return _back(ticket_id)
        work_done = (form.get("work_done") or "").strip()
        fields = {"solution_id": solution["id"], "work_done": work_done or None}
        values = {"solution": solution["label"], "work_done": work_done or solution["label"]}
    elif key == "shipped":
        tracking = (form.get("tracking_number") or "").strip()
        if not TRACKING_RE.match(tracking):
            flash("Scan or type the return tracking number.", "error")
            return _back(ticket_id)
        fields = {"tracking_number": tracking}
        values = {"tracking": tracking}

    # Placeholders from earlier stages stay available to later messages.
    values.setdefault("problem", rma.get("problem_label") or "")
    values.setdefault("solution", rma.get("solution_label") or "")
    values.setdefault("work_done", rma.get("work_done") or rma.get("solution_label") or "")
    values.setdefault("tracking", rma.get("tracking_number") or "")

    message = render_message(stage["comment_body"], values)
    custom_fields = {}
    if stage_field_id() and stage.get("zendesk_value"):
        custom_fields[stage_field_id()] = stage["zendesk_value"]

    zendesk = _get_zendesk()
    if record_only:
        # Only for tickets Zendesk has closed (no comments allowed): keep the
        # repair record moving and tell CS to contact the customer another way.
        try:
            ticket, _req, _org = zendesk.get_ticket(ticket_id)
        except ZendeskError as exc:
            flash(f"Could not read ticket #{ticket_id} from Zendesk: {exc}", "error")
            return _back(ticket_id)
        if ticket.get("status") != "closed":
            flash("This ticket is open in Zendesk, so post the stage normally.", "error")
            return _back(ticket_id)

    emailed = False
    try:
        # One ticket at a time: a double tap waits here, then finds the first
        # post's event below and stops, so the customer is emailed once.
        with repair_db.ticket_lock(ticket_id) as conn:
            if repair_db.recent_stage_event(conn, ticket_id, key, DUPLICATE_WINDOW_SECONDS):
                flash(f"“{stage['label']}” was just posted for this ticket. Not sent again.", "error")
                return _back(ticket_id)
            if not record_only:
                try:
                    zendesk.post_update(
                        ticket_id,
                        message,
                        public=True,
                        tags=[portal_tag(), f"rp-{key}"],
                        custom_fields=custom_fields,
                    )
                except ZendeskError as exc:
                    flash(f"Zendesk rejected the update, so nothing was sent or saved: {exc}", "error")
                    return _back(ticket_id)
                emailed = True
            repair_db.record_stage(
                conn, ticket_id, stage, tech, fields, None if record_only else message, rma["stage"]
            )
    except Exception:
        if not emailed:
            raise
        logger.exception("Stage %s posted to Zendesk but not saved for ticket %s", key, ticket_id)
        flash(
            f"The customer was emailed “{stage['label']}”, but saving the repair record failed. "
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
    else:
        flash(f"Posted “{stage['label']}” on ticket #{ticket_id}. Zendesk will email the customer.", "success")
    return _back(ticket_id)


def _pick_choice(raw, kind):
    try:
        choice = repair_db.get_choice(int(raw), kind)
    except (TypeError, ValueError):
        return None
    return choice if choice and choice["active"] else None


@portal_bp.route("/t/<int:ticket_id>/action", methods=["POST"])
def post_action(ticket_id):
    """Extra quick-action buttons and free-form notes (no stage change)."""
    if not db.available():
        flash("Database not configured. Cannot post actions.", "error")
        return _back(ticket_id)
    tech = _need_tech()
    if tech is None:
        return redirect(url_for("portal.tech_signin", next=url_for("portal.action_page", ticket_id=ticket_id)))

    action_id = request.form.get("action_id")
    note = (request.form.get("note") or "").strip()

    if action_id:
        try:
            action = db.get_quick_action(int(action_id))
        except ValueError:
            action = None
        if not action or not action["active"]:
            flash("That quick action no longer exists.", "error")
            return _back(ticket_id)
        body, public, kind = action["comment_body"], True, "action"
        tags = [portal_tag(), action["tag"]]
        label = action["label"]
        excerpt = None
    elif note:
        public = request.form.get("note_visibility") == "public"
        body, kind = note, "note"
        tags = [portal_tag()]
        label = "Public note" if public else "Internal note"
        excerpt = note[:NOTE_EXCERPT_CHARS]
    else:
        flash("Pick a quick action or write a note first.", "error")
        return _back(ticket_id)

    try:
        _get_zendesk().post_update(ticket_id, body, public=public, tags=tags)
    except ZendeskError as exc:
        flash(f"Zendesk rejected the update: {exc}", "error")
        return _back(ticket_id)

    try:
        if repair_db.get_rma(ticket_id):
            repair_db.record_event(
                ticket_id,
                kind,
                label,
                tech,
                detail={"note": excerpt} if excerpt else None,
                customer_message=body if public else None,
            )
        else:
            db.log_action(ticket_id, label, excerpt, tech["name"])
    except Exception:  # the Zendesk write succeeded; a lost log line is not fatal
        logger.exception("Could not log portal action for ticket %s", ticket_id)

    what = "a public reply" if public else "an internal note"
    flash(f"Posted “{label}” as {what} on ticket #{ticket_id}.", "success")
    return _back(ticket_id)
