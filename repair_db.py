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

from db import _conn

STAGE_KEYS = ("received", "inspected", "repaired", "shipped")
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


def get_choice(choice_id: int, kind: str) -> Optional[dict[str, Any]]:
    with _conn() as conn:
        return conn.execute(
            "SELECT id, kind, label, position, active FROM repair.choice "
            "WHERE id = %s AND kind = %s",
            (choice_id, kind),
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


_RMA_SELECT = (
    "SELECT r.*, p.label AS problem_label, s.label AS solution_label "
    "FROM repair.rma r "
    "LEFT JOIN repair.choice p ON p.id = r.problem_id "
    "LEFT JOIN repair.choice s ON s.id = r.solution_id "
)


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
            conn.execute(
                "INSERT INTO repair.event (ticket_id, kind, label, tech_id, tech_name) "
                "VALUES (%s, 'added', 'Added to repair queue', %s, %s)",
                (ticket_id, tech["id"], tech["name"]),
            )
    return True


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
    allowed = {"problem_id", "problem_notes", "solution_id", "work_done", "tracking_number"}
    cols = {k: v for k, v in fields.items() if k in allowed}
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
    for col, val in cols.items():
        sets.append(f"{col} = %s")
        params.append(val)
    params.append(ticket_id)
    conn.execute(f"UPDATE repair.rma SET {', '.join(sets)} WHERE ticket_id = %s", params)
    detail = dict(cols)
    if moving_back:
        detail["moved_back_from"] = current_stage
    if customer_message is None:
        detail["not_emailed"] = True
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
        problems = conn.execute(
            "SELECT COALESCE(c.label, '(none picked)') AS label, count(*) AS n "
            "FROM repair.rma r LEFT JOIN repair.choice c ON c.id = r.problem_id "
            f"WHERE {_local_date('r.inspected_at')} BETWEEN %s AND %s "
            "GROUP BY 1 ORDER BY n DESC, label",
            rng,
        ).fetchall()
        solutions = conn.execute(
            "SELECT COALESCE(c.label, '(none picked)') AS label, count(*) AS n "
            "FROM repair.rma r LEFT JOIN repair.choice c ON c.id = r.solution_id "
            f"WHERE {_local_date('r.repaired_at')} BETWEEN %s AND %s "
            "GROUP BY 1 ORDER BY n DESC, label",
            rng,
        ).fetchall()
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
    }


def _with_columns(cur) -> tuple[list[str], list[dict[str, Any]]]:
    return [d.name for d in cur.description], cur.fetchall()


def export_rmas(start, end) -> tuple[list[str], list[dict[str, Any]]]:
    """RMAs added or updated in the date range, for CSV download: (columns, rows)."""
    with _conn() as conn:
        return _with_columns(conn.execute(
            "SELECT r.ticket_id, r.subject, r.requester_name, r.requester_email, r.stage, "
            "r.added_by, r.added_at, r.received_at, r.inspected_at, r.repaired_at, r.shipped_at, "
            "p.label AS problem, r.problem_notes, s.label AS solution, r.work_done, r.tracking_number "
            "FROM repair.rma r "
            "LEFT JOIN repair.choice p ON p.id = r.problem_id "
            "LEFT JOIN repair.choice s ON s.id = r.solution_id "
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
