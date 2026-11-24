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
)
_SKIP_FOR_INBOUND = re.compile(r"return|outbound|ship\s*back", re.I)

DEFAULTS = {
    "delivered_auto": "1",
    "delivered_zendesk_value": "",
    "delivered_message": (
        "Good news: {carrier} shows your rifle was delivered on {delivered_date} "
        "(tracking {tracking}). Thank you for your patience. If anything isn't right, just reply to this email."
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
        snap[f"zd_{role}"] = " ".join(str(options.get(value, value)).split())[:200]
    snap["ticket_fields"] = resolve_custom_fields(ticket, fields_map)
    return snap
