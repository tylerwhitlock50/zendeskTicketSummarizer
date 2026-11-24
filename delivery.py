"""Return-shipment delivery: check tracking, and when a rifle is delivered post
the "delivered" public comment (Zendesk emails the customer) once.

Run the checker on a schedule (the docker-compose ``delivery-checker`` service
does this every 30 minutes):

    python delivery.py            # one pass over every shipped, undelivered RMA
    python delivery.py --loop 1800

Each RMA is checked at most once an hour. The per-ticket lock and the
delivered_at check mean two checkers (or a tech pressing "Mark delivered" at
the same time) still email the customer only once.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import repair_db
import repair_settings
import tracking
from zendesk_client import ZendeskError

logger = logging.getLogger("delivery")


def _parse_when(raw):
    if not raw:
        return None
    if isinstance(raw, datetime):
        return raw
    try:
        when = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    return when if when.tzinfo else when.replace(tzinfo=timezone.utc)


def delivered_message(rma, delivered_at, settings) -> str:
    import portal_views  # late import: portal_views imports this module

    when = (delivered_at or datetime.now(timezone.utc)).astimezone(ZoneInfo(repair_db.REPORT_TZ))
    values = {
        **portal_views._base_values(rma),
        "delivered_date": when.strftime("%B %d").replace(" 0", " "),
        "ticket_id": str(rma["ticket_id"]),
    }
    return portal_views.render_message(settings["delivered_message"], values).strip()


def mark_delivered(zendesk, ticket_id, *, delivered_at=None, detail=None, tech=None, how="tracking",
                   email=True, settings=None) -> str:
    """Record delivery and (optionally) email the customer. Returns what happened:

    "emailed", "recorded" (no email: turned off, or the ticket is closed),
    "already" (someone else marked it first), or "not_shipped".
    """
    settings = settings or repair_settings.load()
    delivered_at = _parse_when(delivered_at)
    with repair_db.ticket_lock(ticket_id) as conn:
        if repair_db.is_delivered(conn, ticket_id):
            return "already"
        rma = repair_db.get_rma(ticket_id)
        if not rma or rma["stage"] != "shipped":
            return "not_shipped"
        body = None
        outcome = "recorded"
        if email:
            body = delivered_message(rma, delivered_at, settings)
            custom_fields = {}
            field_id = os.environ.get("ZENDESK_STAGE_FIELD_ID", "").strip()
            if field_id.isdigit() and settings.get("delivered_zendesk_value"):
                custom_fields[int(field_id)] = settings["delivered_zendesk_value"]
            try:
                zendesk.post_update(
                    ticket_id, body, public=True,
                    tags=[os.environ.get("PORTAL_TAG", "repair-portal"), "rp-delivered"],
                    custom_fields=custom_fields,
                )
                outcome = "emailed"
            except ZendeskError as exc:
                if how != "tracking":
                    raise
                # Closed ticket or similar: still record the delivery; CS sees it on the record.
                logger.warning("Ticket %s delivered but the email failed: %s", ticket_id, exc)
                body = None
        repair_db.record_delivered(conn, ticket_id, delivered_at, detail, tech, body, how)
    return outcome


def check_one(zendesk, rma, settings) -> str:
    """Look up one shipment; mark it delivered when the carrier says so. Returns the status."""
    result = tracking.check(rma["tracking_number"], rma.get("return_carrier"))
    if result["status"] == "delivered":
        mark_delivered(
            zendesk, rma["ticket_id"], delivered_at=result.get("delivered_at"), detail=result.get("detail"),
            email=settings.get("delivered_auto") == "1", settings=settings,
        )
    else:
        repair_db.set_delivery_status(rma["ticket_id"], result["status"], result.get("detail"))
    return result["status"]


def check_all(zendesk, recheck_minutes=60) -> dict[str, int]:
    """One pass over shipped, undelivered RMAs. Returns counts by status (plus "errors")."""
    counts: dict[str, int] = {}
    if not tracking.provider_ready():
        logger.info("No tracking service configured (TRACKING_PROVIDER); nothing to check.")
        return counts
    settings = repair_settings.load()
    for rma in repair_db.awaiting_delivery(recheck_minutes=recheck_minutes):
        try:
            status = check_one(zendesk, rma, settings)
        except (tracking.TrackingError, ZendeskError) as exc:
            logger.warning("Ticket %s: tracking check failed: %s", rma["ticket_id"], exc)
            repair_db.set_delivery_status(rma["ticket_id"], rma.get("delivery_status") or "unknown", str(exc))
            status = "errors"
        counts[status] = counts.get(status, 0) + 1
    return counts


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--loop", type=int, default=0, help="repeat every N seconds (0 = run once)")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    from dotenv import load_dotenv

    load_dotenv()
    import db
    from zendesk_client import ZendeskClient

    dsn = os.environ.get("DATABASE_URL") or os.environ.get("RTM_DATABASE_URL")
    if not dsn:
        print("DATABASE_URL is not set", file=sys.stderr)
        return 1
    db.init_pool(dsn)
    zendesk = ZendeskClient(os.environ["ZENDESK_SUBDOMAIN"], os.environ["ZENDESK_EMAIL"],
                            os.environ["ZENDESK_API_TOKEN"])
    while True:
        try:
            logger.info("Delivery check: %s", check_all(zendesk) or "nothing due")
        except Exception:
            logger.exception("Delivery check failed")
            if not args.loop:
                return 1
        if not args.loop:
            return 0
        time.sleep(args.loop)


if __name__ == "__main__":
    sys.exit(main())
