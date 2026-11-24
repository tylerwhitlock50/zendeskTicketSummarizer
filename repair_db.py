"""Postgres data layer for the repair record (schema ``repair``).

Shares the connection pool in ``db``. Zendesk stays the system of record for
the customer conversation; these tables hold the shop-side data: the queue,
every scan with who and when, the inspection and repair forms, and settings
(stages, drop-down lists, techs).
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any, Iterator, Optional

import psycopg

from psycopg.types.json import Jsonb
from werkzeug.security import check_password_hash, generate_password_hash

import atf_log
from db import _conn

STAGE_KEYS = ("received", "inspected", "repaired", "tested", "shipped")
# Board columns in order; "queued" = added from Incoming, not yet received.
BOARD_STAGES = ("queued",) + STAGE_KEYS
_STAGE_TS = {k: f"{k}_at" for k in STAGE_KEYS}

REPORT_TZ = "America/Denver"

PIN_MAX_FAILS = 5          # wrong PINs in a row before sign-in pauses
PIN_LOCK_MINUTES = 10


def stage_index(key: str) -> int:
    """Position in the repair flow: queued = -1, received = 0 ... shipped = 3."""
    return STAGE_KEYS.index(key) if key in STAGE_KEYS else -1


# ---------------------------------------------------------------- stages


def list_stages() -> list[dict[str, Any]]:
    with _conn() as conn:
        return conn.execute(
            "SELECT key, label, comment_body, zendesk_value, position "
            "FROM repair.stage ORDER BY position"
        ).fetchall()


def get_stage(key: str) -> Optional[dict[str, Any]]:
    with _conn() as conn:
        return conn.execute(
            "SELECT key, label, comment_body, zendesk_value, position "
            "FROM repair.stage WHERE key = %s",
            (key,),
        ).fetchone()


def update_stage(key: str, label: str, comment_body: str, zendesk_value: str | None) -> None:
    with _conn() as conn:
        conn.execute(
            "UPDATE repair.stage SET label = %s, comment_body = %s, zendesk_value = %s "
            "WHERE key = %s",
            (label, comment_body, zendesk_value or None, key),
        )


# ---------------------------------------------------------------- choices


def list_choices(kind: str, include_inactive: bool = False) -> list[dict[str, Any]]:
    where = "" if include_inactive else "AND active"
    with _conn() as conn:
        return conn.execute(
            f"SELECT id, kind, label, position, active FROM repair.choice "
            f"WHERE kind = %s {where} ORDER BY position, label",
            (kind,),
        ).fetchall()


def get_active_choices(choice_ids: list[int], kind: str) -> list[dict[str, Any]]:
    """The active list items of this kind among these ids, in list order."""
    if not choice_ids:
        return []
    with _conn() as conn:
        return conn.execute(
            "SELECT id, kind, label, position, active FROM repair.choice "
            "WHERE id = ANY(%s) AND kind = %s AND active ORDER BY position, label",
            (list(choice_ids), kind),
        ).fetchall()


def ensure_choice(kind: str, label: str, conn: psycopg.Connection | None = None) -> dict[str, Any]:
    """Add a list item (a tech's "Other" text), or reuse and re-activate a match.

    Pass the ``ticket_lock`` connection so the addition rolls back with the post.
    """
    if conn is not None:
        return _ensure_choice(conn, kind, label)
    with _conn() as c:
        return _ensure_choice(c, kind, label)


def _ensure_choice(conn: psycopg.Connection, kind: str, label: str) -> dict[str, Any]:
    return conn.execute(
        "INSERT INTO repair.choice (kind, label, position) VALUES (%s, %s, "
        "  (SELECT COALESCE(MAX(position), 0) + 10 FROM repair.choice WHERE kind = %s)) "
        "ON CONFLICT (kind, lower(label)) DO UPDATE SET active = true "
        "RETURNING id, kind, label, position, active",
        (kind, label, kind),
    ).fetchone()


def create_choice(kind: str, label: str) -> None:
    with _conn() as conn:
        conn.execute(
            "INSERT INTO repair.choice (kind, label, position) VALUES (%s, %s, "
            "  (SELECT COALESCE(MAX(position), 0) + 10 FROM repair.choice WHERE kind = %s))",
            (kind, label, kind),
        )


def update_choice(choice_id: int, label: str, position: int, active: bool) -> None:
    with _conn() as conn:
        conn.execute(
            "UPDATE repair.choice SET label = %s, position = %s, active = %s WHERE id = %s",
            (label, position, active, choice_id),
        )


# ---------------------------------------------------------------- techs


def list_techs(include_inactive: bool = False) -> list[dict[str, Any]]:
    where = "" if include_inactive else "WHERE active"
    with _conn() as conn:
        return conn.execute(
            f"SELECT id, name, active FROM repair.tech {where} ORDER BY lower(name)"
        ).fetchall()


def get_tech(tech_id: int) -> Optional[dict[str, Any]]:
    with _conn() as conn:
        return conn.execute(
            "SELECT id, name, active FROM repair.tech WHERE id = %s", (tech_id,)
        ).fetchone()


def create_tech(name: str, pin: str) -> None:
    with _conn() as conn:
        conn.execute(
            "INSERT INTO repair.tech (name, pin_hash) VALUES (%s, %s)",
            (name, generate_password_hash(pin)),
        )


def update_tech(tech_id: int, name: str, active: bool, new_pin: str | None = None) -> None:
    with _conn() as conn:
        if new_pin:
            conn.execute(
                "UPDATE repair.tech SET name = %s, active = %s, pin_hash = %s WHERE id = %s",
                (name, active, generate_password_hash(new_pin), tech_id),
            )
        else:
            conn.execute(
                "UPDATE repair.tech SET name = %s, active = %s WHERE id = %s",
                (name, active, tech_id),
            )


def check_tech_pin(tech_id: int, pin: str) -> tuple[Optional[dict[str, Any]], bool]:
    """Check a sign-in. Returns (tech or None, locked).

    After PIN_MAX_FAILS wrong PINs in a row the person's sign-in pauses for
    PIN_LOCK_MINUTES (shared by all app workers, since it lives in the DB).
    """
    with _conn() as conn:
        with conn.transaction():
            row = conn.execute(
                "SELECT id, name, active, pin_hash, failed_pins, "
                "  locked_until IS NOT NULL AND locked_until > now() AS locked "
                "FROM repair.tech WHERE id = %s FOR UPDATE",
                (tech_id,),
            ).fetchone()
            if not row or not row["active"]:
                return None, False
            if row["locked"]:
                return None, True
            if check_password_hash(row["pin_hash"], pin):
                conn.execute(
                    "UPDATE repair.tech SET failed_pins = 0, locked_until = NULL WHERE id = %s",
                    (tech_id,),
                )
                return {"id": row["id"], "name": row["name"], "pin_hash": row["pin_hash"]}, False
            fails = row["failed_pins"] + 1
            locked = fails >= PIN_MAX_FAILS
            conn.execute(
                "UPDATE repair.tech SET failed_pins = %s, "
                "locked_until = CASE WHEN %s THEN now() + make_interval(mins => %s) END "
                "WHERE id = %s",
                (0 if locked else fails, locked, PIN_LOCK_MINUTES, tech_id),
            )
            return None, locked


def get_tech_auth(tech_id: int) -> Optional[dict[str, Any]]:
    """id, name, active and pin_hash, for re-checking a signed-in session."""
    with _conn() as conn:
        return conn.execute(
            "SELECT id, name, active, pin_hash FROM repair.tech WHERE id = %s", (tech_id,)
        ).fetchone()


# ---------------------------------------------------------------- RMAs


def _picked(kind: str) -> str:
    """SQL for one RMA's picks of a kind: an id array and a 'A, B, other' summary."""
    links = (
        "FROM repair.rma_choice rc JOIN repair.choice c ON c.id = rc.choice_id "
        f"WHERE rc.ticket_id = r.ticket_id AND c.kind = '{kind}'"
    )
    return (
        f"ARRAY(SELECT rc.choice_id {links} ORDER BY c.position, c.label) AS {kind}_ids, "
        f"NULLIF(concat_ws(', ', (SELECT string_agg(c.label, ', ' ORDER BY c.position, c.label) {links}), "
        f"NULLIF(r.{kind}_other, '')), '') AS {kind}_summary"
    )


CHOICE_KINDS = ("problem", "solution", "accessory", "inspection_point", "root_cause")

_RMA_SELECT = (
    f"SELECT r.*, {_picked('problem')}, {_picked('solution')}, {_picked('accessory')}, "
    "(SELECT label FROM repair.choice WHERE id = r.root_cause_id) AS root_cause, "
    "fr.sent_at AS report_sent_at, fr.body IS NOT NULL AS report_edited "
    "FROM repair.rma r LEFT JOIN repair.final_report fr ON fr.ticket_id = r.ticket_id "
)

# Form columns a stage may write (everything else in ``fields`` is ignored).
STAGE_COLUMNS = {
    "accessory_other", "arrival_damage", "arrival_notes",
    "serial_number", "serial_match", "inbound_tracking", "inbound_match",
    "model", "caliber", "part_number", "arrived_loaded",
    "problem_other", "problem_notes", "inspection", "complaint_match", "trigger_pull_found", "headspace",
    "solution_other", "work_done", "root_cause_id", "coverage", "parts_used", "trigger_pull_after",
    "test_required", "test_result", "group_size", "group_unit", "test_distance", "test_ammo",
    "function_check",
    "tracking_number", "ship_serial", "return_carrier", "ship_override", "flag",
}

# Copied from the Zendesk ticket (see update_snapshot).
SNAPSHOT_COLUMNS = ("zd_serial", "zd_model", "zd_caliber", "zd_part_number", "zd_inbound_tracking",
                    "zd_address", "zd_ffl")


def get_rma(ticket_id: int) -> Optional[dict[str, Any]]:
    with _conn() as conn:
        return conn.execute(_RMA_SELECT + "WHERE r.ticket_id = %s", (ticket_id,)).fetchone()


def rma_ids(ticket_ids: list[int]) -> set[int]:
    """Which of these tickets are already in the repair queue."""
    if not ticket_ids:
        return set()
    with _conn() as conn:
        rows = conn.execute(
            "SELECT ticket_id FROM repair.rma WHERE ticket_id = ANY(%s)", (list(ticket_ids),)
        ).fetchall()
    return {r["ticket_id"] for r in rows}


def add_rma(
    ticket_id: int,
    subject: str | None,
    requester_name: str | None,
    requester_email: str | None,
    tech: dict[str, Any],
    snapshot: dict[str, Any] | None = None,
) -> bool:
    """Add a ticket to the queue. Returns False if it was already there."""
    with _conn() as conn:
        with conn.transaction():
            row = conn.execute(
                "INSERT INTO repair.rma (ticket_id, subject, requester_name, requester_email, added_by) "
                "VALUES (%s, %s, %s, %s, %s) ON CONFLICT (ticket_id) DO NOTHING RETURNING ticket_id",
                (ticket_id, subject, requester_name, requester_email, tech["name"]),
            ).fetchone()
            if not row:
                return False
            if snapshot:
                _write_snapshot(conn, ticket_id, snapshot)
            conn.execute(
                "INSERT INTO repair.event (ticket_id, kind, label, tech_id, tech_name) "
                "VALUES (%s, 'added', 'Added to repair queue', %s, %s)",
                (ticket_id, tech["id"], tech["name"]),
            )
    return True


def _write_snapshot(conn: psycopg.Connection, ticket_id: int, snap: dict[str, Any]) -> None:
    conn.execute(
        "UPDATE repair.rma SET zd_serial = %s, zd_model = %s, zd_caliber = %s, zd_part_number = %s, "
        "zd_inbound_tracking = %s, zd_address = %s, zd_ffl = %s, ticket_fields = %s WHERE ticket_id = %s",
        (*(snap.get(c) for c in SNAPSHOT_COLUMNS), Jsonb(snap.get("ticket_fields") or []), ticket_id),
    )


def update_snapshot(ticket_id: int, snap: dict[str, Any]) -> None:
    """Refresh the copy of the ticket's Zendesk fields (serial, model, tracking...)."""
    with _conn() as conn:
        _write_snapshot(conn, ticket_id, snap)


def normalize_code(raw: str | None) -> str:
    """Upper-case with spaces and dashes removed, for comparing scans."""
    return "".join((raw or "").split()).replace("-", "").upper()


def _norm_sql(col: str) -> str:
    return f"upper(regexp_replace(COALESCE({col}, ''), '[[:space:]-]+', '', 'g'))"


def find_by_code(code: str) -> list[dict[str, Any]]:
    """RMAs whose serial or a tracking number matches a scan (spaces/dashes ignored)."""
    norm = normalize_code(code)
    if len(norm) < 4:
        return []
    cols = ("serial_number", "zd_serial", "ship_serial", "inbound_tracking", "zd_inbound_tracking",
            "tracking_number")
    where = " OR ".join(f"{_norm_sql('r.' + c)} = %s" for c in cols)
    with _conn() as conn:
        return conn.execute(
            _RMA_SELECT + f"WHERE {where} ORDER BY r.added_at DESC LIMIT 20", [norm] * len(cols)
        ).fetchall()


@contextmanager
def ticket_lock(ticket_id: int) -> Iterator[psycopg.Connection]:
    """Hold a per-ticket lock for one stage post, start to finish.

    Two taps on the same button (or two devices) are handled one after the
    other: the second waits, then sees the first one's event and stops, so the
    customer is emailed once. The lock is released when the block ends.
    """
    with _conn() as conn:
        with conn.transaction():
            conn.execute("SELECT pg_advisory_xact_lock(%s, %s)", (4242, ticket_id))
            yield conn


def recent_stage_event(conn: psycopg.Connection, ticket_id: int, stage: str, seconds: int) -> bool:
    """True if this stage was posted for the ticket within the last N seconds."""
    row = conn.execute(
        "SELECT 1 FROM repair.event WHERE ticket_id = %s AND kind = 'stage' AND stage = %s "
        "AND created_at > now() - make_interval(secs => %s) LIMIT 1",
        (ticket_id, stage, seconds),
    ).fetchone()
    return row is not None


def record_stage(
    conn: psycopg.Connection,
    ticket_id: int,
    stage: dict[str, Any],
    tech: dict[str, Any],
    fields: dict[str, Any],
    customer_message: str | None,
    current_stage: str,
    extra: dict[str, Any] | None = None,
) -> None:
    """Move the RMA to a stage, save that stage's form answers, and log the event.

    Runs on the connection from ``ticket_lock``. ``fields`` holds the form
    columns for this stage only. Moving forward keeps a stage's FIRST time
    (re-sending a stage doesn't reset it). Moving back (a correction, or the
    rifle came back) restarts that stage's time and clears the later stages'
    times and the tracking number, so the board and turnaround stay right.
    ``customer_message`` is None when the stage was saved without emailing.
    """
    key = stage["key"]
    if key not in STAGE_KEYS:
        raise ValueError(f"unknown stage {key!r}")
    cols = {k: v for k, v in fields.items() if k in STAGE_COLUMNS}
    # Posting a stage clears any "Other update" flag unless the stage sets one.
    cols.setdefault("flag", None)
    picks = fields.get("picks")  # (kind, [choice ids]) for the inspection / repair forms
    moving_back = stage_index(key) < stage_index(current_stage)
    ts = _STAGE_TS[key]
    sets = [
        "stage = %s",
        f"{ts} = now()" if moving_back else f"{ts} = COALESCE({ts}, now())",
        "updated_at = now()",
    ]
    params: list[Any] = [key]
    if moving_back:
        for later in STAGE_KEYS[stage_index(key) + 1 :]:
            sets.append(f"{_STAGE_TS[later]} = NULL")
        if "tracking_number" not in cols:
            sets.append("tracking_number = NULL")
    if moving_back or "tracking_number" in cols:
        # A new shipment (or none): track it from scratch. Re-posting the same
        # tracking number keeps what was already found.
        for col in ("delivery_status", "delivery_detail", "delivery_checked_at", "delivered_at"):
            if moving_back:
                sets.append(f"{col} = NULL")
            else:
                sets.append(f"{col} = CASE WHEN tracking_number IS DISTINCT FROM %s THEN NULL ELSE {col} END")
                params.append(cols["tracking_number"])
    for col, val in cols.items():
        sets.append(f"{col} = %s")
        params.append(Jsonb(val) if isinstance(val, (list, dict)) else val)
    sets.append("flag_at = CASE WHEN %s::text IS NULL THEN NULL ELSE now() END")
    params.append(cols["flag"])
    params.append(ticket_id)
    conn.execute(f"UPDATE repair.rma SET {', '.join(sets)} WHERE ticket_id = %s", params)
    detail = {k: (float(v) if hasattr(v, "as_integer_ratio") and not isinstance(v, (int, bool)) else v)
              for k, v in cols.items() if v is not None}
    if picks:
        kind, ids = picks[0], picks[1]
        if kind not in CHOICE_KINDS:
            raise ValueError(f"unknown list {kind!r}")
        conn.execute(
            "DELETE FROM repair.rma_choice rc USING repair.choice c "
            "WHERE c.id = rc.choice_id AND c.kind = %s AND rc.ticket_id = %s",
            (kind, ticket_id),
        )
        for cid in ids:
            conn.execute(
                "INSERT INTO repair.rma_choice (ticket_id, choice_id) VALUES (%s, %s) "
                "ON CONFLICT DO NOTHING",
                (ticket_id, cid),
            )
        detail[f"{kind}_ids"] = list(ids)
    if moving_back:
        detail["moved_back_from"] = current_stage
    if customer_message is None:
        detail["not_emailed"] = True
    detail.update(extra or {})
    conn.execute(
        "INSERT INTO repair.event "
        "(ticket_id, kind, stage, label, tech_id, tech_name, detail, customer_message) "
        "VALUES (%s, 'stage', %s, %s, %s, %s, %s, %s)",
        (
            ticket_id,
            key,
            stage["label"],
            tech["id"],
            tech["name"],
            Jsonb(detail),
            customer_message,
        ),
    )
    atf_log.on_stage(conn, ticket_id, key, current_stage, tech)


# ---------------------------------------------------------------- delivery


def awaiting_delivery(recheck_minutes: int = 60, max_days: int = 30, limit: int = 200) -> list[dict[str, Any]]:
    """Shipped RMAs not yet delivered, due for a tracking check (oldest check first)."""
    with _conn() as conn:
        return conn.execute(
            _RMA_SELECT + "WHERE r.stage = 'shipped' AND r.delivered_at IS NULL "
            "AND r.tracking_number IS NOT NULL "
            "AND r.shipped_at > now() - make_interval(days => %s) "
            "AND (r.delivery_checked_at IS NULL "
            "     OR r.delivery_checked_at < now() - make_interval(mins => %s)) "
            "ORDER BY r.delivery_checked_at NULLS FIRST, r.shipped_at LIMIT %s",
            (max_days, recheck_minutes, limit),
        ).fetchall()


def set_delivery_status(ticket_id: int, status: str, detail: str | None) -> None:
    with _conn() as conn:
        conn.execute(
            "UPDATE repair.rma SET delivery_status = %s, delivery_detail = %s, delivery_checked_at = now() "
            "WHERE ticket_id = %s AND delivered_at IS NULL",
            (status, (detail or "")[:300] or None, ticket_id),
        )


def record_delivered(
    conn: psycopg.Connection,
    ticket_id: int,
    delivered_at,
    detail: str | None,
    tech: dict[str, Any] | None,
    customer_message: str | None,
    how: str,
) -> None:
    """Mark the return shipment delivered and log it (on the ticket_lock connection)."""
    conn.execute(
        "UPDATE repair.rma SET delivered_at = COALESCE(%s, now()), delivery_status = 'delivered', "
        "delivery_detail = %s, delivery_checked_at = now(), updated_at = now() WHERE ticket_id = %s",
        (delivered_at, (detail or "")[:300] or None, ticket_id),
    )
    event_detail = {"how": how}
    if customer_message is None:
        event_detail["not_emailed"] = True
    conn.execute(
        "INSERT INTO repair.event (ticket_id, kind, label, tech_id, tech_name, detail, customer_message) "
        "VALUES (%s, 'delivery', 'Delivered', %s, %s, %s, %s)",
        (ticket_id, tech["id"] if tech else None, tech["name"] if tech else "Tracking check",
         Jsonb(event_detail), customer_message),
    )


def is_delivered(conn: psycopg.Connection, ticket_id: int) -> bool:
    row = conn.execute("SELECT delivered_at FROM repair.rma WHERE ticket_id = %s", (ticket_id,)).fetchone()
    return bool(row and row["delivered_at"])


# ---------------------------------------------------------------- final reports


REPORT_STAGES = ("repaired", "tested", "shipped")  # a repair report can be drafted from here on


def final_report_queue(sent_days: int = 14) -> dict[str, list[dict[str, Any]]]:
    """Reports to review: ready (shipped, not sent), in progress, and recently sent."""
    with _conn() as conn:
        rows = conn.execute(
            _RMA_SELECT + "WHERE (r.stage = ANY(%s) AND fr.sent_at IS NULL) "
            "   OR fr.sent_at > now() - make_interval(days => %s) "
            "ORDER BY COALESCE(r.shipped_at, r.repaired_at, r.updated_at)",
            (list(REPORT_STAGES), sent_days),
        ).fetchall()
    out: dict[str, list[dict[str, Any]]] = {"ready": [], "in_progress": [], "sent": []}
    for r in rows:
        if r["report_sent_at"]:
            out["sent"].append(r)
        elif r["stage"] == "shipped":
            out["ready"].append(r)
        else:
            out["in_progress"].append(r)
    out["sent"].sort(key=lambda r: r["report_sent_at"], reverse=True)
    return out


def final_reports_ready_count() -> int:
    with _conn() as conn:
        row = conn.execute(
            "SELECT count(*) AS n FROM repair.rma r LEFT JOIN repair.final_report fr USING (ticket_id) "
            "WHERE r.stage = 'shipped' AND fr.sent_at IS NULL"
        ).fetchone()
    return row["n"]


def get_final_report(ticket_id: int) -> Optional[dict[str, Any]]:
    with _conn() as conn:
        return conn.execute(
            "SELECT * FROM repair.final_report WHERE ticket_id = %s", (ticket_id,)
        ).fetchone()


def save_final_report(ticket_id: int, body: str | None, tech: dict[str, Any]) -> None:
    """Save an edited draft (None goes back to the report built from the record)."""
    with _conn() as conn:
        conn.execute(
            "INSERT INTO repair.final_report (ticket_id, body, edited_by, edited_at) "
            "VALUES (%s, %s, %s, now()) ON CONFLICT (ticket_id) DO UPDATE "
            "SET body = EXCLUDED.body, edited_by = EXCLUDED.edited_by, edited_at = now()",
            (ticket_id, body, tech["name"]),
        )


def report_sent(conn: psycopg.Connection, ticket_id: int) -> bool:
    row = conn.execute(
        "SELECT sent_at FROM repair.final_report WHERE ticket_id = %s", (ticket_id,)
    ).fetchone()
    return bool(row and row["sent_at"])


def record_report_sent(conn: psycopg.Connection, ticket_id: int, body: str, tech: dict[str, Any]) -> None:
    conn.execute(
        "INSERT INTO repair.final_report (ticket_id, sent_at, sent_by, sent_body) "
        "VALUES (%s, now(), %s, %s) ON CONFLICT (ticket_id) DO UPDATE "
        "SET sent_at = now(), sent_by = EXCLUDED.sent_by, sent_body = EXCLUDED.sent_body",
        (ticket_id, tech["name"], body),
    )
    conn.execute(
        "INSERT INTO repair.event (ticket_id, kind, label, tech_id, tech_name, customer_message) "
        "VALUES (%s, 'report', 'Final repair report sent', %s, %s, %s)",
        (ticket_id, tech["id"], tech["name"], body),
    )
    conn.execute("UPDATE repair.rma SET updated_at = now() WHERE ticket_id = %s", (ticket_id,))


def reopen_final_report(ticket_id: int) -> None:
    """Allow a corrected report to be sent again (keeps the draft)."""
    with _conn() as conn:
        conn.execute("UPDATE repair.final_report SET sent_at = NULL WHERE ticket_id = %s", (ticket_id,))


# ---------------------------------------------------------------- settings


def get_settings() -> dict[str, str]:
    with _conn() as conn:
        rows = conn.execute("SELECT key, value FROM repair.setting").fetchall()
    return {r["key"]: r["value"] for r in rows if r["value"] is not None}


def save_settings(values: dict[str, str | None]) -> None:
    with _conn() as conn:
        with conn.transaction():
            for key, value in values.items():
                conn.execute(
                    "INSERT INTO repair.setting (key, value) VALUES (%s, %s) "
                    "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()",
                    (key, value),
                )


def set_flag(ticket_id: int, label: str | None) -> None:
    """Show an "Other update" (Awaiting parts, On hold...) on the queue card."""
    with _conn() as conn:
        conn.execute(
            "UPDATE repair.rma SET flag = %s, flag_at = CASE WHEN %s::text IS NULL THEN NULL ELSE now() END "
            "WHERE ticket_id = %s",
            (label, label, ticket_id),
        )


def record_event(
    ticket_id: int,
    kind: str,
    label: str,
    tech: dict[str, Any],
    detail: dict[str, Any] | None = None,
    customer_message: str | None = None,
) -> None:
    """Log a quick action or note on an RMA (no stage change)."""
    with _conn() as conn:
        with conn.transaction():
            conn.execute(
                "INSERT INTO repair.event "
                "(ticket_id, kind, label, tech_id, tech_name, detail, customer_message) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s)",
                (ticket_id, kind, label, tech["id"], tech["name"], Jsonb(detail or {}), customer_message),
            )
            conn.execute("UPDATE repair.rma SET updated_at = now() WHERE ticket_id = %s", (ticket_id,))


def events_for(ticket_id: int, limit: int = 50) -> list[dict[str, Any]]:
    with _conn() as conn:
        return conn.execute(
            "SELECT kind, stage, label, tech_name, detail, customer_message, created_at "
            "FROM repair.event WHERE ticket_id = %s ORDER BY created_at DESC, id DESC LIMIT %s",
            (ticket_id, limit),
        ).fetchall()


def board(shipped_days: int = 14) -> dict[str, list[dict[str, Any]]]:
    """Open RMAs by stage, plus those shipped in the last N days."""
    with _conn() as conn:
        rows = conn.execute(
            _RMA_SELECT + "WHERE r.stage <> 'shipped' "
            "   OR r.shipped_at > now() - make_interval(days => %s)",
            (shipped_days,),
        ).fetchall()
    cols: dict[str, list[dict[str, Any]]] = {k: [] for k in BOARD_STAGES}
    for r in rows:
        # When it entered its current stage (notes and quick actions don't count).
        ts_col = _STAGE_TS.get(r["stage"])
        r["stage_since"] = (r[ts_col] if ts_col else None) or r["added_at"]
        cols.setdefault(r["stage"], []).append(r)
    for key, items in cols.items():
        # Longest-waiting first; shipped shows the newest shipments first.
        items.sort(key=lambda r: r["stage_since"], reverse=(key == "shipped"))
    return cols


# ---------------------------------------------------------------- reporting


def _local_date(col: str) -> str:
    return f"({col} AT TIME ZONE '{REPORT_TZ}')::date"


def _choice_counts(kind: str, stage_ts: str) -> str:
    """Counts for one list over RMAs that reached the stage in range.

    An RMA with three problems counts once under each. "Other" texts are
    counted by their text so repeats stand out (and can be added to the list).
    """
    in_range = f"{_local_date('r.' + stage_ts)} BETWEEN %s AND %s"
    return (
        "SELECT label, n FROM ("
        "  SELECT c.label AS label, count(*) AS n FROM repair.rma r "
        "  JOIN repair.rma_choice rc ON rc.ticket_id = r.ticket_id "
        f"  JOIN repair.choice c ON c.id = rc.choice_id AND c.kind = '{kind}' "
        f"  WHERE {in_range} GROUP BY c.label "
        "  UNION ALL "
        f"  SELECT 'Other: ' || trim(r.{kind}_other), count(*) FROM repair.rma r "
        f"  WHERE {in_range} AND COALESCE(trim(r.{kind}_other), '') <> '' "
        f"  GROUP BY trim(r.{kind}_other) "
        "  UNION ALL "
        "  SELECT '(nothing recorded)', count(*) FROM repair.rma r "
        f"  WHERE {in_range} AND COALESCE(trim(r.{kind}_other), '') = '' "
        "    AND NOT EXISTS (SELECT 1 FROM repair.rma_choice rc JOIN repair.choice c ON c.id = rc.choice_id "
        f"                    WHERE rc.ticket_id = r.ticket_id AND c.kind = '{kind}') "
        "  HAVING count(*) > 0"
        ") t ORDER BY n DESC, label"
    )


def report(start, end) -> dict[str, Any]:
    """Weekly/ad-hoc report for local dates start..end inclusive."""
    rng = (start, end)
    with _conn() as conn:
        stage_counts = conn.execute(
            "SELECT COALESCE(stage, kind) AS stage, count(*) AS n FROM repair.event "
            f"WHERE kind IN ('added', 'stage') AND {_local_date('created_at')} BETWEEN %s AND %s "
            "GROUP BY 1",
            rng,
        ).fetchall()
        open_by_stage = conn.execute(
            "SELECT stage, count(*) AS n FROM repair.rma WHERE stage <> 'shipped' GROUP BY stage"
        ).fetchall()
        turnaround = conn.execute(
            "SELECT count(*) AS shipped, "
            "  avg(EXTRACT(EPOCH FROM shipped_at - added_at)) / 86400 AS avg_total_days, "
            "  avg(EXTRACT(EPOCH FROM shipped_at - received_at)) / 86400 AS avg_shop_days, "
            "  avg(EXTRACT(EPOCH FROM inspected_at - received_at)) / 86400 AS avg_to_inspect_days, "
            "  avg(EXTRACT(EPOCH FROM repaired_at - inspected_at)) / 86400 AS avg_repair_days, "
            "  avg(EXTRACT(EPOCH FROM shipped_at - repaired_at)) / 86400 AS avg_to_ship_days "
            "FROM repair.rma WHERE stage = 'shipped' "
            f"AND {_local_date('shipped_at')} BETWEEN %s AND %s",
            rng,
        ).fetchone()
        problems = conn.execute(_choice_counts("problem", "inspected_at"), rng * 3).fetchall()
        solutions = conn.execute(_choice_counts("solution", "repaired_at"), rng * 3).fetchall()
        accuracy = conn.execute(
            "SELECT count(*) FILTER (WHERE test_required) AS tested, "
            "  count(*) FILTER (WHERE test_required AND test_result = 'pass') AS passed, "
            "  count(*) FILTER (WHERE test_required AND test_result = 'fail') AS failed, "
            "  count(*) FILTER (WHERE test_required IS FALSE) AS not_required, "
            "  avg(group_size) FILTER (WHERE test_result = 'pass' AND group_unit = 'moa') AS avg_moa, "
            "  avg(group_size) FILTER (WHERE test_result = 'pass' AND group_unit = 'in') AS avg_in "
            f"FROM repair.rma WHERE {_local_date('tested_at')} BETWEEN %s AND %s",
            rng,
        ).fetchone()
        complaint = conn.execute(
            "SELECT complaint_match AS answer, count(*) AS n FROM repair.rma "
            f"WHERE {_local_date('inspected_at')} BETWEEN %s AND %s AND complaint_match IS NOT NULL "
            "GROUP BY 1",
            rng,
        ).fetchall()
        point_issues = conn.execute(
            "SELECT p->>'label' AS label, count(*) AS n "
            "FROM repair.rma r, jsonb_array_elements(COALESCE(r.inspection, '[]'::jsonb)) p "
            f"WHERE {_local_date('r.inspected_at')} BETWEEN %s AND %s AND p->>'result' = 'issue' "
            "GROUP BY 1 ORDER BY n DESC, label",
            rng,
        ).fetchall()
        arrivals = conn.execute(
            "SELECT count(*) AS received, count(*) FILTER (WHERE arrival_damage) AS damaged "
            f"FROM repair.rma WHERE {_local_date('received_at')} BETWEEN %s AND %s",
            rng,
        ).fetchone()
        root_causes = conn.execute(
            "SELECT COALESCE(c.label, '(not recorded)') AS label, count(*) AS n FROM repair.rma r "
            "LEFT JOIN repair.choice c ON c.id = r.root_cause_id "
            f"WHERE {_local_date('r.repaired_at')} BETWEEN %s AND %s GROUP BY 1 ORDER BY n DESC, label",
            rng,
        ).fetchall()
        coverage = conn.execute(
            "SELECT COALESCE(coverage, 'not_recorded') AS answer, count(*) AS n FROM repair.rma "
            f"WHERE {_local_date('repaired_at')} BETWEEN %s AND %s GROUP BY 1",
            rng,
        ).fetchall()
        qc = conn.execute(
            "SELECT count(*) FILTER (WHERE serial_match IS FALSE) AS serial_mismatch, "
            "  count(*) FILTER (WHERE inbound_match IS FALSE) AS inbound_mismatch, "
            "  count(*) FILTER (WHERE arrived_loaded) AS arrived_loaded "
            f"FROM repair.rma WHERE {_local_date('received_at')} BETWEEN %s AND %s",
            rng,
        ).fetchone()
        final_checks = conn.execute(
            "SELECT count(*) FILTER (WHERE function_check = 'pass') AS passed, "
            "  count(*) FILTER (WHERE function_check = 'fail') AS failed "
            f"FROM repair.rma WHERE {_local_date('tested_at')} BETWEEN %s AND %s",
            rng,
        ).fetchone()
        deliveries = conn.execute(
            "SELECT count(*) AS delivered, "
            "  avg(EXTRACT(EPOCH FROM delivered_at - shipped_at)) / 86400 AS avg_transit_days "
            f"FROM repair.rma WHERE {_local_date('delivered_at')} BETWEEN %s AND %s",
            rng,
        ).fetchone()
        shipped_overrides = conn.execute(
            "SELECT count(*) AS n FROM repair.rma WHERE ship_override "
            f"AND {_local_date('shipped_at')} BETWEEN %s AND %s",
            rng,
        ).fetchone()
        by_tech = conn.execute(
            "SELECT COALESCE(tech_name, '(unknown)') AS tech, COALESCE(stage, kind) AS stage, "
            "count(*) AS n FROM repair.event "
            f"WHERE {_local_date('created_at')} BETWEEN %s AND %s "
            "GROUP BY 1, 2 ORDER BY 1, 2",
            rng,
        ).fetchall()

    techs: dict[str, dict[str, int]] = {}
    for row in by_tech:
        t = techs.setdefault(row["tech"], {})
        t[row["stage"]] = row["n"]
        t["total"] = t.get("total", 0) + row["n"]
    return {
        "stage_counts": {r["stage"]: r["n"] for r in stage_counts},
        "open_by_stage": {r["stage"]: r["n"] for r in open_by_stage},
        "turnaround": turnaround,
        "problems": problems,
        "solutions": solutions,
        "techs": techs,
        "accuracy": accuracy,
        "complaint": {r["answer"]: r["n"] for r in complaint},
        "point_issues": point_issues,
        "arrivals": arrivals,
        "root_causes": root_causes,
        "coverage": {r["answer"]: r["n"] for r in coverage},
        "qc": qc,
        "final_checks": final_checks,
        "deliveries": deliveries,
        "shipped_overrides": shipped_overrides["n"],
    }


def _with_columns(cur) -> tuple[list[str], list[dict[str, Any]]]:
    return [d.name for d in cur.description], cur.fetchall()


def export_rmas(start, end) -> tuple[list[str], list[dict[str, Any]]]:
    """RMAs added or updated in the date range, for CSV download: (columns, rows)."""
    with _conn() as conn:
        return _with_columns(conn.execute(
            "SELECT r.ticket_id, r.subject, r.requester_name, r.requester_email, r.stage, "
            "COALESCE(NULLIF(r.model, ''), r.zd_model) AS model, "
            "COALESCE(NULLIF(r.caliber, ''), r.zd_caliber) AS caliber, "
            "COALESCE(NULLIF(r.part_number, ''), r.zd_part_number) AS part_number, "
            "COALESCE(NULLIF(r.serial_number, ''), r.zd_serial) AS serial_number, "
            "r.zd_serial AS ticket_serial, r.serial_match, r.zd_model AS ticket_model, "
            "r.zd_caliber AS ticket_caliber, r.zd_part_number AS ticket_part_number, "
            "r.zd_inbound_tracking AS ticket_inbound_tracking, r.inbound_tracking, r.inbound_match, "
            "r.added_by, r.added_at, r.received_at, r.inspected_at, r.repaired_at, r.tested_at, "
            "r.shipped_at, r.delivered_at, "
            "r.arrived_loaded, x.accessory_summary AS accessories, r.arrival_damage, r.arrival_notes, "
            "r.complaint_match, x.problem_summary AS problems, r.problem_notes, r.trigger_pull_found, "
            "r.headspace, x.root_cause, r.coverage, x.solution_summary AS solutions, r.work_done, "
            "r.parts_used, r.trigger_pull_after, r.test_required, r.test_result, r.group_size, "
            "r.group_unit, r.test_distance, r.test_ammo, r.function_check, r.ship_serial, "
            "r.tracking_number, r.return_carrier, r.ship_override, r.delivery_status, r.flag, "
            "x.report_sent_at AS final_report_sent_at, r.ticket_fields::text AS zendesk_fields "
            f"FROM repair.rma r JOIN ({_RMA_SELECT}) x ON x.ticket_id = r.ticket_id "
            f"WHERE {_local_date('r.added_at')} BETWEEN %s AND %s "
            f"   OR {_local_date('r.updated_at')} BETWEEN %s AND %s "
            "ORDER BY r.added_at",
            (start, end, start, end),
        ))


def export_events(start, end) -> tuple[list[str], list[dict[str, Any]]]:
    with _conn() as conn:
        return _with_columns(conn.execute(
            "SELECT e.created_at, e.ticket_id, e.kind, e.stage, e.label, e.tech_name, "
            "e.detail::text AS detail, e.customer_message "
            "FROM repair.event e "
            f"WHERE {_local_date('e.created_at')} BETWEEN %s AND %s ORDER BY e.created_at, e.id",
            (start, end),
        ))


AGE_BUCKETS = ((0, 2, "0–2 days"), (3, 7, "3–7 days"), (8, 14, "8–14 days"),
               (15, 30, "15–30 days"), (31, None, "Over 30 days"))
TURNAROUND_BUCKETS = ((0, 3, "0–3"), (4, 7, "4–7"), (8, 14, "8–14"), (15, 21, "15–21"),
                      (22, 30, "22–30"), (31, 45, "31–45"), (46, None, "46+"))


def _bucket(days: float, buckets) -> int:
    d = int(days)
    for i, (lo, hi, _label) in enumerate(buckets):
        if d >= lo and (hi is None or d <= hi):
            return i
    return 0


def report_charts(start, end) -> dict[str, Any]:
    """Series for the report's charts, for local dates start..end inclusive.

    daily:   events per local day and stage (every day in range, zeros filled)
    aging:   open RMAs by current stage and days in that stage
    shipped: per-RMA days for those shipped in range (turnaround spread)
    previous: the same headline counts for the period just before, for deltas
    """
    from datetime import timedelta

    days = (end - start).days + 1
    prev_start, prev_end = start - timedelta(days=days), start - timedelta(days=1)
    with _conn() as conn:
        daily_rows = conn.execute(
            f"SELECT {_local_date('created_at')} AS day, COALESCE(stage, kind) AS stage, count(*) AS n "
            "FROM repair.event WHERE kind IN ('added', 'stage') "
            f"AND {_local_date('created_at')} BETWEEN %s AND %s GROUP BY 1, 2",
            (start, end),
        ).fetchall()
        open_rows = conn.execute(
            "SELECT stage, EXTRACT(EPOCH FROM now() - COALESCE("
            "  CASE stage WHEN 'received' THEN received_at WHEN 'inspected' THEN inspected_at "
            "    WHEN 'repaired' THEN repaired_at WHEN 'tested' THEN tested_at END, added_at)) / 86400 AS days "
            "FROM repair.rma WHERE stage <> 'shipped'"
        ).fetchall()
        shipped_rows = conn.execute(
            "SELECT EXTRACT(EPOCH FROM shipped_at - added_at) / 86400 AS total_days, "
            "  EXTRACT(EPOCH FROM shipped_at - received_at) / 86400 AS shop_days "
            "FROM repair.rma WHERE stage = 'shipped' "
            f"AND {_local_date('shipped_at')} BETWEEN %s AND %s",
            (start, end),
        ).fetchall()
        prev_counts = conn.execute(
            "SELECT COALESCE(stage, kind) AS stage, count(*) AS n FROM repair.event "
            f"WHERE kind IN ('added', 'stage') AND {_local_date('created_at')} BETWEEN %s AND %s "
            "GROUP BY 1",
            (prev_start, prev_end),
        ).fetchall()
        prev_turn = conn.execute(
            "SELECT avg(EXTRACT(EPOCH FROM shipped_at - received_at)) / 86400 AS avg_shop_days "
            "FROM repair.rma WHERE stage = 'shipped' "
            f"AND {_local_date('shipped_at')} BETWEEN %s AND %s",
            (prev_start, prev_end),
        ).fetchone()

    labels = [(start + timedelta(days=i)).isoformat() for i in range(days)]
    index = {d: i for i, d in enumerate(labels)}
    daily: dict[str, list[int]] = {k: [0] * days for k in ("added",) + STAGE_KEYS}
    for r in daily_rows:
        if r["stage"] in daily:
            daily[r["stage"]][index[r["day"].isoformat()]] = r["n"]

    aging = {k: [0] * len(AGE_BUCKETS) for k in BOARD_STAGES if k != "shipped"}
    for r in open_rows:
        if r["stage"] in aging:
            aging[r["stage"]][_bucket(float(r["days"] or 0), AGE_BUCKETS)] += 1

    spread = {"total": [0] * len(TURNAROUND_BUCKETS), "shop": [0] * len(TURNAROUND_BUCKETS)}
    for r in shipped_rows:
        if r["total_days"] is not None:
            spread["total"][_bucket(float(r["total_days"]), TURNAROUND_BUCKETS)] += 1
        if r["shop_days"] is not None:
            spread["shop"][_bucket(float(r["shop_days"]), TURNAROUND_BUCKETS)] += 1

    prev_avg = prev_turn["avg_shop_days"] if prev_turn else None
    return {
        "days": labels,
        "daily": daily,
        "age_buckets": [b[2] for b in AGE_BUCKETS],
        "aging": aging,
        "turnaround_buckets": [b[2] for b in TURNAROUND_BUCKETS],
        "spread": spread,
        "previous": {
            "start": prev_start.isoformat(),
            "end": prev_end.isoformat(),
            "counts": {r["stage"]: r["n"] for r in prev_counts},
            "avg_shop_days": float(prev_avg) if prev_avg is not None else None,
        },
    }
