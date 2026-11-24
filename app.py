"""Flask app: look up a Zendesk ticket and render a printable one-page summary sheet."""

import logging
import os
import re
import secrets
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from flask import Flask, redirect, render_template, request, url_for

import barcodes
from summarizer import build_transcript, summarize_ticket
from repair_db import REPORT_TZ
from zendesk_client import ZendeskClient, ZendeskError, resolve_custom_fields

load_dotenv()

REQUIRED_ENV_VARS = ["ZENDESK_SUBDOMAIN", "ZENDESK_EMAIL", "ZENDESK_API_TOKEN", "OPENAI_API_KEY"]

app = Flask(__name__)
_client = None

# Scan portal (queue, stage screens, settings, report). The ticket-print routes
# must keep working even if the portal database is not configured.
import auth
import db
from admin_views import admin_bp
from portal_views import portal_bp
from report_views import report_bp
from atf_views import atf_bp
import intake_views
from intake_views import intake_bp

app.config["MAX_CONTENT_LENGTH"] = 120 * 1024 * 1024  # several phone photos per post
app.register_blueprint(portal_bp)
app.register_blueprint(admin_bp)
app.register_blueprint(report_bp)
app.register_blueprint(atf_bp)
app.register_blueprint(intake_bp)


def _public_only():
    """PORTAL_PUBLIC_ONLY=1: this copy faces the internet (e.g. repairs.example.com)
    and serves the customer intake form and nothing else. The staff portal runs
    as a second copy, without the setting, on the same database."""
    if os.environ.get("PORTAL_PUBLIC_ONLY", "").strip().lower() not in ("1", "true", "yes", "on"):
        return None
    path = request.path
    if path.startswith(intake_views.PUBLIC_PREFIX) or path.startswith("/static/") or path == "/healthz":
        return None
    return "Not found", 404, {"Content-Type": "text/plain; charset=utf-8"}


app.before_request(_public_only)
# Before check_csrf, which parses the whole upload: the customer form's guard
# turns away floods, oversized posts and dead links first.
app.before_request(intake_views.guard_request)
app.before_request(auth.check_csrf)


def _reports_waiting():
    """How many final repair reports are ready to review (nav badge); 0 if unknown."""
    if not db.available():
        return 0
    try:
        import repair_db

        return repair_db.final_reports_ready_count()
    except Exception:  # never break a page over the badge
        return 0


@app.context_processor
def _portal_context():
    return {
        "csrf_field": auth.csrf_field,
        "current_tech": auth.current_tech,
        "admin_locked": bool(auth.admin_password()),
        "reports_waiting": _reports_waiting,
    }

_dsn = os.environ.get("DATABASE_URL", "") or os.environ.get("RTM_DATABASE_URL", "")
if _dsn:
    db.init_pool(_dsn)
else:
    logging.getLogger(__name__).warning(
        "DATABASE_URL is not set; the repair portal will be unavailable."
    )

# Session signing key (sign-ins, CSRF, flash). Never a guessable default:
# FLASK_SECRET_KEY if set, else a random key kept in the database so every
# gunicorn worker shares it, else a per-process random key (one-pager only).
app.secret_key = (
    os.environ.get("FLASK_SECRET_KEY")
    or (db.shared_secret() if db.available() else None)
    or secrets.token_urlsafe(48)
)


def missing_config():
    return [name for name in REQUIRED_ENV_VARS if not os.environ.get(name)]


def get_client():
    global _client
    if _client is None:
        _client = ZendeskClient(
            subdomain=os.environ["ZENDESK_SUBDOMAIN"],
            email=os.environ["ZENDESK_EMAIL"],
            api_token=os.environ["ZENDESK_API_TOKEN"],
        )
    return _client


@app.template_filter("datefmt")
def datefmt(value):
    """Format a Zendesk ISO 8601 timestamp as a readable local-agnostic date."""
    if not value:
        return "—"
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return value
    return parsed.strftime("%b %d, %Y %H:%M UTC")


@app.template_filter("initials")
def initials(name):
    """'Kyler Smith' -> 'KS'; a single name keeps its first two letters."""
    parts = [p for p in re.split(r"[\s.\-]+", name or "") if p]
    if not parts:
        return ""
    if len(parts) == 1:
        return parts[0][:2].upper()
    return (parts[0][0] + parts[-1][0]).upper()


@app.errorhandler(413)
def too_large(_exc):
    if request.path.startswith(intake_views.PUBLIC_PREFIX):
        return intake_views.too_large_page(_exc)
    return render_template(
        "error.html", ticket_id=None, message="Those files are too big to upload together (120 MB max). Attach fewer at a time."
    ), 413


@app.errorhandler(400)
def bad_request(exc):
    """The customer form gets its typed answers back; every other page keeps the plain 400."""
    if request.path.startswith(intake_views.PUBLIC_PREFIX):
        return intake_views.bad_request_page(exc)
    return exc


@app.template_filter("localtime")
def localtime(value, fmt="%b %d %I:%M %p"):
    """Show a database timestamp (UTC) in shop-local time."""
    if not value:
        return "—"
    return value.astimezone(ZoneInfo(REPORT_TZ)).strftime(fmt)


@app.route("/healthz")
def healthz():
    return "ok", 200, {"Content-Type": "text/plain; charset=utf-8"}


@app.route("/")
def index():
    return render_template("index.html", missing=missing_config(), error=request.args.get("error"))


@app.route("/lookup")
def lookup():
    raw = (request.args.get("ticket_id") or "").strip()
    match = re.search(r"(\d+)\s*/?\s*$", raw)  # accepts a bare ID or an agent URL ending in the ID
    if not match:
        return redirect(url_for("index", error="Enter a numeric ticket ID or paste a Zendesk ticket URL."))
    return redirect(url_for("ticket_sheet", ticket_id=int(match.group(1))))


@app.route("/ticket/<int:ticket_id>")
def ticket_sheet(ticket_id):
    missing = missing_config()
    if missing:
        return render_template("index.html", missing=missing, error=None), 503

    client = get_client()
    try:
        ticket, requester, organization = client.get_ticket(ticket_id)
        comments, users_by_id = client.get_comments(ticket_id)
        fields_map = client.get_ticket_fields()
        form = client.get_ticket_form(ticket.get("ticket_form_id"))
    except ZendeskError as exc:
        status = 404 if exc.status == 404 else 502
        return render_template("error.html", ticket_id=ticket_id, message=str(exc)), status

    custom_fields = resolve_custom_fields(ticket, fields_map, form)
    transcript = build_transcript(comments, users_by_id)

    summary = None
    summary_error = None
    try:
        summary = summarize_ticket(ticket, custom_fields, transcript)
    except Exception as exc:  # render the sheet even if OpenAI fails
        summary_error = f"AI summary unavailable: {exc}"

    portal_url = url_for("portal.action_page", ticket_id=ticket_id, _external=True)
    base = os.environ.get("PORTAL_BASE_URL", "").rstrip("/")
    if base:  # printed QR should point at the deployed portal, not the dev host
        portal_url = f"{base}/t/{ticket_id}"

    return render_template(
        "ticket_sheet.html",
        barcode_svg=barcodes.code128_svg(ticket_id),
        qr_svg=barcodes.qr_svg(portal_url),
        ticket=ticket,
        requester=requester,
        organization=organization,
        form=form,
        custom_fields=custom_fields,
        summary=summary,
        summary_error=summary_error,
        comment_count=len(comments),
        generated_at=datetime.now(timezone.utc),
    )


if __name__ == "__main__":
    app.run(debug=True, port=5000)
