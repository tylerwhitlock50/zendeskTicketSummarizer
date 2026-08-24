"""Postgres data layer for the RTM (rifle return) tracker.

Plain SQL via psycopg v3 with a connection pool. All tables live in the ``rtm``
schema; run ``python migrations/migrate.py`` to create them. Timestamps are
stored UTC (timestamptz) — rendering in local time is the views' job.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import datetime
from decimal import Decimal
from typing import Any, Iterator, Optional

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

STATUSES = ["received", "in_inspection", "in_repair", "qc_test", "ready", "shipped", "closed"]

# from-status -> set of allowed to-statuses
ALLOWED_TRANSITIONS: dict[str, set[str]] = {
    "received": {"in_inspection"},
    "in_inspection": {"in_repair", "qc_test"},
    "in_repair": {"qc_test"},
    "qc_test": {"ready", "shipped", "in_repair"},
    "ready": {"shipped", "in_repair"},
    "shipped": {"closed"},
    "closed": set(),
}

_pool: Optional[ConnectionPool] = None


class InvalidTransition(Exception):
    """Raised when a status change is not allowed from the current status."""


class OpenSessionError(Exception):
    """Raised when a tech already has an open work session somewhere."""


def init_pool(dsn: str) -> None:
    """Create the module-level connection pool. Safe to call once at startup."""
    global _pool
    if _pool is None:
        _pool = ConnectionPool(
            dsn, min_size=1, max_size=8, open=True, kwargs={"row_factory": dict_row}
        )


def close_pool() -> None:
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None


@contextmanager
def _conn() -> Iterator[psycopg.Connection]:
    if _pool is None:
        raise RuntimeError("rtm_db.init_pool() has not been called")
    with _pool.connection() as conn:
        yield conn


# ---------------------------------------------------------------- lookups


def get_lookups() -> dict[str, list[dict[str, Any]]]:
    """Active lookup values and techs for form rendering."""
    out: dict[str, list[dict[str, Any]]] = {}
    with _conn() as conn:
        for table in ("root_cause", "responsibility", "resolution"):
            rows = conn.execute(
                f"SELECT id, code, label FROM rtm.{table} WHERE active ORDER BY sort_order, id"
            ).fetchall()
            out[table] = rows
        out["techs"] = conn.execute(
            "SELECT id, name FROM rtm.tech WHERE active ORDER BY name"
        ).fetchall()
    return out


def get_config(key: str, default: str) -> str:
    with _conn() as conn:
        row = conn.execute("SELECT value FROM rtm.config WHERE key = %s", (key,)).fetchone()
    return row["value"] if row else default


def set_config(key: str, value: str) -> None:
    with _conn() as conn:
        conn.execute(
            "INSERT INTO rtm.config (key, value) VALUES (%s, %s) "
            "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
            (key, value),
        )


# ---------------------------------------------------------------- create / read


def create_rtm(
    *,
    serial_no: str,
    visual: Any = None,
    zendesk_ticket_id: Optional[int] = None,
    reason_for_return: Optional[str] = None,
    created_by: Optional[int] = None,
    manual_fields: Optional[dict[str, Any]] = None,
) -> int:
    """Insert a new RTM and its initial 'received' status event; return its id.

    ``visual`` is a SerialInfo-like object (duck-typed) or None. ``manual_fields``
    may override model / caliber / barrel_length.
    """
    part_id = None
    model = caliber = barrel_length = None
    build_date = ship_date = None
    original_order_id = original_customer_id = None
    snapshot = None

    if visual is not None:
        part_id = getattr(visual, "part_id", None)
        build_date = getattr(visual, "build_date", None)
        ship_date = getattr(visual, "ship_date", None)
        original_order_id = getattr(visual, "original_order_id", None)
        original_customer_id = getattr(visual, "original_customer_id", None)
        product = getattr(visual, "product", None) or {}
        model = product.get("model")
        caliber = product.get("caliber")
        barrel_length = product.get("barrel_length")
        snapshot = json.dumps(
            {"product": product, "transactions": getattr(visual, "transactions", None)},
            default=str,
        )

    manual_fields = manual_fields or {}
    model = manual_fields.get("model") or model
    caliber = manual_fields.get("caliber") or caliber
    barrel_length = manual_fields.get("barrel_length") or barrel_length

    with _conn() as conn:
        with conn.transaction():
            year = conn.execute("SELECT extract(year FROM now())::int AS y").fetchone()["y"]
            # Serialize number generation per-year so two intakes cannot collide.
            conn.execute("SELECT pg_advisory_xact_lock(hashtext('rtm_number'), %s)", (year,))
            seq = conn.execute(
                "SELECT count(*) + 1 AS n FROM rtm.rtm WHERE rtm_number LIKE %s",
                (f"RTM-{year}-%",),
            ).fetchone()["n"]
            rtm_number = f"RTM-{year}-{seq:05d}"

            repeat = conn.execute(
                "SELECT EXISTS (SELECT 1 FROM rtm.rtm WHERE serial_no = %s) AS r", (serial_no,)
            ).fetchone()["r"]

            row = conn.execute(
                """
                INSERT INTO rtm.rtm
                    (rtm_number, serial_no, part_id, model, caliber, barrel_length,
                     visual_snapshot, build_date, ship_date, original_order_id,
                     original_customer_id, zendesk_ticket_id, reason_for_return,
                     repeat_return, created_by)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id
                """,
                (
                    rtm_number,
                    serial_no,
                    part_id,
                    model,
                    caliber,
                    barrel_length,
                    snapshot,
                    build_date,
                    ship_date,
                    original_order_id,
                    original_customer_id,
                    zendesk_ticket_id,
                    reason_for_return,
                    repeat,
                    created_by,
                ),
            ).fetchone()
            rtm_id = row["id"]
            conn.execute(
                "INSERT INTO rtm.status_event (rtm_id, status, tech_id) VALUES (%s, 'received', %s)",
                (rtm_id, created_by),
            )
    return rtm_id


def get_rtm(rtm_id: int) -> Optional[dict[str, Any]]:
    """One RTM row plus events, sessions, part lines, and computed cost fields."""
    with _conn() as conn:
        rtm = conn.execute(
            """
            SELECT r.*,
                   rc.code AS root_cause_code, rc.label AS root_cause_label,
                   rs.code AS responsibility_code, rs.label AS responsibility_label,
                   re.code AS resolution_code, re.label AS resolution_label,
                   ct.name AS created_by_name
              FROM rtm.rtm r
              LEFT JOIN rtm.root_cause rc ON rc.id = r.root_cause_id
              LEFT JOIN rtm.responsibility rs ON rs.id = r.responsibility_id
              LEFT JOIN rtm.resolution re ON re.id = r.resolution_id
              LEFT JOIN rtm.tech ct ON ct.id = r.created_by
             WHERE r.id = %s
            """,
            (rtm_id,),
        ).fetchone()
        if rtm is None:
            return None

        rtm["status_events"] = conn.execute(
            """
            SELECT e.id, e.status, e.changed_at, e.tech_id, t.name AS tech_name
              FROM rtm.status_event e LEFT JOIN rtm.tech t ON t.id = e.tech_id
             WHERE e.rtm_id = %s ORDER BY e.changed_at, e.id
            """,
            (rtm_id,),
        ).fetchall()

        rtm["sessions"] = conn.execute(
            """
            SELECT s.id, s.tech_id, t.name AS tech_name, s.started_at, s.ended_at, s.note,
                   (s.ended_at IS NULL) AS open,
                   EXTRACT(EPOCH FROM (COALESCE(s.ended_at, now()) - s.started_at)) / 3600.0
                       AS hours
              FROM rtm.work_session s JOIN rtm.tech t ON t.id = s.tech_id
             WHERE s.rtm_id = %s ORDER BY s.started_at, s.id
            """,
            (rtm_id,),
        ).fetchall()

        rtm["part_lines"] = conn.execute(
            """
            SELECT p.id, p.part_id, p.description, p.qty, p.unit_cost,
                   (p.qty * p.unit_cost) AS line_cost,
                   p.added_by, t.name AS added_by_name, p.added_at
              FROM rtm.part_line p LEFT JOIN rtm.tech t ON t.id = p.added_by
             WHERE p.rtm_id = %s ORDER BY p.added_at, p.id
            """,
            (rtm_id,),
        ).fetchall()

    parts_cost = sum((line["qty"] * line["unit_cost"] for line in rtm["part_lines"]), Decimal("0"))
    labor_hours = sum((Decimal(str(s["hours"])) for s in rtm["sessions"]), Decimal("0"))
    labor_hours = labor_hours.quantize(Decimal("0.01"))
    rate = Decimal(get_config("labor_rate_per_hour", "85.00"))
    labor_cost = (labor_hours * rate).quantize(Decimal("0.01"))

    end: datetime = rtm["shipped_at"] or rtm["closed_at"] or datetime.now(
        rtm["received_at"].tzinfo
    )
    rtm["parts_cost"] = parts_cost
    rtm["labor_hours"] = labor_hours
    rtm["labor_cost"] = labor_cost
    rtm["total_cost"] = parts_cost + labor_cost
    rtm["calendar_days"] = (end - rtm["received_at"]).days
    return rtm


def list_open_rtms() -> list[dict[str, Any]]:
    """All non-closed RTMs for the worklist, oldest first."""
    with _conn() as conn:
        return conn.execute(
            """
            SELECT r.id, r.rtm_number, r.serial_no, r.model, r.caliber, r.status,
                   r.repeat_return, r.received_at, r.zendesk_ticket_id,
                   r.on_hold, r.hold_reason, r.shipped_at,
                   (SELECT count(*) FROM rtm.work_session s
                     WHERE s.rtm_id = r.id AND s.ended_at IS NULL) AS open_sessions
              FROM rtm.rtm r
             WHERE r.status <> 'closed'
             ORDER BY r.received_at, r.id
            """
        ).fetchall()


def history_for_serial(serial_no: str) -> list[dict[str, Any]]:
    """Prior RTMs for a serial, newest first."""
    with _conn() as conn:
        return conn.execute(
            """
            SELECT r.id, r.rtm_number, r.status, r.received_at, r.created_at,
                   r.closed_at, r.reason_for_return, re.label AS resolution_label
              FROM rtm.rtm r LEFT JOIN rtm.resolution re ON re.id = r.resolution_id
             WHERE r.serial_no = %s ORDER BY r.received_at DESC, r.id DESC
            """,
            (serial_no,),
        ).fetchall()


# ---------------------------------------------------------------- workflow


def transition(rtm_id: int, to_status: str, tech_id: Optional[int] = None) -> None:
    """Move an RTM to a new status, recording a status event.

    Raises InvalidTransition if the move is not allowed from the current status.
    """
    if to_status not in STATUSES:
        raise InvalidTransition(f"Unknown status {to_status!r}")
    with _conn() as conn:
        with conn.transaction():
            row = conn.execute(
                "SELECT status FROM rtm.rtm WHERE id = %s FOR UPDATE", (rtm_id,)
            ).fetchone()
            if row is None:
                raise InvalidTransition(f"RTM {rtm_id} not found")
            current = row["status"]
            if to_status not in ALLOWED_TRANSITIONS.get(current, set()):
                raise InvalidTransition(f"Cannot go from {current} to {to_status}")
            conn.execute(
                """
                UPDATE rtm.rtm
                   SET status = %s,
                       shipped_at = CASE WHEN %s = 'shipped' THEN now() ELSE shipped_at END,
                       closed_at  = CASE WHEN %s = 'closed'  THEN now() ELSE closed_at  END,
                       updated_at = now()
                 WHERE id = %s
                """,
                (to_status, to_status, to_status, rtm_id),
            )
            conn.execute(
                "INSERT INTO rtm.status_event (rtm_id, status, tech_id) VALUES (%s, %s, %s)",
                (rtm_id, to_status, tech_id),
            )


def set_hold(rtm_id: int, on_hold: bool, reason: Optional[str] = None) -> None:
    """Flag an RTM as waiting (e.g. on parts or customer) or clear the flag."""
    with _conn() as conn:
        conn.execute(
            """
            UPDATE rtm.rtm
               SET on_hold = %s,
                   hold_reason = CASE WHEN %s THEN %s ELSE NULL END,
                   updated_at = now()
             WHERE id = %s
            """,
            (on_hold, on_hold, reason, rtm_id),
        )


def clock_in(rtm_id: int, tech_id: int) -> None:
    """Start a work session. Raises OpenSessionError if the tech is already clocked in anywhere."""
    with _conn() as conn:
        try:
            conn.execute(
                "INSERT INTO rtm.work_session (rtm_id, tech_id) VALUES (%s, %s)",
                (rtm_id, tech_id),
            )
        except psycopg.errors.UniqueViolation:
            raise OpenSessionError(
                "Tech already has an open work session; clock out first."
            ) from None


def clock_out(rtm_id: int, tech_id: int) -> None:
    """End this tech's open session on this RTM."""
    with _conn() as conn:
        cur = conn.execute(
            """
            UPDATE rtm.work_session SET ended_at = now()
             WHERE rtm_id = %s AND tech_id = %s AND ended_at IS NULL
            """,
            (rtm_id, tech_id),
        )
        if cur.rowcount == 0:
            raise OpenSessionError("No open work session on this RTM for that tech.")


# ---------------------------------------------------------------- parts / findings


def add_part_line(
    rtm_id: int,
    part_id: str,
    description: Optional[str],
    qty: Decimal,
    unit_cost: Decimal,
    tech_id: Optional[int],
) -> int:
    with _conn() as conn:
        row = conn.execute(
            """
            INSERT INTO rtm.part_line (rtm_id, part_id, description, qty, unit_cost, added_by)
            VALUES (%s, %s, %s, %s, %s, %s) RETURNING id
            """,
            (rtm_id, part_id, description, qty, unit_cost, tech_id),
        ).fetchone()
    return row["id"]


def delete_part_line(rtm_id: int, line_id: int) -> None:
    with _conn() as conn:
        conn.execute(
            "DELETE FROM rtm.part_line WHERE id = %s AND rtm_id = %s", (line_id, rtm_id)
        )


_FINDINGS_FIELDS = {
    "inspection_notes",
    "findings",
    "root_cause_id",
    "responsibility_id",
    "resolution_id",
    "round_count",
    "ammo_used",
}


def save_findings(rtm_id: int, **fields: Any) -> None:
    """Update inspection/findings columns; only whitelisted fields are accepted."""
    unknown = set(fields) - _FINDINGS_FIELDS
    if unknown:
        raise ValueError(f"Unknown findings fields: {sorted(unknown)}")
    if not fields:
        return
    sets = ", ".join(f"{name} = %s" for name in fields)
    params = list(fields.values()) + [rtm_id]
    with _conn() as conn:
        conn.execute(f"UPDATE rtm.rtm SET {sets}, updated_at = now() WHERE id = %s", params)


def link_ticket(rtm_id: int, zendesk_ticket_id: int) -> None:
    with _conn() as conn:
        conn.execute(
            "UPDATE rtm.rtm SET zendesk_ticket_id = %s, updated_at = now() WHERE id = %s",
            (zendesk_ticket_id, rtm_id),
        )


def set_reason(rtm_id: int, text: str) -> None:
    with _conn() as conn:
        conn.execute(
            "UPDATE rtm.rtm SET reason_for_return = %s, updated_at = now() WHERE id = %s",
            (text, rtm_id),
        )


def close_rtm(rtm_id: int, tech_id: Optional[int] = None) -> None:
    """Close an RTM. Raises ValueError if root cause / responsibility / resolution unset."""
    with _conn() as conn:
        row = conn.execute(
            "SELECT root_cause_id, responsibility_id, resolution_id FROM rtm.rtm WHERE id = %s",
            (rtm_id,),
        ).fetchone()
    if row is None:
        raise ValueError(f"RTM {rtm_id} not found")
    missing = [
        name
        for name in ("root_cause_id", "responsibility_id", "resolution_id")
        if row[name] is None
    ]
    if missing:
        raise ValueError(
            "Cannot close: set root cause, responsibility, and resolution first "
            f"(missing {', '.join(missing)})."
        )
    transition(rtm_id, "closed", tech_id)


# ---------------------------------------------------------------- reporting


def monthly_report(year: int, month: int) -> dict[str, Any]:
    """Monthly metrics, all computed Postgres-side.

    Counts / pareto / splits / repeats use RTMs *received* in the month;
    cost and turnaround metrics use RTMs *closed* in the month.
    """
    start = f"{year:04d}-{month:02d}-01"
    rate = Decimal(get_config("labor_rate_per_hour", "85.00"))
    with _conn() as conn:
        recv = "r.received_at >= %s::date AND r.received_at < %s::date + interval '1 month'"
        closed = "r.closed_at >= %s::date AND r.closed_at < %s::date + interval '1 month'"

        rtm_counts = conn.execute(
            f"""
            SELECT r.model, r.caliber, count(*) AS rtm_count
              FROM rtm.rtm r WHERE {recv}
             GROUP BY r.model, r.caliber ORDER BY rtm_count DESC, r.model, r.caliber
            """,
            (start, start),
        ).fetchall()

        root_cause_pareto = conn.execute(
            f"""
            SELECT COALESCE(rc.label, 'Unset') AS label, count(*) AS rtm_count
              FROM rtm.rtm r LEFT JOIN rtm.root_cause rc ON rc.id = r.root_cause_id
             WHERE {recv}
             GROUP BY COALESCE(rc.label, 'Unset') ORDER BY rtm_count DESC, label
            """,
            (start, start),
        ).fetchall()

        responsibility_split = conn.execute(
            f"""
            SELECT COALESCE(rs.label, 'Unset') AS label, count(*) AS rtm_count
              FROM rtm.rtm r LEFT JOIN rtm.responsibility rs ON rs.id = r.responsibility_id
             WHERE {recv}
             GROUP BY COALESCE(rs.label, 'Unset') ORDER BY rtm_count DESC, label
            """,
            (start, start),
        ).fetchall()

        resolution_split = conn.execute(
            f"""
            SELECT COALESCE(re.label, 'Unset') AS label, count(*) AS rtm_count
              FROM rtm.rtm r LEFT JOIN rtm.resolution re ON re.id = r.resolution_id
             WHERE {recv}
             GROUP BY COALESCE(re.label, 'Unset') ORDER BY rtm_count DESC, label
            """,
            (start, start),
        ).fetchall()

        received_count = conn.execute(
            f"SELECT count(*) AS n FROM rtm.rtm r WHERE {recv}", (start, start)
        ).fetchone()["n"]

        closed_metrics = conn.execute(
            f"""
            WITH closed_rtms AS (
                SELECT r.id, r.received_at, r.shipped_at, r.closed_at,
                       COALESCE((SELECT sum(p.qty * p.unit_cost)
                                   FROM rtm.part_line p WHERE p.rtm_id = r.id), 0) AS parts_cost,
                       COALESCE((SELECT sum(EXTRACT(EPOCH FROM (s.ended_at - s.started_at)))
                                   FROM rtm.work_session s
                                  WHERE s.rtm_id = r.id AND s.ended_at IS NOT NULL), 0) / 3600.0
                           AS touch_hours
                  FROM rtm.rtm r
                 WHERE r.status = 'closed' AND {closed}
            )
            SELECT count(*) AS closed_count,
                   round(avg(parts_cost + touch_hours * %s)::numeric, 2) AS avg_total_cost,
                   round(sum(parts_cost + touch_hours * %s)::numeric, 2) AS total_cost,
                   round(avg(touch_hours)::numeric, 2) AS avg_touch_hours,
                   round(avg(EXTRACT(EPOCH FROM
                       (COALESCE(shipped_at, closed_at) - received_at)) / 86400.0)::numeric, 1)
                       AS avg_calendar_days
              FROM closed_rtms
            """,
            (start, start, rate, rate),
        ).fetchone()

        repeat_returns = conn.execute(
            f"""
            SELECT r.id, r.rtm_number, r.serial_no, r.model, r.caliber, r.status, r.received_at
              FROM rtm.rtm r WHERE {recv} AND r.repeat_return
             ORDER BY r.received_at
            """,
            (start, start),
        ).fetchall()

    return {
        "year": year,
        "month": month,
        "rtm_counts": rtm_counts,
        "root_cause_pareto": root_cause_pareto,
        "responsibility_split": responsibility_split,
        "resolution_split": resolution_split,
        "received_count": received_count,
        "closed_count": closed_metrics["closed_count"],
        "avg_total_cost": closed_metrics["avg_total_cost"],
        "total_cost": closed_metrics["total_cost"],
        "avg_touch_hours": closed_metrics["avg_touch_hours"],
        "avg_calendar_days": closed_metrics["avg_calendar_days"],
        "repeat_returns": repeat_returns,
    }
