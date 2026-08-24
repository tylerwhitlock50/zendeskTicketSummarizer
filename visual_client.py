"""Read-only client for Infor VISUAL (SQL Server) serial-trace and part data.

All access is strictly read-only SELECTs against the VISUAL database via
pymssql. Connections are short-lived (one per call). Configuration comes
from environment variables:

    VISUAL_DB_HOST, VISUAL_DB_PORT (default 1433), VISUAL_DB_USER,
    VISUAL_DB_PASSWORD, VISUAL_DB_NAME (default VECA),
    VISUAL_SITE_ID (optional; when unset, part costs aggregate across sites).
"""

import os
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal

import pymssql


class VisualError(Exception):
    """VISUAL database failure with a user-presentable message."""


@dataclass
class SerialInfo:
    serial: str
    part_id: str | None
    product: dict | None
    build_date: date | None
    ship_date: date | None
    original_order_id: str | None
    original_customer_id: str | None
    transactions: list[dict] = field(default_factory=list)


SERIAL_SQL = """
SELECT t.PART_ID, it.TRANSACTION_ID, it.TYPE, it.CLASS, it.TRANSACTION_DATE,
       it.CUST_ORDER_ID, it.WORKORDER_TYPE, it.WORKORDER_BASE_ID, tit.QTY, co.CUSTOMER_ID
FROM dbo.TRACE t
JOIN dbo.TRACE_INV_TRANS tit ON tit.PART_ID = t.PART_ID AND tit.TRACE_ID = t.ID
JOIN dbo.INVENTORY_TRANS it ON it.TRANSACTION_ID = tit.TRANSACTION_ID
LEFT JOIN dbo.CUSTOMER_ORDER co ON co.ID = it.CUST_ORDER_ID
WHERE t.ID = %(serial)s ORDER BY it.TRANSACTION_DATE
"""

PRODUCT_SQL = """
SELECT ID, DESCRIPTION, PRODUCT_CODE, chambering, Bar_Length, Twist, Family,
       finish, handedness, action_type, handguard, stock_color, stock_style, UPC
FROM dbo.Z_PRODUCT_DETAIL WHERE ID = %(part_id)s
"""

PART_SQL = """
SELECT p.ID, p.DESCRIPTION,
  COALESCE(ps.UNIT_MATERIAL_COST,0)+COALESCE(ps.UNIT_LABOR_COST,0)+COALESCE(ps.UNIT_BURDEN_COST,0)+COALESCE(ps.UNIT_SERVICE_COST,0) AS UNIT_COST
FROM dbo.PART p
LEFT JOIN dbo.PART_SITE ps ON ps.PART_ID = p.ID AND (%(site_id)s IS NULL OR ps.SITE_ID = %(site_id)s)
WHERE p.ID = %(part_id)s
"""

SHIPPED_SQL = """
SELECT pd.Family AS model, pd.chambering AS caliber, SUM(sl.SHIPPED_QTY) AS units_shipped
FROM dbo.SHIPPER s
JOIN dbo.SHIPPER_LINE sl ON sl.PACKLIST_ID = s.PACKLIST_ID
JOIN dbo.CUST_ORDER_LINE col ON col.CUST_ORDER_ID = sl.CUST_ORDER_ID AND col.LINE_NO = sl.CUST_ORDER_LINE_NO
JOIN dbo.Z_PRODUCT_DETAIL pd ON pd.ID = col.PART_ID
WHERE s.SHIPPED_DATE >= %(start)s AND s.SHIPPED_DATE < %(end)s
  AND pd.chambering IS NOT NULL AND pd.chambering <> 'N/A'
GROUP BY pd.Family, pd.chambering
"""


def _site_id():
    value = os.environ.get("VISUAL_SITE_ID", "").strip()
    return value or None


@contextmanager
def _connection():
    """Yield a short-lived pymssql connection; wrap failures in VisualError."""
    host = os.environ.get("VISUAL_DB_HOST")
    if not host:
        raise VisualError("VISUAL_DB_HOST is not configured.")
    try:
        conn = pymssql.connect(
            server=host,
            port=int(os.environ.get("VISUAL_DB_PORT", "1433")),
            user=os.environ.get("VISUAL_DB_USER"),
            password=os.environ.get("VISUAL_DB_PASSWORD"),
            database=os.environ.get("VISUAL_DB_NAME", "VECA"),
            login_timeout=5,
            as_dict=True,
        )
    except pymssql.Error as exc:
        raise VisualError(f"Could not connect to VISUAL: {exc}") from exc
    try:
        yield conn
    except pymssql.Error as exc:
        raise VisualError(f"VISUAL query failed: {exc}") from exc
    finally:
        conn.close()


def _to_date(value):
    if isinstance(value, datetime):
        return value.date()
    return value  # date or None


def _clean(value):
    """Normalize free-text VISUAL values: literal 'N/A' and blanks -> None."""
    if value is None:
        return None
    if isinstance(value, str):
        value = value.strip()
        if not value or value.upper() == "N/A":
            return None
    return value


def _json_safe(value):
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    return value


def lookup_serial(serial):
    """Trace a serial through VISUAL and return a SerialInfo.

    Unknown serial returns SerialInfo(part_id=None, transactions=[]).
    Raises VisualError only on connection/query failure.
    """
    with _connection() as conn:
        cur = conn.cursor()
        cur.execute(SERIAL_SQL, {"serial": serial})
        rows = cur.fetchall()

        if not rows:
            return SerialInfo(
                serial=serial, part_id=None, product=None, build_date=None,
                ship_date=None, original_order_id=None, original_customer_id=None,
                transactions=[],
            )

        # Shipment: TYPE='O' AND CLASS='I' with a customer order; take the latest.
        shipment = None
        for row in rows:
            if row["TYPE"] == "O" and row["CLASS"] == "I" and row["CUST_ORDER_ID"]:
                shipment = row
        # Work-order build receipts: TYPE='I' AND CLASS='R' with a work order.
        receipts = [
            row for row in rows
            if row["TYPE"] == "I" and row["CLASS"] == "R" and row["WORKORDER_BASE_ID"]
        ]

        ship_date = None
        original_order_id = None
        original_customer_id = None
        if shipment is not None:
            part_id = shipment["PART_ID"]
            ship_date = _to_date(shipment["TRANSACTION_DATE"])
            original_order_id = shipment["CUST_ORDER_ID"]
            original_customer_id = shipment["CUSTOMER_ID"]
        elif receipts:
            part_id = receipts[-1]["PART_ID"]
        else:
            part_id = rows[-1]["PART_ID"]

        # build_date = date of the finished part's WO receipt (latest for that part)
        build_date = None
        for row in receipts:
            if row["PART_ID"] == part_id:
                build_date = _to_date(row["TRANSACTION_DATE"])

        product = None
        if part_id:
            cur.execute(PRODUCT_SQL, {"part_id": part_id})
            prow = cur.fetchone()
            if prow:
                product = {
                    # Contract keys used by rtm_db.create_rtm and the intake API:
                    "model": _clean(prow["Family"]),
                    "caliber": _clean(prow["chambering"]),
                    "barrel_length": _clean(prow["Bar_Length"]),
                    "part_id": prow["ID"],
                    "description": _clean(prow["DESCRIPTION"]),
                    "product_code": _clean(prow["PRODUCT_CODE"]),
                    "chambering": _clean(prow["chambering"]),
                    "bar_length": _clean(prow["Bar_Length"]),
                    "twist": _clean(prow["Twist"]),
                    "family": _clean(prow["Family"]),
                    "finish": _clean(prow["finish"]),
                    "handedness": _clean(prow["handedness"]),
                    "action_type": _clean(prow["action_type"]),
                    "handguard": _clean(prow["handguard"]),
                    "stock_color": _clean(prow["stock_color"]),
                    "stock_style": _clean(prow["stock_style"]),
                    "upc": _clean(prow["UPC"]),
                }

        transactions = [
            {key: _json_safe(value) for key, value in row.items()} for row in rows
        ]

        return SerialInfo(
            serial=serial,
            part_id=part_id,
            product=product,
            build_date=build_date,
            ship_date=ship_date,
            original_order_id=original_order_id,
            original_customer_id=original_customer_id,
            transactions=transactions,
        )


def lookup_part(part_id):
    """Validate a part and return {"part_id", "description", "unit_cost"} or None.

    Without VISUAL_SITE_ID, multiple PART_SITE rows may match; the MAX cost
    across sites is used (documented choice).
    """
    with _connection() as conn:
        cur = conn.cursor()
        cur.execute(PART_SQL, {"part_id": part_id, "site_id": _site_id()})
        rows = cur.fetchall()
        if not rows:
            return None
        costs = [
            Decimal(str(row["UNIT_COST"])) for row in rows if row["UNIT_COST"] is not None
        ]
        return {
            "part_id": rows[0]["ID"],
            "description": rows[0]["DESCRIPTION"],
            "unit_cost": max(costs) if costs else Decimal("0"),
        }


def shipped_units(start, end):
    """Units shipped per (model, caliber) for SHIPPED_DATE in [start, end)."""
    with _connection() as conn:
        cur = conn.cursor()
        cur.execute(SHIPPED_SQL, {"start": start, "end": end})
        return [
            {
                "model": row["model"],
                "caliber": row["caliber"],
                "units_shipped": int(row["units_shipped"] or 0),
            }
            for row in cur.fetchall()
        ]


if __name__ == "__main__":
    import sys
    from pprint import pprint

    if len(sys.argv) != 2:
        print("usage: python visual_client.py <serial>")
        sys.exit(1)
    pprint(lookup_serial(sys.argv[1]))
