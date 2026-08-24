"""Smoke tests for the RTM tracker: intake -> workflow -> parts/labor -> close -> reports.

Runs against the Postgres named by RTM_DATABASE_URL (docker-compose.dev.yml default:
postgresql://postgres:rtm_dev@localhost:5433/rtm_dev). DB-backed tests skip cleanly
when the database is unset or unreachable. VISUAL access is monkeypatched throughout.
"""

import os
import sys
from datetime import date
from decimal import Decimal

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

DEFAULT_DSN = "postgresql://postgres:rtm_dev@localhost:5433/rtm_dev"
os.environ.setdefault("RTM_DATABASE_URL", DEFAULT_DSN)

DSN = os.environ["RTM_DATABASE_URL"]


def _db_available():
    try:
        import psycopg

        with psycopg.connect(DSN, connect_timeout=3):
            return True
    except Exception:
        return False


DB_OK = _db_available()

import visual_client  # noqa: E402
from visual_client import SerialInfo, VisualError  # noqa: E402

SERIAL = "CV126868"
PART_ID = "801-06444-01"
MODEL = "RIDGELINE FLASH FORGED TECHNOLOGY"
CALIBER = "7MM BACKCOUNTRY"

FIXTURE_SERIAL = SerialInfo(
    serial=SERIAL,
    part_id=PART_ID,
    product={
        "model": MODEL,
        "caliber": CALIBER,
        "barrel_length": 16,
        "part_id": PART_ID,
        "description": "RIDGELINE FFT 7MM BC",
    },
    build_date=date(2026, 8, 24),
    ship_date=date(2026, 8, 24),
    original_order_id="SO-129445",
    original_customer_id="BILL HICK",
    transactions=[],
)

FIXTURE_PART = {
    "part_id": PART_ID,
    "description": "RIDGELINE FFT 7MM BC",
    "unit_cost": Decimal("123.4500"),
}


def fake_lookup_serial(serial):
    serial = serial.strip().upper()
    if serial == SERIAL:
        return FIXTURE_SERIAL
    return SerialInfo(
        serial=serial, part_id=None, product=None, build_date=None, ship_date=None,
        original_order_id=None, original_customer_id=None, transactions=[],
    )


def fake_lookup_part(part_id):
    if part_id.strip().upper() == PART_ID.upper():
        return dict(FIXTURE_PART)
    return None


def fake_shipped_units(start, end):
    return [{"model": MODEL, "caliber": CALIBER, "units_shipped": 200}]


@pytest.fixture(autouse=True)
def _patch_visual(monkeypatch):
    monkeypatch.setattr(visual_client, "lookup_serial", fake_lookup_serial)
    monkeypatch.setattr(visual_client, "lookup_part", fake_lookup_part)
    monkeypatch.setattr(visual_client, "shipped_units", fake_shipped_units)


@pytest.fixture(scope="module")
def flask_app():
    if not DB_OK:
        pytest.skip(f"RTM database not reachable at {DSN}")
    from app import app as flask_app

    flask_app.config["TESTING"] = True

    import rtm_db

    with rtm_db._conn() as conn:
        conn.execute(
            "TRUNCATE rtm.part_line, rtm.work_session, rtm.status_event, rtm.rtm "
            "RESTART IDENTITY CASCADE"
        )
    return flask_app


@pytest.fixture
def client(flask_app):
    return flask_app.test_client()


@pytest.fixture(scope="module")
def techs(flask_app):
    import rtm_db

    lookups = rtm_db.get_lookups()
    assert len(lookups["techs"]) >= 2, "seed data should provide at least two techs"
    return lookups["techs"]


def _create_rtm(client, techs, serial=SERIAL):
    resp = client.post(
        "/rtm/intake",
        data={"serial_no": serial, "tech_id": str(techs[0]["id"])},
        follow_redirects=False,
    )
    assert resp.status_code == 302
    location = resp.headers["Location"]
    return int(location.rstrip("/").rsplit("/", 1)[-1])


# ---------------------------------------------------------------- regression


def test_index_still_works(client):
    assert client.get("/").status_code == 200


# ---------------------------------------------------------------- intake


def test_intake_creates_rtm_with_snapshot(client, techs):
    import rtm_db

    rtm_id = _create_rtm(client, techs)
    rtm = rtm_db.get_rtm(rtm_id)
    assert rtm is not None
    assert rtm["serial_no"] == SERIAL
    assert rtm["part_id"] == PART_ID
    assert rtm["model"] == MODEL
    assert rtm["caliber"] == CALIBER
    assert str(rtm["barrel_length"]) == "16"
    assert rtm["build_date"] == date(2026, 8, 24)
    assert rtm["ship_date"] == date(2026, 8, 24)
    assert rtm["original_order_id"] == "SO-129445"
    assert rtm["original_customer_id"] == "BILL HICK"
    assert rtm["status"] == "received"
    assert rtm["repeat_return"] is False
    import re

    assert re.fullmatch(r"RTM-\d{4}-\d{5}", rtm["rtm_number"])


def test_second_intake_same_serial_is_repeat(client, techs):
    import rtm_db

    rtm_id = _create_rtm(client, techs)
    rtm = rtm_db.get_rtm(rtm_id)
    assert rtm["repeat_return"] is True


def test_serial_api(client):
    data = client.get(f"/rtm/api/serial/{SERIAL}").get_json()
    assert data["found"] is True
    assert data["part_id"] == PART_ID
    assert data["model"] == MODEL
    assert data["caliber"] == CALIBER
    assert data["repeat"] is True
    assert len(data["prior_rtms"]) >= 2

    miss = client.get("/rtm/api/serial/NOPE123").get_json()
    assert miss["found"] is False


# ---------------------------------------------------------------- worklist


def test_worklist_renders(client):
    resp = client.get("/rtm/")
    assert resp.status_code == 200
    assert SERIAL.encode() in resp.data


# ---------------------------------------------------------------- workflow


def test_illegal_transition_is_rejected(client, techs):
    import rtm_db

    rtm_id = _create_rtm(client, techs)
    resp = client.post(
        f"/rtm/{rtm_id}/status",
        data={"to_status": "shipped", "tech_id": str(techs[0]["id"])},
    )
    assert resp.status_code == 302
    assert rtm_db.get_rtm(rtm_id)["status"] == "received"


def test_clock_in_twice_same_tech_blocked(client, techs):
    import rtm_db

    rtm_id = _create_rtm(client, techs)
    tech_id = str(techs[0]["id"])
    assert client.post(f"/rtm/{rtm_id}/clock-in", data={"tech_id": tech_id}).status_code == 302
    assert client.post(f"/rtm/{rtm_id}/clock-in", data={"tech_id": tech_id}).status_code == 302
    rtm = rtm_db.get_rtm(rtm_id)
    assert len(rtm["sessions"]) == 1  # second clock-in refused
    assert client.post(f"/rtm/{rtm_id}/clock-out", data={"tech_id": tech_id}).status_code == 302
    assert not [s for s in rtm_db.get_rtm(rtm_id)["sessions"] if s["open"]]


# ---------------------------------------------------------------- parts & cost


def test_add_part_snapshots_cost_and_total_math(client, techs):
    import rtm_db

    rtm_id = _create_rtm(client, techs)
    tech_id = str(techs[0]["id"])
    resp = client.post(
        f"/rtm/{rtm_id}/parts",
        data={"part_id": PART_ID, "qty": "2", "tech_id": tech_id},
    )
    assert resp.status_code == 302
    rtm = rtm_db.get_rtm(rtm_id)
    assert len(rtm["part_lines"]) == 1
    line = rtm["part_lines"][0]
    assert line["part_id"] == PART_ID
    assert line["unit_cost"] == Decimal("123.4500")
    assert rtm["parts_cost"] == Decimal("246.9000")
    rate = Decimal(rtm_db.get_config("labor_rate_per_hour", "85.00"))
    assert rtm["labor_cost"] == (rtm["labor_hours"] * rate).quantize(Decimal("0.01"))
    assert rtm["total_cost"] == rtm["parts_cost"] + rtm["labor_cost"]

    unknown = client.post(
        f"/rtm/{rtm_id}/parts", data={"part_id": "NOT-A-PART", "qty": "1", "tech_id": tech_id}
    )
    assert unknown.status_code == 302
    assert len(rtm_db.get_rtm(rtm_id)["part_lines"]) == 1  # not added

    api = client.get(f"/rtm/api/part/{PART_ID}").get_json()
    assert api["found"] is True and api["unit_cost"] == "123.4500"


# ---------------------------------------------------------------- close


def test_close_blocked_then_succeeds(client, techs):
    import rtm_db

    rtm_id = _create_rtm(client, techs)
    tech_id = str(techs[0]["id"])
    for step in ("in_inspection", "in_repair", "qc_test", "shipped"):
        client.post(f"/rtm/{rtm_id}/status", data={"to_status": step, "tech_id": tech_id})
    assert rtm_db.get_rtm(rtm_id)["status"] == "shipped"

    resp = client.post(f"/rtm/{rtm_id}/close", data={"tech_id": tech_id})
    assert resp.status_code == 302
    assert rtm_db.get_rtm(rtm_id)["status"] == "shipped"  # blocked: judgment unset

    lookups = rtm_db.get_lookups()
    resp = client.post(
        f"/rtm/{rtm_id}/findings",
        data={
            "root_cause_id": str(lookups["root_cause"][0]["id"]),
            "responsibility_id": str(lookups["responsibility"][0]["id"]),
            "resolution_id": str(lookups["resolution"][0]["id"]),
            "findings": "Barrel replaced.",
        },
    )
    assert resp.status_code == 302

    resp = client.post(f"/rtm/{rtm_id}/close", data={"tech_id": tech_id})
    assert resp.status_code == 302
    rtm = rtm_db.get_rtm(rtm_id)
    assert rtm["status"] == "closed"
    assert rtm["closed_at"] is not None


# ---------------------------------------------------------------- reports


def test_reports_page_and_rate_math(client):
    resp = client.get("/rtm/reports")
    assert resp.status_code == 200
    assert MODEL.encode() in resp.data

    from rtm_reports import _build_rate_rows

    rows = _build_rate_rows(
        [{"model": MODEL, "caliber": CALIBER, "rtm_count": 5}],
        [{"model": MODEL, "caliber": CALIBER, "units_shipped": 200}],
    )
    assert rows[0]["rtms"] == 5
    assert rows[0]["units_shipped"] == 200
    assert rows[0]["rate_pct"] == 2.5

    csv_resp = client.get("/rtm/reports.csv?report=rate")
    assert csv_resp.status_code == 200
    assert csv_resp.mimetype == "text/csv"


def test_reports_page_when_visual_down(client, monkeypatch):
    def boom(start, end):
        raise VisualError("VISUAL is down")

    monkeypatch.setattr(visual_client, "shipped_units", boom)
    resp = client.get("/rtm/reports")
    assert resp.status_code == 200
    assert b"unavailable" in resp.data.lower()


def test_ready_status_and_board(client, techs):
    rtm_id = _create_rtm(client, techs, serial="CVBOARD01")
    tech = techs[0]["id"]
    for to in ("in_inspection", "qc_test", "ready"):
        resp = client.post(f"/rtm/{rtm_id}/status", data={"to_status": to, "tech_id": tech})
        assert resp.status_code == 302

    import rtm_db

    assert rtm_db.get_rtm(rtm_id)["status"] == "ready"

    resp = client.get("/rtm/board")
    assert resp.status_code == 200
    assert b"Ready" in resp.data and b"Waiting" in resp.data
    assert b"CVBOARD01" in resp.data


def test_hold_flag_moves_to_waiting_column(client, techs):
    rtm_id = _create_rtm(client, techs, serial="CVHOLD01")
    resp = client.post(
        f"/rtm/{rtm_id}/hold", data={"on_hold": "1", "hold_reason": "Waiting on barrel"}
    )
    assert resp.status_code == 302

    import rtm_db

    rtm = rtm_db.get_rtm(rtm_id)
    assert rtm["on_hold"] is True and rtm["hold_reason"] == "Waiting on barrel"

    resp = client.get("/rtm/board")
    assert b"Waiting on barrel" in resp.data

    resp = client.post(f"/rtm/{rtm_id}/hold", data={"on_hold": "0"})
    assert resp.status_code == 302
    rtm = rtm_db.get_rtm(rtm_id)
    assert rtm["on_hold"] is False and rtm["hold_reason"] is None


def test_api_ticket_extracts_serial(client, monkeypatch):
    import rtm_views

    class FakeZendesk:
        def get_ticket(self, ticket_id):
            ticket = {"id": ticket_id, "subject": "Rifle won't group", "ticket_form_id": None,
                      "custom_fields": [{"id": 10, "value": "cv126868"}]}
            return ticket, {"name": "Jane Dealer"}, None

        def get_ticket_fields(self):
            return {10: {"id": 10, "title": "Serial Number", "custom_field_options": []}}

        def get_ticket_form(self, form_id):
            return None

    monkeypatch.setattr(rtm_views, "_get_zendesk", lambda: FakeZendesk())
    resp = client.get("/rtm/api/ticket/5555")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["found"] is True
    assert data["subject"] == "Rifle won't group"
    assert data["requester"] == "Jane Dealer"
    assert data["serial"] == "CV126868"
