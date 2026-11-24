"""Tracking numbers: clean-up, carrier detection, public tracking links, and
delivery status lookups through a tracking service (EasyPost or Shippo).

Set TRACKING_PROVIDER to ``easypost`` or ``shippo`` with its API key
(EASYPOST_API_KEY / SHIPPO_API_KEY). Without one, delivery is marked by hand
on the ticket page ("Mark delivered").
"""

from __future__ import annotations

import os
import re
from typing import Any, Optional

import requests

CARRIER_NAMES = {"ups": "UPS", "fedex": "FedEx", "usps": "USPS", "dhl": "DHL"}

_LINKS = {
    "ups": "https://www.ups.com/track?tracknum={n}",
    "fedex": "https://www.fedex.com/fedextrack/?trknbr={n}",
    "usps": "https://tools.usps.com/go/TrackConfirmAction?tLabels={n}",
    "dhl": "https://www.dhl.com/us-en/home/tracking/tracking-express.html?tracking-id={n}",
}

# Statuses we store (provider statuses are mapped onto these).
STATUS_WORDS = {
    "pre_transit": "Label created, not picked up yet",
    "in_transit": "In transit",
    "out_for_delivery": "Out for delivery",
    "delivered": "Delivered",
    "exception": "Delivery problem",
    "returned": "Returned to sender",
    "unknown": "No tracking information yet",
}


def normalize(raw: str | None) -> str:
    """Upper-case with spaces and dashes removed, for comparing scans."""
    return re.sub(r"[\s\-]+", "", (raw or "")).upper()


def detect_carrier(number: str | None) -> Optional[str]:
    """Best guess at the carrier from the number's shape (None if unsure)."""
    n = normalize(number)
    if not n:
        return None
    if re.fullmatch(r"1Z[0-9A-Z]{16}", n) or re.fullmatch(r"[TK]\d{10}", n):
        return "ups"
    if re.fullmatch(r"(94|93|92|95|82)\d{18,24}", n) or re.fullmatch(r"[A-Z]{2}\d{9}US", n):
        return "usps"
    if re.fullmatch(r"\d{12}|\d{15}|\d{20}|\d{22}", n):
        # 20/22-digit numbers starting 9 are USPS (handled above); the rest FedEx.
        return "fedex"
    if re.fullmatch(r"\d{10}", n):
        return "dhl"
    return None


def carrier_name(carrier: str | None) -> str:
    return CARRIER_NAMES.get(carrier or "", "the carrier")


def tracking_url(number: str | None, carrier: str | None = None) -> str:
    n = normalize(number)
    carrier = carrier or detect_carrier(n)
    if not n or carrier not in _LINKS:
        return ""
    return _LINKS[carrier].format(n=n)


class TrackingError(Exception):
    pass


def provider_name() -> str:
    return (os.environ.get("TRACKING_PROVIDER") or "").strip().lower()


def provider_ready() -> bool:
    name = provider_name()
    return (name == "easypost" and bool(os.environ.get("EASYPOST_API_KEY"))) or (
        name == "shippo" and bool(os.environ.get("SHIPPO_API_KEY"))
    )


def check(number: str, carrier: str | None) -> dict[str, Any]:
    """Ask the tracking service for a shipment's status.

    Returns {"status": one of STATUS_WORDS, "detail": text, "delivered_at": ISO text or None}.
    Raises TrackingError when the service can't answer.
    """
    name = provider_name()
    carrier = carrier or detect_carrier(number)
    if name == "easypost" and os.environ.get("EASYPOST_API_KEY"):
        return _easypost(normalize(number), carrier)
    if name == "shippo" and os.environ.get("SHIPPO_API_KEY"):
        return _shippo(normalize(number), carrier)
    raise TrackingError("No tracking service is set up (TRACKING_PROVIDER).")


def _http(method, url, **kwargs):
    try:
        resp = requests.request(method, url, timeout=30, **kwargs)
    except requests.RequestException as exc:
        raise TrackingError(f"Could not reach the tracking service: {exc}") from exc
    if not resp.ok:
        raise TrackingError(f"Tracking service error {resp.status_code}: {resp.text[:200]}")
    return resp.json()


_EASYPOST_CARRIERS = {"ups": "UPS", "fedex": "FedEx", "usps": "USPS", "dhl": "DHLExpress"}
_EASYPOST_STATUS = {
    "pre_transit": "pre_transit", "in_transit": "in_transit", "out_for_delivery": "out_for_delivery",
    "delivered": "delivered", "return_to_sender": "returned", "failure": "exception",
    "available_for_pickup": "exception", "error": "exception", "cancelled": "exception",
}


def _easypost(number, carrier):
    tracker = {"tracking_code": number}
    if carrier in _EASYPOST_CARRIERS:
        tracker["carrier"] = _EASYPOST_CARRIERS[carrier]
    # EasyPost re-uses an existing tracker for the same number, so this is safe to repeat.
    data = _http("POST", "https://api.easypost.com/v2/trackers",
                 auth=(os.environ["EASYPOST_API_KEY"], ""), json={"tracker": tracker})
    status = _EASYPOST_STATUS.get(data.get("status") or "", "unknown")
    delivered_at = None
    if status == "delivered":
        for d in reversed(data.get("tracking_details") or []):
            if d.get("status") == "delivered":
                delivered_at = d.get("datetime")
                break
    detail = data.get("status_detail") or ""
    details = data.get("tracking_details") or []
    if details and details[-1].get("message"):
        detail = details[-1]["message"]
    return {"status": status, "detail": detail.replace("_", " "), "delivered_at": delivered_at}


_SHIPPO_CARRIERS = {"ups": "ups", "fedex": "fedex", "usps": "usps", "dhl": "dhl_express"}
_SHIPPO_STATUS = {"PRE_TRANSIT": "pre_transit", "TRANSIT": "in_transit", "DELIVERED": "delivered",
                  "RETURNED": "returned", "FAILURE": "exception", "UNKNOWN": "unknown"}


def _shippo(number, carrier):
    if carrier not in _SHIPPO_CARRIERS:
        raise TrackingError("Shippo needs the carrier; this tracking number's carrier wasn't recognised.")
    data = _http("GET", f"https://api.goshippo.com/tracks/{_SHIPPO_CARRIERS[carrier]}/{number}",
                 headers={"Authorization": f"ShippoToken {os.environ['SHIPPO_API_KEY']}"})
    ts = data.get("tracking_status") or {}
    status = _SHIPPO_STATUS.get(ts.get("status") or "", "unknown")
    if status == "in_transit" and "out for delivery" in (ts.get("status_details") or "").lower():
        status = "out_for_delivery"
    return {
        "status": status,
        "detail": ts.get("status_details") or "",
        "delivered_at": ts.get("status_date") if status == "delivered" else None,
    }
