"""Settings: stage messages, problem/solution lists, techs, and extra quick actions.

Protected by ADMIN_PASSWORD when it is set (see auth.py).
"""

import re

from psycopg.errors import UniqueViolation
from flask import Blueprint, flash, redirect, render_template, request, session, url_for

import auth
import db
import repair_db
import repair_settings
import tracking
import portal_views
from portal_views import intake_tag, portal_tag, stage_field_id
from zendesk_client import ZendeskError

admin_bp = Blueprint("admin", __name__, url_prefix="/admin")

PIN_RE = re.compile(r"^\d{4,8}$")
LIST_TITLES = (
    ("inspection_point", "Inspection checklist (Inspected)"),
    ("accessory", "Items received with it (Received)"),
    ("problem", "Problems found (Inspected)"),
    ("solution", "Repairs done (Repair complete)"),
    ("root_cause", "Root causes (Repair complete)"),
    ("concern", "Customer concerns (intake form tick boxes)"),
)


def _clean_tag(raw):
    """Zendesk tags: lowercase, no spaces. Normalize rather than reject."""
    return re.sub(r"[^a-z0-9_\-.]+", "-", (raw or "").strip().lower()).strip("-")


def _int(raw, default=0):
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


@admin_bp.route("/login", methods=["GET", "POST"])
def login():
    next_url = request.values.get("next") or url_for("admin.settings")
    if not next_url.startswith("/") or next_url.startswith("//"):
        next_url = url_for("admin.settings")
    if request.method == "POST":
        client = request.remote_addr or "?"
        if auth.try_admin_login(request.form.get("password") or "", client):
            return redirect(next_url)
        if auth.admin_login_locked(client):
            flash("Too many wrong passwords. Try again in 10 minutes.", "error")
        else:
            flash("Wrong password.", "error")
    return render_template("portal/login.html", next_url=next_url)


@admin_bp.route("/logout", methods=["POST"])
def logout():
    session.pop("admin", None)
    return redirect(url_for("portal.queue"))


@admin_bp.route("/", methods=["GET"])
@auth.admin_required
def settings():
    if not db.available():
        return render_template("portal/settings.html", ready=False), 503
    current = repair_settings.load()
    zd_fields, zd_error = {}, None
    try:
        zd_fields = portal_views._get_zendesk().get_ticket_fields()
    except ZendeskError as exc:
        zd_error = str(exc)
    resolved = repair_settings.resolve_roles(zd_fields, current)
    return render_template(
        "portal/settings.html",
        ready=True,
        stages=repair_db.list_stages(),
        lists=[
            (kind, title, repair_db.list_choices(kind, include_inactive=True))
            for kind, title in LIST_TITLES
        ],
        techs=repair_db.list_techs(include_inactive=True),
        settings=current,
        field_roles=repair_settings.FIELD_ROLES,
        zd_fields=zd_fields,
        zd_fields_error=zd_error,
        resolved=resolved,
        tracking_provider=tracking.provider_name(),
        tracking_ready=tracking.provider_ready(),
        actions=db.list_quick_actions(include_inactive=True),
        base_tag=portal_tag(),
        intake=intake_tag(),
        stage_field=stage_field_id(),
        admin_open=not auth.admin_password(),
    )


def _done(anchor):
    return redirect(url_for("admin.settings") + f"#{anchor}")


@admin_bp.route("/stage/<key>", methods=["POST"])
@auth.admin_required
def save_stage(key):
    if key not in repair_db.STAGE_KEYS:
        flash("Unknown stage.", "error")
        return _done("stages")
    label = (request.form.get("label") or "").strip()
    body = (request.form.get("comment_body") or "").strip()
    if not label or not body:
        flash("A stage needs a button label and a customer message.", "error")
        return _done("stages")
    repair_db.update_stage(key, label, body, (request.form.get("zendesk_value") or "").strip())
    flash(f"Saved “{label}”.", "success")
    return _done("stages")


@admin_bp.route("/choice", methods=["POST"])
@auth.admin_required
def save_choice():
    form = request.form
    kind = form.get("kind")
    if kind not in repair_db.CHOICE_KINDS:
        flash("Unknown list.", "error")
        return _done("lists")
    label = (form.get("label") or "").strip()
    if not label:
        flash("Enter a name for the list item.", "error")
        return _done(f"{kind}s")
    try:
        if form.get("op") == "create":
            repair_db.create_choice(kind, label)
            flash(f"Added “{label}”.", "success")
        else:
            repair_db.update_choice(
                _int(form.get("id")), label, _int(form.get("position")), form.get("active") == "on"
            )
            flash(f"Saved “{label}”.", "success")
    except UniqueViolation:
        flash(f"“{label}” is already on the list.", "error")
    return _done(f"{kind}s")


@admin_bp.route("/tech", methods=["POST"])
@auth.admin_required
def save_tech():
    form = request.form
    name = (form.get("name") or "").strip()
    pin = (form.get("pin") or "").strip()
    if not name:
        flash("Enter the person's name.", "error")
        return _done("techs")
    if pin and not PIN_RE.match(pin):
        flash("A PIN is 4 to 8 digits.", "error")
        return _done("techs")
    try:
        if form.get("op") == "create":
            if not pin:
                flash("Set a PIN for the new person.", "error")
                return _done("techs")
            repair_db.create_tech(name, pin)
            flash(f"Added {name}.", "success")
        else:
            repair_db.update_tech(_int(form.get("id")), name, form.get("active") == "on", pin or None)
            flash(f"Saved {name}{' with a new PIN' if pin else ''}.", "success")
    except UniqueViolation:
        flash(f"There is already someone named {name}.", "error")
    return _done("techs")


@admin_bp.route("/action", methods=["POST"])
@auth.admin_required
def save_action():
    form = request.form
    label = (form.get("label") or "").strip()
    body = (form.get("comment_body") or "").strip()
    tag = _clean_tag(form.get("tag"))
    if not (label and body and tag):
        flash("Label, comment text, and tag are all required.", "error")
        return _done("actions")
    if form.get("op") == "create":
        db.create_quick_action(label, body, tag, form.get("public") == "on")
        flash(f"Added “{label}”.", "success")
    else:
        db.update_quick_action(
            _int(form.get("id")),
            label,
            body,
            tag,
            _int(form.get("position")),
            form.get("active") == "on",
            form.get("public") == "on",
        )
        flash(f"Saved “{label}”.", "success")
    return _done("actions")


@admin_bp.route("/fields", methods=["POST"])
@auth.admin_required
def save_fields():
    """Which Zendesk ticket fields hold the serial, model, caliber... (blank = match by title)."""
    values = {}
    for role, _title, _pattern in repair_settings.FIELD_ROLES:
        raw = (request.form.get(f"field_{role}") or "").strip()
        values[f"field_{role}"] = raw if raw.isdigit() else ""
    repair_db.save_settings(values)
    flash("Saved the Zendesk field mapping. Tickets pick it up the next time they're opened.", "success")
    return _done("fields")


@admin_bp.route("/messages", methods=["POST"])
@auth.admin_required
def save_messages():
    form = request.form
    delivered = (form.get("delivered_message") or "").strip()
    template = (form.get("final_report_template") or "").replace(chr(13) + chr(10), chr(10)).strip()
    if not delivered or not template:
        flash("The delivered message and the report template can't be empty.", "error")
        return _done("delivery")
    repair_db.save_settings({
        "delivered_message": delivered,
        "delivered_auto": "1" if form.get("delivered_auto") == "on" else "0",
        "delivered_zendesk_value": (form.get("delivered_zendesk_value") or "").strip(),
        "final_report_template": template,
    })
    flash("Saved the delivered email and the final report template.", "success")
    return _done("delivery")


@admin_bp.route("/messages/reset", methods=["POST"])
@auth.admin_required
def reset_message():
    key = request.form.get("key")
    if key in ("delivered_message", "final_report_template"):
        repair_db.save_settings({key: repair_settings.DEFAULTS[key]})
        flash("Put back the original wording.", "success")
    return _done("delivery")


@admin_bp.route("/atf", methods=["POST"])
@auth.admin_required
def save_atf():
    """Manufacturer and firearm type written on new A&D log entries."""
    maker = " ".join((request.form.get("atf_manufacturer") or "").split())[:120]
    kind = " ".join((request.form.get("atf_firearm_type") or "").split())[:60]
    if not maker or not kind:
        flash("Manufacturer and firearm type can't be empty.", "error")
        return _done("atf")
    repair_db.save_settings({"atf_manufacturer": maker, "atf_firearm_type": kind})
    flash("Saved. New A&D log entries use these; existing entries keep theirs.", "success")
    return _done("atf")
