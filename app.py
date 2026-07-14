"""Flask app: look up a Zendesk ticket and render a printable one-page summary sheet."""

import os
import re
from datetime import datetime, timezone

from dotenv import load_dotenv
from flask import Flask, redirect, render_template, request, url_for

from summarizer import build_transcript, summarize_ticket
from zendesk_client import ZendeskClient, ZendeskError, resolve_custom_fields

load_dotenv()

REQUIRED_ENV_VARS = ["ZENDESK_SUBDOMAIN", "ZENDESK_EMAIL", "ZENDESK_API_TOKEN", "OPENAI_API_KEY"]

app = Flask(__name__)
_client = None


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

    return render_template(
        "ticket_sheet.html",
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
