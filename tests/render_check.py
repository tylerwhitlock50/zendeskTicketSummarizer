"""Render every RTM screen against a seeded shop-floor scenario.

Not a unit test: this drives the pages the redesign touched through the states
that actually exercise them — a rifle waiting 27 days on a part, one with a tech
on the clock, one ready to pack out, a repeat return — and fails on any 500,
any unrendered Jinja, or any of the contrast pairs the pass was meant to remove.

    RTM_DATABASE_URL=... python tests/render_check.py
"""

import os
import re
import sys
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault(
    "RTM_DATABASE_URL", "postgresql://postgres:rtm_dev@127.0.0.1:5433/rtm_dev"
)

import visual_client  # noqa: E402
from visual_client import SerialInfo  # noqa: E402

PRODUCTS = {
    "CA24-02255": ("Ridge LR", "6.5 Creedmoor", "24"),
    "CA24-08812": ("Ridge LR", "6.5 Creedmoor", "24"),
    "CA25-00317": ("Mesa FF", "6.5 Creedmoor", "22"),
    "CA25-01044": ("Summit 300", "300 PRC", "26"),
}


def _fake_lookup_serial(serial):
    serial = serial.strip().upper()
    model, caliber, barrel = PRODUCTS.get(serial, (None, None, None))
    return SerialInfo(
        serial=serial,
        part_id="801-06444-01" if model else None,
        product={"model": model, "caliber": caliber, "barrel_length": barrel} if model else None,
        build_date=date(2024, 3, 1),
        ship_date=date(2024, 4, 2),
        original_order_id="SO-129445",
        original_customer_id="DALE FERRIS",
        transactions=[],
    )


def _fake_lookup_part(part_id):
    parts = {
        "BRL-65C-24": ("Barrel, 6.5 CM, 24 in", Decimal("487.50")),
        "TRG-320": ("Trigger assembly, adj.", Decimal("142.00")),
    }
    hit = parts.get(part_id.strip().upper())
    if not hit:
        return None
    return {"part_id": part_id.strip().upper(), "description": hit[0], "unit_cost": hit[1]}


def _fake_shipped_units(start, end):
    return [{"model": "Ridge LR", "caliber": "6.5 Creedmoor", "units_shipped": 200}]


visual_client.lookup_serial = _fake_lookup_serial
visual_client.lookup_part = _fake_lookup_part
visual_client.shipped_units = _fake_shipped_units

import rtm_db  # noqa: E402
import rtm_views  # noqa: E402
from app import app  # noqa: E402

rtm_views.visual_client = visual_client

FAILURES = []
CHECKS = 0


def check(condition, message):
    global CHECKS
    CHECKS += 1
    if not condition:
        FAILURES.append(message)


def seed():
    """Build the scenario the redesign was drawn against."""
    with rtm_db._conn() as conn:
        conn.execute(
            "TRUNCATE rtm.part_line, rtm.work_session, rtm.status_event, rtm.rtm "
            "RESTART IDENTITY CASCADE"
        )
    techs = {t["name"]: t["id"] for t in rtm_db.get_lookups()["techs"]}
    trevor, colton = techs["Trevor"], techs["Colton"]
    now = datetime.now(timezone.utc)
    ids = {}

    def age(rtm_id, days, last_touch_days=None):
        """Backdate an RTM *after* its history exists, so 'untouched for N days'
        is true of every row the bench derives it from — not just received_at."""
        received = now - timedelta(days=days)
        touched = now - timedelta(days=last_touch_days if last_touch_days is not None else days)
        with rtm_db._conn() as conn:
            conn.execute(
                "UPDATE rtm.rtm SET received_at = %s, created_at = %s, updated_at = %s "
                "WHERE id = %s",
                (received, received, touched, rtm_id),
            )
            conn.execute(
                "UPDATE rtm.status_event SET changed_at = "
                "  least(%s, greatest(%s, changed_at)) WHERE rtm_id = %s",
                (touched, received, rtm_id),
            )
            conn.execute(
                "UPDATE rtm.part_line SET added_at = %s WHERE rtm_id = %s AND added_at > %s",
                (touched, rtm_id, touched),
            )
            conn.execute(
                "UPDATE rtm.work_session SET started_at = %s "
                " WHERE rtm_id = %s AND started_at > %s",
                (touched, rtm_id, touched),
            )

    # 27 days in the shop, waiting on a barrel, untouched for 11.
    ids["stuck"] = rtm_db.create_rtm(
        serial_no="CA24-02255", visual=_fake_lookup_serial("CA24-02255"),
        reason_for_return="Barrel swapped at ~800 rounds; will not hold a group.",
        created_by=colton, zendesk_ticket_id=48213,
    )
    rtm_db.transition(ids["stuck"], "in_inspection", colton)
    rtm_db.transition(ids["stuck"], "in_repair", trevor)
    rtm_db.set_hold(ids["stuck"], True, "Barrel blank on backorder")
    age(ids["stuck"], 27, last_touch_days=11)

    # An earlier, finished RTM on the same serial is what makes the next one a
    # repeat return.
    prior = rtm_db.create_rtm(
        serial_no="CA24-08812", visual=_fake_lookup_serial("CA24-08812"),
        reason_for_return="Would not group; recrowned.", created_by=colton,
    )
    for step in ("in_inspection", "in_repair", "qc_test", "shipped"):
        rtm_db.transition(prior, step, trevor)
    rtm_db.save_findings(prior, root_cause_id=4, responsibility_id=4, resolution_id=1,
                         findings="Recrowned.", inspection_notes=None, round_count=None,
                         ammo_used=None)
    rtm_db.close_rtm(prior, trevor)
    age(prior, 200)
    ids["closed"] = prior

    # The repeat: a tech on the clock, parts on it, findings half-filled.
    ids["live"] = rtm_db.create_rtm(
        serial_no="CA24-08812", visual=_fake_lookup_serial("CA24-08812"),
        reason_for_return=(
            "Customer swapped the factory barrel at roughly 800 rounds and the rifle no "
            "longer holds a group — accuracy went from 0.7 MOA to over 2 MOA."
        ),
        created_by=colton, zendesk_ticket_id=48214,
    )
    rtm_db.transition(ids["live"], "in_inspection", trevor)
    rtm_db.transition(ids["live"], "in_repair", trevor)
    rtm_db.add_part_line(ids["live"], "BRL-65C-24", "Barrel, 6.5 CM, 24 in",
                         Decimal("1"), Decimal("487.50"), trevor)
    rtm_db.add_part_line(ids["live"], "TRG-320", "Trigger assembly, adj.",
                         Decimal("1"), Decimal("142.00"), trevor)
    rtm_db.save_findings(
        ids["live"],
        root_cause_id=4,          # Accuracy
        responsibility_id=4,      # Undetermined
        resolution_id=None,       # deliberately unset — must flag REQUIRED TO CLOSE
        findings="Bore scoped: uneven crown wear, action screws below torque spec.",
        inspection_notes="Scoped on arrival.",
        round_count=800,
        ammo_used="Factory match 140gr",
    )
    age(ids["live"], 12, last_touch_days=0)
    rtm_db.clock_in(ids["live"], trevor)

    # Cleared QC, waiting on a shipping label.
    ids["ready"] = rtm_db.create_rtm(
        serial_no="CA25-00317", visual=_fake_lookup_serial("CA25-00317"), created_by=colton
    )
    rtm_db.transition(ids["ready"], "in_inspection", colton)
    rtm_db.transition(ids["ready"], "qc_test", colton)
    rtm_db.transition(ids["ready"], "ready", colton)
    age(ids["ready"], 9, last_touch_days=2)

    # Received yesterday, nothing done yet.
    ids["new"] = rtm_db.create_rtm(
        serial_no="CA25-01044", visual=_fake_lookup_serial("CA25-01044"), created_by=colton
    )
    age(ids["new"], 1)

    # Shipped and fully judged, so the close screen has something to sign off.
    ids["closing"] = rtm_db.create_rtm(
        serial_no="CA23-04410", visual=_fake_lookup_serial("CA23-04410"), created_by=colton,
        reason_for_return="Stock cracked at the recoil lug.",
    )
    for step in ("in_inspection", "in_repair", "qc_test", "shipped"):
        rtm_db.transition(ids["closing"], step, trevor)
    rtm_db.save_findings(ids["closing"], root_cause_id=1, responsibility_id=1, resolution_id=2,
                         findings="Stock replaced under warranty.", inspection_notes=None,
                         round_count=None, ammo_used=None)
    age(ids["closing"], 6, last_touch_days=1)
    return ids, trevor


def body(resp):
    return resp.get_data(as_text=True)


def main():
    app.config["TESTING"] = True
    app.secret_key = "render-check"
    ids, trevor = seed()

    client = app.test_client()

    # ---- every route renders, in both station states ----
    routes = [
        ("bench", "/rtm/"),
        ("bench: mine", "/rtm/?view=mine"),
        ("bench: stale", "/rtm/?view=stale"),
        ("board", "/rtm/board"),
        ("intake", "/rtm/intake"),
        ("reports", "/rtm/reports"),
        ("detail: waiting", f"/rtm/{ids['stuck']}"),
        ("detail: on the clock", f"/rtm/{ids['live']}"),
        ("detail: ready", f"/rtm/{ids['ready']}"),
        ("detail: shipped", f"/rtm/{ids['closing']}"),
        ("detail: closed", f"/rtm/{ids['closed']}"),
        ("findings", f"/rtm/{ids['live']}/findings"),
        ("close: blocked", f"/rtm/{ids['live']}/close"),
        ("close: ready to sign", f"/rtm/{ids['closing']}/close"),
        ("ticket printer index", "/"),
    ]

    pages = {}
    for label, url in routes:
        resp = client.get(url)
        check(resp.status_code == 200, f"{label} ({url}) returned {resp.status_code}")
        html = body(resp)
        pages[label] = html
        check("{{" not in html and "{%" not in html, f"{label} leaked an unrendered Jinja tag")

    # Same sweep with a tech at the station, which changes the verbs and the clock.
    with client.session_transaction() as sess:
        sess["tech_id"] = trevor
    station_pages = {}
    for label, url in routes:
        resp = client.get(url)
        check(resp.status_code == 200, f"{label} with a station tech returned {resp.status_code}")
        station_pages[label] = body(resp)

    bench = station_pages["bench"]
    detail_live = station_pages["detail: on the clock"]
    board = station_pages["board"]

    # ---- contrast: the pairs the pass existed to remove ----
    css = open("static/rtm.css", encoding="utf-8").read()
    check(
        re.search(r"\.status-badge\.danger,\s*\n\.repeat-badge \{[^}]*background: var\(--rtm-danger\)",
                  css),
        "repeat-return badge is not red-filled",
    )
    check("REPEAT RETURN" in bench.upper(), "bench does not flag the repeat return")
    check("clocked-badge" in bench, "no green clocked-in badge in the header")
    check("Tech clocked in" not in bench, "the old grey 'Tech clocked in' badge is still rendered")

    # ---- order: oldest first, not grouped by status ----
    order = re.findall(r'class="bench-serial"[^>]*>([A-Z0-9-]+)<', bench)
    check(order == ["CA24-02255", "CA24-08812", "CA25-00317", "CA23-04410", "CA25-01044"],
          f"bench is not sorted oldest-first: {order}")
    check("worklist-group" not in bench, "the status-grouped worklist markup is still there")

    # ---- the at-risk banner names the right rifle ----
    check("has been in the shop 27 days" in bench, "no at-risk banner for the 27-day rifle")
    check("Nobody has touched it in 11 days" in bench, "banner does not say how long it sat")

    # ---- every row carries a verb, and the verbs are the right ones ----
    for verb in ("Chase it", "Ship it", "Open"):
        check(verb in bench, f"no '{verb}' verb button on the bench")
    check('value="shipped"' in bench, "the 'Ship it' verb does not actually ship")
    # Trevor is already on the clock, and the schema allows one open session per
    # tech — so offering him "Clock in" would only ever flash an error.
    check("Clock in" not in bench,
          "bench offers 'Clock in' to a tech who is already on the clock elsewhere")
    check("Clock in" not in pages["bench"],
          "bench offers 'Clock in' with nobody at the station")

    # ---- detail: reason before spec, findings inline, one primary ----
    check(detail_live.index("Why it's here") < detail_live.index("Rifle spec"),
          "the rifle spec still comes before the reason for return")
    check("REQUIRED TO CLOSE" in detail_live, "the unset resolution is not flagged before close")
    check(detail_live.count('class="btn-next"') == 1,
          "the dark bar has more (or fewer) than one primary action")
    check("Work log" in detail_live and "$629.50" in detail_live,
          "the merged work log or its total is missing")
    check('class="btn-big danger"' not in detail_live,
          "a full-size red destructive button is still on the detail page")
    check("Clock out" in detail_live, "the station tech's running clock has no clock-out")

    # A shipped RTM must not offer a raw transition straight to 'closed'.
    shipped = station_pages["detail: shipped"]
    check('value="closed"' not in shipped,
          "detail offers a raw transition to closed, bypassing the findings gate")
    check("Review &amp; close" in shipped, "shipped RTM has no route to the close screen")

    # A closed RTM is finished: no next step, no clock, nothing to press.
    closed = station_pages["detail: closed"]
    check('class="next-step"' not in closed, "a closed RTM still shows a next-step bar")
    check("Clock in" not in closed, "a closed RTM still offers a clock-in")

    # ---- board: the day count leads ----
    check("board-days-label" in board, "board cards do not lead with the day count")
    check('class="board-card status-' in board, "board cards carry no status colour bar")
    check(">R<" not in board and "Repeat" in board, "board still uses the bare 'R' repeat marker")

    # ---- close: no red button, findings pointed at the rifle page ----
    close_ok = station_pages["close: ready to sign"]
    close_blocked = station_pages["close: blocked"]
    check('class="btn-big danger"' not in close_ok, "close still uses a red primary button")
    check("Close this RTM" in close_ok, "close screen has no close action")
    check("Not ready to close" in close_blocked, "blocked close screen does not say so")
    check("Resolution" in close_blocked, "blocked close screen does not name the missing field")

    # ---- reports: no opacity-based greys ----
    reports = station_pages["reports"]
    check("opacity: 0.7" not in reports and "opacity: 0.6" not in reports,
          "reports still fades text with opacity instead of using a colour")

    # ---- the actions the new buttons fire actually work ----
    # Free Trevor up: the verb should appear, and then actually start a clock.
    client.post(f"/rtm/{ids['live']}/clock-out")
    freed = body(client.get("/rtm/"))
    check("Clock in" in freed, "bench does not offer 'Clock in' once the tech is free")
    check('action="/rtm/%d/clock-in"' % ids["new"] in freed,
          "the 'Clock in' verb does not post to the clock-in route")

    resp = client.post(f"/rtm/{ids['new']}/clock-in")
    check(resp.status_code == 302, "bench clock-in did not redirect")
    rtm = rtm_db.get_rtm(ids["new"])
    check(any(s["open"] and s["tech_id"] == trevor for s in rtm["sessions"]),
          "bench clock-in did not open a session for the station tech")

    resp = client.post(f"/rtm/{ids['ready']}/status", data={"to_status": "shipped"})
    check(resp.status_code == 302, "bench 'Ship it' did not redirect")
    check(rtm_db.get_rtm(ids["ready"])["status"] == "shipped", "bench 'Ship it' did not ship")

    # Saving findings from the detail page must not blank the fields it hides.
    before = rtm_db.get_rtm(ids["live"])
    client.post(
        f"/rtm/{ids['live']}/findings",
        data={
            "root_cause_id": "4", "responsibility_id": "4", "resolution_id": "1",
            "findings": before["findings"],
            "inspection_notes": before["inspection_notes"],
            "round_count": str(before["round_count"]),
            "ammo_used": before["ammo_used"],
        },
    )
    after = rtm_db.get_rtm(ids["live"])
    check(after["inspection_notes"] == before["inspection_notes"],
          "inline findings save wiped the inspection notes")
    check(after["round_count"] == before["round_count"],
          "inline findings save wiped the round count")
    check(after["ammo_used"] == before["ammo_used"], "inline findings save wiped the ammo used")
    check(after["resolution_id"] == 1, "inline findings save did not set the resolution")

    # The station picker round-trips.
    resp = client.post("/rtm/station", data={"tech_id": str(trevor), "next": "/rtm/"})
    check(resp.status_code == 302, "station picker did not redirect")
    resp = client.post("/rtm/station", data={"tech_id": "9999", "next": "/rtm/"})
    check(resp.status_code == 302, "station picker rejected an unknown tech badly")
    with client.session_transaction() as sess:
        check("tech_id" not in sess, "an unknown tech id was accepted as the station identity")

    print(f"{CHECKS - len(FAILURES)}/{CHECKS} checks passed")
    for failure in FAILURES:
        print(f"  FAIL  {failure}")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
