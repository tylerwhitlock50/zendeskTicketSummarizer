"""ATF acquisition & disposition (A&D) log (tables repair.atf_entry / atf_change).

One entry per time a firearm comes into the building and leaves it:

- **Received** opens an entry (acquired now, from the ticket's requester, their
  address and FFL from the mapped Zendesk fields). Re-posting Received while the
  rifle is still here corrects that entry instead of opening another.
- **Shipped** closes the open entry (disposed now, to whoever it came from).
  Re-posting Shipped corrects the disposition; its date stays the first scan.
- Posting Received again on a shipped RMA means the rifle came back: it opens
  a NEW entry. Moving a shipped RMA back to a repair stage (Inspected, Repair
  complete, Final test) means it never left: the disposition is cleared.

The hooks run on the stage post's ``ticket_lock`` connection, so the log and
the stage are saved together or not at all. Nothing is ever deleted; every
change after an entry is written is kept in ``atf_change`` with before/after.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any, Optional

import psycopg
from psycopg.types.json import Jsonb

from db import _conn

DEFAULT_MANUFACTURER = "Christensen Arms"
DEFAULT_FIREARM_TYPE = "Rifle"

# Fields the compliance officer may edit on an entry (with a reason).
EDITABLE = (
    "manufacturer", "model", "caliber", "firearm_type", "serial_number",
    "acquired_from_name", "acquired_from_address", "acquired_from_ffl", "acquired_tracking",
    "disposed_to_name", "disposed_to_address", "disposed_to_ffl", "disposed_tracking", "notes",
)
FIELD_LABELS = {
    "manufacturer": "Manufacturer", "model": "Model", "caliber": "Caliber", "firearm_type": "Type",
    "serial_number": "Serial number", "acquired_at": "Acquired",
    "acquired_from_name": "Received from", "acquired_from_address": "From address",
    "acquired_from_ffl": "From FFL", "acquired_tracking": "Inbound tracking", "acquired_by": "Scanned in by",
    "disposed_at": "Disposed", "disposed_to_name": "Shipped to", "disposed_to_address": "To address",
    "disposed_to_ffl": "To FFL", "disposed_serial": "Serial at pack-out", "disposed_tracking": "Return tracking",
    "disposed_carrier": "Carrier", "disposed_by": "Shipped by", "review_note": "Review note", "notes": "Notes",
}
_DISPOSITION = ("disposed_at", "disposed_to_name", "disposed_to_address", "disposed_to_ffl",
                "disposed_serial", "disposed_tracking", "disposed_carrier", "disposed_by")

# Stages that come after Received: moving a shipped RMA back to one of these
# is a correction (it never left), not a return.
_AFTER_RECEIVED = ("inspected", "repaired", "tested")


def _norm(col: str) -> str:
    return f"upper(regexp_replace(COALESCE({col}, ''), '[[:space:]-]+', '', 'g'))"


def _json(value: Any) -> Any:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    return value


def _defaults(conn: psycopg.Connection) -> dict[str, str]:
    rows = conn.execute(
        "SELECT key, value FROM repair.setting WHERE key IN ('atf_manufacturer', 'atf_firearm_type')"
    ).fetchall()
    found = {r["key"]: r["value"] for r in rows if r["value"]}
    return {
        "manufacturer": found.get("atf_manufacturer", DEFAULT_MANUFACTURER),
        "firearm_type": found.get("atf_firearm_type", DEFAULT_FIREARM_TYPE),
    }


def _open_entry(conn: psycopg.Connection, ticket_id: int) -> Optional[dict[str, Any]]:
    return conn.execute(
        "SELECT * FROM repair.atf_entry WHERE ticket_id = %s AND disposed_at IS NULL FOR UPDATE",
        (ticket_id,),
    ).fetchone()


def _last_disposed(conn: psycopg.Connection, ticket_id: int) -> Optional[dict[str, Any]]:
    return conn.execute(
        "SELECT * FROM repair.atf_entry WHERE ticket_id = %s AND disposed_at IS NOT NULL "
        "ORDER BY disposed_at DESC, id DESC LIMIT 1 FOR UPDATE",
        (ticket_id,),
    ).fetchone()


def _change(conn: psycopg.Connection, entry: dict[str, Any], new: dict[str, Any],
            reason: str, by: str | None, force_log: bool = False) -> bool:
    """Apply the fields of ``new`` that differ, and log them. Returns True if anything changed."""
    diff = {k: v for k, v in new.items() if entry.get(k) != v}
    if not diff and not force_log:
        return False
    if diff:
        sets = ", ".join(f"{k} = %s" for k in diff)
        conn.execute(
            f"UPDATE repair.atf_entry SET {sets}, updated_at = now() WHERE id = %s",
            [*diff.values(), entry["id"]],
        )
    conn.execute(
        "INSERT INTO repair.atf_change (entry_id, changed_by, reason, before, after) VALUES (%s, %s, %s, %s, %s)",
        (
            entry["id"], by, reason,
            Jsonb({k: _json(entry.get(k)) for k in diff}),
            Jsonb({k: _json(v) for k, v in diff.items()}),
        ),
    )
    return True


def _rifle(rma: dict[str, Any]) -> dict[str, Any]:
    return {
        "model": rma.get("model") or rma.get("zd_model"),
        "caliber": rma.get("caliber") or rma.get("zd_caliber"),
        "serial_number": rma.get("serial_number") or rma.get("zd_serial"),
    }


# ---------------------------------------------------------------- stage hooks


def on_stage(conn: psycopg.Connection, ticket_id: int, key: str, previous_stage: str,
             tech: dict[str, Any]) -> None:
    """Keep the A&D log in step with a stage post (call after the RMA row is updated)."""
    rma = conn.execute("SELECT * FROM repair.rma WHERE ticket_id = %s", (ticket_id,)).fetchone()
    if rma is None:
        return
    who = tech.get("name")
    if key == "received":
        _receive(conn, rma, who)
    elif key == "shipped":
        _ship(conn, rma, who)
    elif previous_stage == "shipped" and key in _AFTER_RECEIVED:
        _unship(conn, rma, who, key)


def _receive(conn: psycopg.Connection, rma: dict[str, Any], who: str | None) -> None:
    rifle = _rifle(rma)
    inbound = rma.get("inbound_tracking") or rma.get("zd_inbound_tracking")
    entry = _open_entry(conn, rma["ticket_id"])
    if entry:
        # Received re-posted while it's still here: a correction of the check-in.
        _change(conn, entry, {**rifle, "acquired_tracking": inbound},
                f"Received re-posted by {who}", who)
        return
    defaults = _defaults(conn)
    conn.execute(
        "INSERT INTO repair.atf_entry (ticket_id, manufacturer, model, caliber, firearm_type, serial_number, "
        "  acquired_at, acquired_from_name, acquired_from_address, acquired_from_ffl, acquired_tracking, "
        "  acquired_by, review_note) "
        "VALUES (%s, %s, %s, %s, %s, %s, now(), %s, %s, %s, %s, %s, %s)",
        (
            rma["ticket_id"], defaults["manufacturer"], rifle["model"], rifle["caliber"],
            defaults["firearm_type"], rifle["serial_number"],
            rma.get("requester_name"), rma.get("zd_address"), rma.get("zd_ffl"), inbound, who,
            None if rma.get("zd_address") else "No customer address on the ticket; add it.",
        ),
    )


def _ship(conn: psycopg.Connection, rma: dict[str, Any], who: str | None) -> None:
    ticket_id = rma["ticket_id"]
    entry = _open_entry(conn, ticket_id)
    if entry is None:
        last = _last_disposed(conn, ticket_id)
        if last is not None and (not rma.get("received_at") or last["disposed_at"] >= rma["received_at"]):
            # Shipped re-posted (e.g. a new tracking number): correct the disposition, keep its date.
            _change(conn, last, {
                "disposed_serial": rma.get("ship_serial"),
                "disposed_tracking": rma.get("tracking_number"),
                "disposed_carrier": rma.get("return_carrier"),
            }, f"Shipped re-posted by {who}", who)
            return
        # Shipped without a Received scan in the portal: log it, and flag it.
        defaults = _defaults(conn)
        rifle = _rifle(rma)
        entry = conn.execute(
            "INSERT INTO repair.atf_entry (ticket_id, manufacturer, model, caliber, firearm_type, serial_number, "
            "  acquired_at, acquired_from_name, acquired_from_address, acquired_from_ffl, review_note) "
            "VALUES (%s, %s, %s, %s, %s, %s, COALESCE(%s, %s), %s, %s, %s, %s) RETURNING *",
            (
                ticket_id, defaults["manufacturer"], rifle["model"], rifle["caliber"], defaults["firearm_type"],
                rifle["serial_number"] or rma.get("ship_serial"), rma.get("received_at"), rma.get("added_at"),
                rma.get("requester_name"), rma.get("zd_address"), rma.get("zd_ffl"),
                "Shipped without a Received scan: the acquisition date is when it was added to the queue. "
                "Confirm the date it actually arrived.",
            ),
        ).fetchone()
    conn.execute(
        "UPDATE repair.atf_entry SET disposed_at = now(), "
        "  disposed_to_name = COALESCE(acquired_from_name, %s), "
        "  disposed_to_address = COALESCE(acquired_from_address, %s), "
        "  disposed_to_ffl = COALESCE(acquired_from_ffl, %s), "
        "  disposed_serial = %s, disposed_tracking = %s, disposed_carrier = %s, disposed_by = %s, "
        "  updated_at = now() "
        "WHERE id = %s",
        (
            rma.get("requester_name"), rma.get("zd_address"), rma.get("zd_ffl"),
            rma.get("ship_serial"), rma.get("tracking_number"), rma.get("return_carrier"), who,
            entry["id"],
        ),
    )


def _unship(conn: psycopg.Connection, rma: dict[str, Any], who: str | None, key: str) -> None:
    """Moved back from Shipped to a repair stage: it never left, so clear the disposition."""
    ticket_id = rma["ticket_id"]
    if _open_entry(conn, ticket_id) is not None:
        return
    last = _last_disposed(conn, ticket_id)
    if last is None:
        return
    _change(conn, last, {k: None for k in _DISPOSITION},
            f"Moved back from Shipped to {key} by {who}: still in the building, disposition cleared", who)


# ---------------------------------------------------------------- reading


_SELECT = (
    "SELECT e.*, r.stage AS rma_stage, r.subject, r.requester_email, "
    "  (SELECT count(*) FROM repair.atf_change c WHERE c.entry_id = e.id) AS changes, "
    "  EXTRACT(EPOCH FROM COALESCE(e.disposed_at, now()) - e.acquired_at) / 86400 AS days_held "
    "FROM repair.atf_entry e JOIN repair.rma r ON r.ticket_id = e.ticket_id "
)


def _local(col: str, tz: str) -> str:
    return f"({col} AT TIME ZONE '{tz}')::date"


def on_hand() -> list[dict[str, Any]]:
    """Every firearm in the building now, longest here first."""
    with _conn() as conn:
        return conn.execute(_SELECT + "WHERE e.disposed_at IS NULL ORDER BY e.acquired_at").fetchall()


def acquisitions(start, end, tz: str) -> list[dict[str, Any]]:
    with _conn() as conn:
        return conn.execute(
            _SELECT + f"WHERE {_local('e.acquired_at', tz)} BETWEEN %s AND %s ORDER BY e.acquired_at, e.id",
            (start, end),
        ).fetchall()


def dispositions(start, end, tz: str) -> list[dict[str, Any]]:
    with _conn() as conn:
        return conn.execute(
            _SELECT + f"WHERE {_local('e.disposed_at', tz)} BETWEEN %s AND %s ORDER BY e.disposed_at, e.id",
            (start, end),
        ).fetchall()


def bound_book(start, end, tz: str) -> list[dict[str, Any]]:
    """Entries acquired or disposed in the range, plus anything held through it, by acquisition."""
    with _conn() as conn:
        return conn.execute(
            _SELECT + f"WHERE {_local('e.acquired_at', tz)} <= %s "
            f"  AND (e.disposed_at IS NULL OR {_local('e.disposed_at', tz)} >= %s) "
            "ORDER BY e.acquired_at, e.id",
            (end, start),
        ).fetchall()


def search(code: str) -> list[dict[str, Any]]:
    """Every entry whose serial (received or at pack-out) or ticket matches, newest first."""
    norm = "".join((code or "").split()).replace("-", "").upper()
    if not norm:
        return []
    ticket = int(norm) if norm.isdigit() and len(norm) <= 15 else -1
    with _conn() as conn:
        return conn.execute(
            _SELECT + f"WHERE {_norm('e.serial_number')} LIKE %s OR {_norm('e.disposed_serial')} LIKE %s "
            "  OR e.ticket_id = %s ORDER BY e.acquired_at DESC, e.id DESC LIMIT 200",
            (f"%{norm}%", f"%{norm}%", ticket),
        ).fetchall()


def counts(start, end, tz: str) -> dict[str, int]:
    with _conn() as conn:
        row = conn.execute(
            "SELECT "
            "  count(*) FILTER (WHERE disposed_at IS NULL) AS on_hand, "
            f"  count(*) FILTER (WHERE {_local('acquired_at', tz)} BETWEEN %s AND %s) AS acquired, "
            f"  count(*) FILTER (WHERE {_local('disposed_at', tz)} BETWEEN %s AND %s) AS disposed, "
            "  count(*) FILTER (WHERE review_note IS NOT NULL) AS needs_review "
            "FROM repair.atf_entry",
            (start, end, start, end),
        ).fetchone()
    return dict(row)


def needs_review() -> list[dict[str, Any]]:
    with _conn() as conn:
        return conn.execute(_SELECT + "WHERE e.review_note IS NOT NULL ORDER BY e.acquired_at").fetchall()


def get_entry(entry_id: int) -> Optional[dict[str, Any]]:
    with _conn() as conn:
        return conn.execute(_SELECT + "WHERE e.id = %s", (entry_id,)).fetchone()


def changes_for(entry_id: int) -> list[dict[str, Any]]:
    with _conn() as conn:
        return conn.execute(
            "SELECT * FROM repair.atf_change WHERE entry_id = %s ORDER BY changed_at, id", (entry_id,)
        ).fetchall()


def entries_for_ticket(ticket_id: int) -> list[dict[str, Any]]:
    with _conn() as conn:
        return conn.execute(
            _SELECT + "WHERE e.ticket_id = %s ORDER BY e.acquired_at DESC, e.id DESC", (ticket_id,)
        ).fetchall()


# ---------------------------------------------------------------- officer edits


def edit_entry(entry_id: int, values: dict[str, Any], reason: str, by: str,
               clear_review: bool = False) -> bool:
    """The compliance officer corrects an entry. Returns True if anything changed."""
    new = {k: (values.get(k) or None) for k in EDITABLE if k in values}
    with _conn() as conn:
        with conn.transaction():
            entry = conn.execute(
                "SELECT * FROM repair.atf_entry WHERE id = %s FOR UPDATE", (entry_id,)
            ).fetchone()
            if entry is None:
                return False
            if entry["disposed_at"] is None:
                # Not shipped yet: there's no disposition to edit.
                for k in [k for k in new if k.startswith("disposed_")]:
                    new.pop(k)
            if clear_review:
                new["review_note"] = None
            return _change(conn, entry, new, reason, by)


def export_rows(rows: list[dict[str, Any]]) -> tuple[list[str], list[dict[str, Any]]]:
    """Rows for the CSV download, in bound-book column order."""
    columns = [
        "entry_id", "ticket_id", "manufacturer", "model", "caliber", "type", "serial_number",
        "acquired_at", "acquired_from_name", "acquired_from_address", "acquired_from_ffl",
        "acquired_tracking", "acquired_by",
        "disposed_at", "disposed_to_name", "disposed_to_address", "disposed_to_ffl",
        "disposed_serial", "disposed_tracking", "disposed_carrier", "disposed_by",
        "days_held", "status", "source", "review_note", "corrections", "notes",
    ]
    out = []
    for e in rows:
        out.append({
            "entry_id": e["id"], "ticket_id": e["ticket_id"], "manufacturer": e["manufacturer"],
            "model": e["model"], "caliber": e["caliber"], "type": e["firearm_type"],
            "serial_number": e["serial_number"],
            **{k: e[k] for k in ("acquired_at", "acquired_from_name", "acquired_from_address",
                                 "acquired_from_ffl", "acquired_tracking", "acquired_by")},
            **{k: e[k] for k in _DISPOSITION},
            "days_held": round(float(e["days_held"]), 1) if e["days_held"] is not None else None,
            "status": "In building" if e["disposed_at"] is None else "Disposed",
            "source": e["source"], "review_note": e["review_note"], "corrections": e["changes"],
            "notes": e["notes"],
        })
    return columns, out
