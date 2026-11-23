"""Scan portal: open a ticket from a barcode and post quick-action comments.

Zendesk owns the workflow. This blueprint only reads the ticket, posts the
configured public comment, and appends tags so tagged tickets can be reported
on later. Local state is just the button configuration and an audit log.
"""

import logging
import os
import re
from collections import Counter

from flask import (
    Blueprint,
    flash,
    redirect,
    render_template,
    request,
    session,
    url_for,
)

import db
from zendesk_client import ZendeskClient, ZendeskError

logger = logging.getLogger(__name__)

portal_bp = Blueprint("portal", __name__)

NOTE_EXCERPT_CHARS = 200

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
    """The base tag stamped on every ticket the portal touches."""
    return os.environ.get("PORTAL_TAG", "repair-portal")


def _agent_url(ticket_id):
    subdomain = os.environ.get("ZENDESK_SUBDOMAIN", "")
    return f"https://{subdomain}.zendesk.com/agent/tickets/{ticket_id}"


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


# ---------------------------------------------------------------- ticket actions


@portal_bp.route("/t/<int:ticket_id>")
def action_page(ticket_id):
    try:
        ticket, requester, _org = _get_zendesk().get_ticket(ticket_id)
    except ZendeskError as exc:
        status = 404 if exc.status == 404 else 502
        return render_template("error.html", ticket_id=ticket_id, message=str(exc)), status

    actions, history, db_error = [], [], None
    if db.available():
        actions = db.list_quick_actions()
        history = db.recent_actions(ticket_id)
    else:
        db_error = "Database not configured — quick actions are unavailable."

    return render_template(
        "portal/action.html",
        ticket=ticket,
        requester=requester,
        actions=actions,
        history=history,
        db_error=db_error,
        initials=session.get("initials", ""),
        agent_url=_agent_url(ticket_id),
        base_tag=portal_tag(),
    )


@portal_bp.route("/t/<int:ticket_id>/action", methods=["POST"])
def post_action(ticket_id):
    if not db.available():
        flash("Database not configured — cannot post actions.", "error")
        return redirect(url_for("portal.action_page", ticket_id=ticket_id))

    initials = (request.form.get("initials") or "").strip()[:10]
    session["initials"] = initials

    action_id = request.form.get("action_id")
    note = (request.form.get("note") or "").strip()

    if action_id:
        action = db.get_quick_action(int(action_id))
        if not action or not action["active"]:
            flash("That quick action no longer exists.", "error")
            return redirect(url_for("portal.action_page", ticket_id=ticket_id))
        body, public = action["comment_body"], True
        tags = [portal_tag(), action["tag"]]
        label = action["label"]
        excerpt = None
    elif note:
        public = request.form.get("note_visibility") == "public"
        body = note
        tags = [portal_tag()]
        label = "Public note" if public else "Internal note"
        excerpt = note[:NOTE_EXCERPT_CHARS]
    else:
        flash("Pick a quick action or write a note first.", "error")
        return redirect(url_for("portal.action_page", ticket_id=ticket_id))

    try:
        client = _get_zendesk()
        client.add_comment(ticket_id, body, public=public)
        client.add_tags(ticket_id, tags)
    except ZendeskError as exc:
        flash(f"Zendesk rejected the update: {exc}", "error")
        return redirect(url_for("portal.action_page", ticket_id=ticket_id))

    try:
        db.log_action(ticket_id, label, excerpt, initials or None)
    except Exception:  # the Zendesk write succeeded; a lost log line is not fatal
        logger.exception("Could not write portal.action_log for ticket %s", ticket_id)

    kind = "comment" if public else "internal note"
    flash(f"Posted “{label}” as a {kind} on ticket #{ticket_id}.", "success")
    return redirect(url_for("portal.action_page", ticket_id=ticket_id))


# ---------------------------------------------------------------- admin


@portal_bp.route("/admin/actions", methods=["GET", "POST"])
def admin_actions():
    if not db.available():
        return render_template("portal/admin.html", actions=None), 503

    if request.method == "POST":
        form = request.form
        if form.get("op") == "create":
            label = (form.get("label") or "").strip()
            body = (form.get("comment_body") or "").strip()
            tag = _clean_tag(form.get("tag"))
            if label and body and tag:
                db.create_quick_action(label, body, tag)
                flash(f"Added “{label}”.", "success")
            else:
                flash("Label, comment text, and tag are all required.", "error")
        elif form.get("op") == "update":
            action_id = int(form["id"])
            label = (form.get("label") or "").strip()
            body = (form.get("comment_body") or "").strip()
            tag = _clean_tag(form.get("tag"))
            if label and body and tag:
                db.update_quick_action(
                    action_id,
                    label,
                    body,
                    tag,
                    int(form.get("position") or 0),
                    form.get("active") == "on",
                )
                flash(f"Saved “{label}”.", "success")
            else:
                flash("Label, comment text, and tag are all required.", "error")
        return redirect(url_for("portal.admin_actions"))

    return render_template(
        "portal/admin.html",
        actions=db.list_quick_actions(include_inactive=True),
        base_tag=portal_tag(),
    )


def _clean_tag(raw):
    """Zendesk tags: lowercase, no spaces. Normalize rather than reject."""
    tag = re.sub(r"[^a-z0-9_\-.]+", "-", (raw or "").strip().lower()).strip("-")
    return tag


# ---------------------------------------------------------------- report


@portal_bp.route("/report")
def report():
    tag = portal_tag()
    try:
        tickets = list(_get_zendesk().search_export(f"tags:{tag}"))
    except ZendeskError as exc:
        return render_template("error.html", ticket_id=None, message=str(exc)), 502

    action_tags = {}
    if db.available():
        action_tags = {a["tag"]: a["label"] for a in db.list_quick_actions(include_inactive=True)}

    status_counts = Counter(t.get("status") or "unknown" for t in tickets)
    tag_counts = Counter()
    rows = []
    for t in sorted(tickets, key=lambda t: t.get("updated_at") or "", reverse=True):
        hit_tags = [tg for tg in (t.get("tags") or []) if tg in action_tags or tg == tag]
        for tg in hit_tags:
            if tg != tag:
                tag_counts[tg] += 1
        rows.append(
            {
                "id": t.get("id"),
                "subject": t.get("subject"),
                "status": t.get("status"),
                "updated_at": t.get("updated_at"),
                "portal_tags": [action_tags.get(tg, tg) for tg in hit_tags if tg != tag],
                "agent_url": _agent_url(t.get("id")),
            }
        )

    return render_template(
        "portal/report.html",
        rows=rows,
        status_counts=dict(status_counts.most_common()),
        tag_counts=[(action_tags.get(tg, tg), n) for tg, n in tag_counts.most_common()],
        base_tag=tag,
        total=len(rows),
    )
