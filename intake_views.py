"""Customer repair-intake form (public, reached by the emailed link) and the
staff buttons that send a link or retry the write-back to Zendesk.

The public pages are the only part of the portal a customer sees. They never
look at the tech or Settings sign-in, and they log ``RI-<id>``, never the link
token. The logic and SQL are in intake.py.
"""

from __future__ import annotations

import hashlib
import logging
import threading
import time

from flask import Blueprint, flash, g, redirect, render_template, request, url_for

import db
import intake
import portal_views

logger = logging.getLogger("intake")

intake_bp = Blueprint("intake", __name__)

PUBLIC_PREFIX = "/repair/intake/"
_PUBLIC_ENDPOINTS = ("intake.customer_form", "intake.customer_submit")

# A basic brake, kept in memory per gunicorn worker. Behind the reverse proxy
# every customer shares one address, so the address limit only counts requests
# for links that don't exist (guessing): a real link is never locked out by it.
LIMIT_WINDOW = 600
LIMIT_ADDR = 120        # dead-link requests per window per remote address
LIMIT_TOKEN_POSTS = 10  # form posts per window per link
_hits: dict[str, list[float]] = {}
_hits_lock = threading.Lock()

REFRESHED = ("Something went wrong sending the form. Your answers are still here; "
             "re-attach any photos and press Send again.")
_GONE = ("This link is no longer active",
         "It may have expired or been replaced by a newer one. Just reply to the email we sent you "
         "and we'll send you a fresh link.")
_SLOW_DOWN = ("Please wait a few minutes",
              "This page has been opened or sent a lot in a short time. Give it ten minutes and try again, "
              "or just reply to the email we sent you.")
_ANSWERED = ("submitted", "synced", "sync_failed")


# ---------------------------------------------------------------- pages


def _closed(title, message, status):
    return render_template("intake/closed.html", title=title, message=message), status


def _form(row, values, errors, status=200, notice=None):
    return render_template(
        "intake/form.html", values=values, errors=errors, notice=notice,
        concerns=_concern_choices(),
        spec={name: {"label": label, "required": required, "maxlen": maxlen}
              for name, label, required, maxlen in intake.FIELDS},
        max_photos=intake.MAX_PHOTOS, max_photo_mb=intake.MAX_PHOTO_BYTES // (1024 * 1024),
    ), status


def _concern_choices():
    try:
        return intake.concern_choices()
    except Exception:  # the tick boxes are a nicety; never lose the form over them
        logger.exception("Could not load the intake concern list")
        return []


def _answers(form):
    """Cleaned answers plus the ticked concerns, and any validation errors."""
    answers, errors = intake.validate(form)
    answers["concerns"] = intake.pick_concerns(form.getlist("concerns"), _concern_choices())
    return answers, errors


def _done(row):
    answers = row.get("answers") or {}
    labels = {name: label for name, label, _required, _maxlen in intake.FIELDS}
    shown = [(labels[name], answers[name]) for name in ("model", "serial", "caliber", "issue", "check_specific")
             if answers.get(name)]
    if answers.get("concerns"):
        shown.insert(min(3, len(shown)), (intake.CONCERNS_LABEL, ", ".join(answers["concerns"])))
    return render_template("intake/done.html", shown=shown, photo_count=answers.get("photo_count") or row.get("file_count") or 0)


def _no_such_link():
    """The dead-link page, or 'slow down' for an address that keeps asking for links that don't exist."""
    if _over_limit("addr:" + (request.remote_addr or "unknown"), LIMIT_ADDR):
        return _closed(*_SLOW_DOWN, 429)
    return _closed(*_GONE, 404)


def _resolve(token):
    """(row, None) for a link that shows the form or the confirmation, else (None, friendly page)."""
    if not intake.TOKEN_RE.match(token or ""):
        return None, _no_such_link()
    down = not db.available()
    if not down:
        try:
            row = intake.find_by_token(token)
        except Exception:
            logger.exception("Intake form lookup failed")
            down = True
    if down:
        return None, _closed("We can't open the form right now",
                             "Please try again in a few minutes. If it keeps happening, just reply to the "
                             "email we sent you and tell us about your rifle there.", 503)
    if row is None:
        return None, _no_such_link()
    if row["status"] in _ANSWERED or intake.is_open(row):
        return row, None
    return None, _closed(*_GONE, 410)


def _prefill(row):
    """What we already know from the ticket, so the customer only confirms it."""
    return {"model": row.get("zd_model") or "", "serial": row.get("zd_serial") or "",
            "caliber": row.get("zd_caliber") or ""}


# ---------------------------------------------------------------- public routes


@intake_bp.route("/repair/intake/<string:token>", methods=["GET"])
def customer_form(token):
    row, closed = _resolve(token)
    if closed:
        return closed
    if row["status"] in _ANSWERED:
        return _done(row)
    return _form(row, _prefill(row), {})


@intake_bp.route("/repair/intake/<string:token>", methods=["POST"])
def customer_submit(token):
    row = g.pop("intake_row", None)  # guard_request already looked it up
    if row is None:
        row, closed = _resolve(token)
        if closed:
            return closed
    if row["status"] in _ANSWERED:
        return _done(row)

    answers, errors = _answers(request.form)
    uploads, photo_error = intake.read_photos(request.files.getlist("photos"))
    if photo_error:
        errors["photos"] = photo_error
    if errors:
        return _form(row, answers, errors, status=422)

    here = redirect(url_for("intake.customer_form", token=token), code=303)
    if not intake.save_submission(row["id"], answers, uploads, token_hash=row["token_hash"]):
        return here  # sent twice, or the link ran out meanwhile: the GET shows which
    logger.info("RI-%s: intake form saved for ticket %s (%s files)", row["id"], row["ticket_id"], len(uploads))
    # One try at Zendesk now. The answers are saved either way, and the sweep or
    # a tech retries, so the customer never sees a Zendesk problem.
    try:
        intake.sync(portal_views._get_zendesk(), row["id"])
    except Exception:
        logger.exception("RI-%s: first write-back to Zendesk failed", row["id"])
    return here


@intake_bp.after_request
def _public_headers(response):
    # Also covers pages answered by guard_request and the app's 400/413
    # handlers: the endpoint is matched before any of those run.
    if request.endpoint in _PUBLIC_ENDPOINTS:
        response.headers["Cache-Control"] = "no-store"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Robots-Tag"] = "noindex"
    return response


# ---------------------------------------------------------------- guards (registered in app.py)


def _over_limit(key, limit):
    """Count one hit for ``key``; True once it has had ``limit`` in the window."""
    now = time.time()
    with _hits_lock:
        if len(_hits) > 5000:  # forget idle keys so the table can't grow without end
            for stale in [k for k, hits in _hits.items() if not hits or now - hits[-1] > LIMIT_WINDOW]:
                del _hits[stale]
        hits = [t for t in _hits.get(key, []) if now - t < LIMIT_WINDOW]
        over = len(hits) >= limit
        if not over:
            hits.append(now)
        _hits[key] = hits
        return over


def guard_request():
    """before_request hook for the public form. Must run before auth.check_csrf.

    check_csrf reads request.form, which parses the whole upload. For the
    customer form this hook first turns away floods, oversized posts and
    posts to links that don't exist, so none of those get that far.
    """
    if not request.path.startswith(PUBLIC_PREFIX):
        return None
    token = request.path[len(PUBLIC_PREFIX):]
    slow_down = _SLOW_DOWN
    if request.method != "POST":
        return None
    if _over_limit("post:" + hashlib.sha256(token.encode("utf-8")).hexdigest()[:16], LIMIT_TOKEN_POSTS):
        return _closed(*slow_down, 429)
    # A chunked post declares no length, so its size can't be checked before
    # it is read: refuse it. (No length and not chunked means an empty body.)
    length = request.content_length
    if (length is None and request.headers.get("Transfer-Encoding")) or (length or 0) > intake.MAX_POST_BYTES:
        return too_large_page(None)
    row, closed = _resolve(token)
    if closed:
        return closed
    g.intake_row = row
    return None


def too_large_page(_exc):
    megabytes = intake.MAX_POST_BYTES // (1024 * 1024)
    return _closed(
        "Those files are too big to send together",
        f"Go back and attach fewer or smaller photos: up to {intake.MAX_PHOTOS} files, "
        f"{intake.MAX_PHOTO_BYTES // (1024 * 1024)} MB each and {megabytes} MB in total. "
        "You can also reply to the email we sent you with the rest.", 413)


def bad_request_page(_exc):
    """A 400 on the customer form, nearly always a CSRF token that went stale
    while the page sat open. Show the form again with what they typed."""
    token = request.path[len(PUBLIC_PREFIX):]
    row, closed = _resolve(token)
    if closed:
        return closed
    if row["status"] in _ANSWERED:
        return _done(row)
    try:
        values, _errors = _answers(request.form)
    except Exception:  # the body itself could not be read
        values = _prefill(row)
    return _form(row, values, {}, status=400, notice=REFRESHED)


# ---------------------------------------------------------------- staff routes


@intake_bp.route("/t/<int:ticket_id>/intake/send", methods=["POST"])
def intake_send(ticket_id):
    if not db.available():
        return portal_views._back(ticket_id)
    tech = portal_views._need_tech()
    if tech is None:
        return portal_views._back(ticket_id)
    zendesk = portal_views._get_zendesk()
    try:
        ticket, requester, _org = zendesk.get_ticket(ticket_id)
    except portal_views.ZendeskError as exc:
        flash(f"Could not read ticket #{ticket_id} from Zendesk: {exc}", "error")
        return portal_views._back(ticket_id)
    outcome = intake.resend_link(zendesk, ticket, requester, tech=tech, manual=True)
    if outcome == "sent":
        flash("Sent the customer a link to the intake form. Zendesk will email it to them.", "success")
    elif outcome == "failed":
        row = intake.for_ticket(ticket_id) or {}
        flash(f"Zendesk rejected the update, so the link was not sent: {row.get('last_error') or 'unknown error'}",
              "error")
    else:
        flash({
            "submitted": "The customer has already sent the form, so no new link was sent.",
            "ticket_done": "The ticket is solved or closed in Zendesk, so the customer can't be emailed on it.",
            "no_base_url": "No link was sent: set REPAIR_INTAKE_BASE_URL to the public "
                           "address of the form first.",
            "no_rma": f"Ticket #{ticket_id} isn't in the repair queue. Add it first.",
        }.get(outcome, f"No link was sent ({outcome})."), "error")
    return portal_views._back(ticket_id)


@intake_bp.route("/t/<int:ticket_id>/intake/retry", methods=["POST"])
def intake_retry(ticket_id):
    if not db.available() or portal_views._need_tech() is None:
        return portal_views._back(ticket_id)
    row = intake.for_ticket(ticket_id)
    if row is None:
        flash("This ticket has no intake form.", "error")
        return portal_views._back(ticket_id)
    outcome = intake.sync(portal_views._get_zendesk(), row["id"], force=True)
    if outcome == "synced":
        flash("The customer's intake form is on the Zendesk ticket now.", "success")
    elif outcome == "already":
        flash("The customer's intake form was already on the Zendesk ticket.", "success")
    elif outcome == "failed":
        after = intake.for_ticket(ticket_id) or {}
        flash(f"Zendesk still won't take the intake form: {after.get('last_error') or 'unknown error'}", "error")
    elif row["status"] in ("submitted", "sync_failed"):
        flash("Another attempt is running right now. Check back in a few minutes.", "error")
    else:
        flash("There is nothing to send: the customer hasn't filled in the form.", "error")
    return portal_views._back(ticket_id)


@intake_bp.route("/intake/retry-failed", methods=["POST"])
def intake_retry_failed():
    if not db.available() or portal_views._need_tech() is None:
        return redirect(url_for("portal.queue"))
    counts = intake.sweep(portal_views._get_zendesk(), force=True)
    words = {"synced": "written to Zendesk", "failed": "still failing", "links": "links sent",
             "expired": "links expired", "reminded": "reminders sent"}
    if not counts:
        flash("No intake forms needed a retry.", "success")
    else:
        parts = [f"{counts[k]} {words.get(k, k)}" for k in words if counts.get(k)]
        flash("Intake forms: " + ", ".join(parts) + ".", "error" if counts.get("failed") else "success")
    return redirect(url_for("portal.queue"))
