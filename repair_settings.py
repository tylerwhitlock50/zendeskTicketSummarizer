"""Settings kept in repair.setting, with their defaults, and the mapping of
Zendesk ticket fields (serial, model, tracking...) onto the repair record.
"""

from __future__ import annotations

import re
from typing import Any

import repair_db
from zendesk_client import resolve_custom_fields

# Zendesk ticket fields the portal reads. A field is used when its id is set
# in Settings; otherwise the first field whose title matches the pattern.
FIELD_ROLES = (
    ("serial", "Serial number", r"serial"),
    ("model", "Model / product", r"\bmodel\b|product|rifle"),
    ("caliber", "Caliber", r"caliber|calibre|cartridge|chambering"),
    ("part_number", "Part number / SKU", r"\bsku\b|part\s*(no|num|#)|item\s*(no|num|#)"),
    ("inbound_tracking", "Inbound tracking number", r"tracking"),
    # For the ATF A&D log: where the rifle came from and goes back to.
    ("address", "Customer address (A&D log)", r"address"),
    ("ffl", "Dealer FFL number (A&D log)", r"\bffl\b"),
)
_SKIP_FOR_INBOUND = re.compile(r"return|outbound|ship\s*back", re.I)
_SKIP_FOR_ADDRESS = re.compile(r"e-?mail|\bip\b|web", re.I)

DEFAULTS = {
    # ATF A&D log: written on every new entry (the officer can edit an entry)
    "atf_manufacturer": "Christensen Arms",
    "atf_firearm_type": "Rifle",
    "delivered_auto": "1",
    "delivered_zendesk_value": "",
    "delivered_message": (
        "Good news: {carrier} shows your rifle was delivered on {delivered_date} "
        "(tracking {tracking}). Thank you for your patience. If anything isn't right, just reply to this email."
    ),
    # Customer intake form (intake.py). Placeholders: {first_name} {ticket_id} {link} {days}
    "intake_link_message": (
        "Hi {first_name},\n"
        "\n"
        "We're preparing to receive your firearm. Before it arrives, please take a couple of minutes to "
        "tell us about it, and let us know if there is anything specific you'd like us to look at or any "
        "specific concerns you have:\n"
        "\n"
        "{link}\n"
        "\n"
        "You can add photos from your phone. The link is just for you and works for {days} days. "
        "If you'd rather just reply to this email, that works too.\n"
        "\n"
        "Christensen Arms Repair"
    ),
    "intake_resend_message": (
        "Hi {first_name},\n"
        "\n"
        "Here is a fresh link to the form about your firearm. Please use this one; the earlier one no "
        "longer works:\n"
        "\n"
        "{link}\n"
        "\n"
        "It is just for you and works for {days} days. If you'd rather just reply to this email, "
        "that works too.\n"
        "\n"
        "Christensen Arms Repair"
    ),
    "intake_reminder_message": (
        "Hi {first_name},\n"
        "\n"
        "A quick reminder: we sent you a link to a short form about your firearm. If you have a couple of "
        "minutes, filling it in helps our technicians know what to look at. The link is in our earlier "
        "email. If you'd rather just reply here and tell us, that works too.\n"
        "\n"
        "Christensen Arms Repair"
    ),
    "final_report_template": (
        "Repair report for ticket #{ticket_id}\n"
        "\n"
        "Rifle: {model} {caliber}\n"
        "Serial number: {serial}\n"
        "\n"
        "RECEIVED {received_date}\n"
        "Items received with it: {accessories}\n"
        "Condition on arrival: {arrival}\n"
        "\n"
        "INSPECTION ({points}-point)\n"
        "{inspection}\n"
        "\n"
        "What we found: {problem}\n"
        "\n"
        "REPAIR\n"
        "Work performed: {work_done}\n"
        "{parts_line}"
        "\n"
        "FINAL TEST\n"
        "{test}\n"
        "\n"
        "RETURN SHIPMENT\n"
        "Shipped {shipped_date} via {carrier}, tracking {tracking}\n"
        "{tracking_url}\n"
        "\n"
        "Thank you for trusting us with your rifle. If you have any questions about this repair, "
        "just reply to this email."
    ),
}


def load() -> dict[str, str]:
    values = dict(DEFAULTS)
    values.update(repair_db.get_settings())
    return values


def field_ids(settings: dict[str, str]) -> dict[str, int | None]:
    out = {}
    for role, _title, _pattern in FIELD_ROLES:
        raw = (settings.get(f"field_{role}") or "").strip()
        out[role] = int(raw) if raw.isdigit() else None
    return out


def resolve_roles(fields_map: dict[int, dict[str, Any]], settings: dict[str, str]) -> dict[str, int | None]:
    """Which Zendesk field id holds each role (set in Settings, else matched by title)."""
    chosen = field_ids(settings)
    for role, _title, pattern in FIELD_ROLES:
        if chosen[role] is not None:
            continue
        rx = re.compile(pattern, re.I)
        for fid, f in fields_map.items():
            title = f.get("title") or ""
            if f.get("active") is False or not rx.search(title):
                continue
            if role == "inbound_tracking" and _SKIP_FOR_INBOUND.search(title):
                continue
            if role == "address" and _SKIP_FOR_ADDRESS.search(title):
                continue
            if fid in chosen.values():
                continue
            chosen[role] = fid
            break
    return chosen


def ticket_snapshot(ticket: dict[str, Any], fields_map: dict[int, dict[str, Any]],
                    settings: dict[str, str]) -> dict[str, Any]:
    """The ticket's product data for the repair record: mapped roles plus every filled field."""
    roles = resolve_roles(fields_map, settings)
    raw = {cf["id"]: cf.get("value") for cf in ticket.get("custom_fields") or []}
    snap: dict[str, Any] = {}
    for role, fid in roles.items():
        value = raw.get(fid) if fid is not None else None
        if value in (None, "", [], False):
            snap[f"zd_{role}"] = None
            continue
        options = {o["value"]: o["name"] for o in (fields_map.get(fid) or {}).get("custom_field_options", [])}
        if isinstance(value, list):
            value = ", ".join(str(options.get(v, v)) for v in value)
        limit = 500 if role == "address" else 200
        snap[f"zd_{role}"] = " ".join(str(options.get(value, value)).split())[:limit]
    snap["ticket_fields"] = resolve_custom_fields(ticket, fields_map)
    return snap
