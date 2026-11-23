"""Portal tests: a faked Zendesk client and a real Postgres test database.

Set TEST_DATABASE_URL to an EMPTY, throwaway database to run the database
tests; it is wiped (schemas portal, repair, rtm) and migrated at session start.
Without it, only the database-free tests run.

    $env:TEST_DATABASE_URL = "postgresql://postgres:portal_dev@localhost:5434/portal_test"
    venv\\Scripts\\python -m pytest tests
"""

import os
import sys
import threading
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TEST_DSN = os.environ.get("TEST_DATABASE_URL", "")
# Must be set before app import: app opens the pool from DATABASE_URL, and an
# empty value here keeps a developer .env from pointing tests at a real database.
os.environ["DATABASE_URL"] = TEST_DSN
os.environ["RTM_DATABASE_URL"] = ""
os.environ["ADMIN_PASSWORD"] = ""
os.environ.setdefault("ZENDESK_SUBDOMAIN", "example")
os.environ.setdefault("ZENDESK_EMAIL", "test@example.com")
os.environ.setdefault("ZENDESK_API_TOKEN", "token")
os.environ.setdefault("OPENAI_API_KEY", "test")

import psycopg  # noqa: E402

from app import app as flask_app  # noqa: E402
import db  # noqa: E402
import portal_views  # noqa: E402
import repair_db  # noqa: E402

needs_db = pytest.mark.skipif(not TEST_DSN, reason="TEST_DATABASE_URL not set")


# ---------------------------------------------------------------- fakes


class FakeZendesk:
    def __init__(self):
        self.updates = []
        self.fail = False
        self.status = "open"
        self.search_results = []
        self.queries = []
        self.tag_calls = []
        self.delay = 0.0

    def get_ticket(self, ticket_id):
        ticket = {
            "id": ticket_id,
            "subject": "Rifle will not group",
            "status": self.status,
            "updated_at": "2026-11-20T10:00:00Z",
            "created_at": "2026-11-01T10:00:00Z",
            "custom_fields": [],
        }
        requester = {"id": 1, "name": "Jane Hunter", "email": "jane@example.com"}
        return ticket, requester, None

    def post_update(self, ticket_id, body, public=True, tags=None, custom_fields=None):
        if self.fail:
            raise portal_views.ZendeskError("Zendesk API error 422: closed", status=422)
        if self.delay:
            time.sleep(self.delay)
        self.updates.append(
            {
                "ticket_id": ticket_id,
                "body": body,
                "public": public,
                "tags": list(tags or []),
                "custom_fields": dict(custom_fields or {}),
            }
        )

    def add_tags(self, ticket_id, tags):
        self.tag_calls.append((ticket_id, list(tags)))

    def search_export(self, query, max_pages=20):
        self.queries.append(query)
        yield from self.search_results


# ---------------------------------------------------------------- fixtures


@pytest.fixture(scope="session")
def migrated():
    if not TEST_DSN:
        pytest.skip("TEST_DATABASE_URL not set")
    with psycopg.connect(TEST_DSN, autocommit=True) as conn:
        for schema in ("repair", "portal", "rtm"):
            conn.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
    from migrations import migrate

    assert migrate.main() == 0
    db.close_pool()
    db.init_pool(TEST_DSN)
    yield
    db.close_pool()


@pytest.fixture
def clean_db(migrated):
    with psycopg.connect(TEST_DSN, autocommit=True) as conn:
        conn.execute(
            "TRUNCATE repair.event, repair.rma, repair.tech, portal.action_log RESTART IDENTITY CASCADE"
        )
    yield


@pytest.fixture
def zd(monkeypatch):
    fake = FakeZendesk()
    monkeypatch.setattr(portal_views, "_get_zendesk", lambda: fake)
    return fake


@pytest.fixture
def client():
    flask_app.config["TESTING"] = True
    flask_app.config["CSRF_CHECK"] = False
    with flask_app.test_client() as c:
        yield c
    flask_app.config["CSRF_CHECK"] = True


@pytest.fixture
def tech(clean_db, client):
    """Create a tech and sign them in on the test client."""
    repair_db.create_tech("Kyler", "1234")
    tid = repair_db.list_techs()[0]["id"]
    resp = client.post("/tech", data={"tech_id": tid, "pin": "1234"})
    assert resp.status_code == 302
    return {"id": tid, "name": "Kyler"}


def _choice(kind, label):
    return next(c for c in repair_db.list_choices(kind) if c["label"] == label)


def _add(client, ticket_id=500):
    resp = client.post(f"/t/{ticket_id}/add")
    assert resp.status_code == 302
    return resp


# ---------------------------------------------------------------- no database


def test_scan_go_parses_id_and_urls(client):
    resp = client.get("/scan/go?ticket_id=1234")
    assert resp.status_code == 302 and resp.headers["Location"].endswith("/t/1234")
    resp = client.get("/scan/go?ticket_id=https://example.zendesk.com/agent/tickets/987")
    assert resp.headers["Location"].endswith("/t/987")
    resp = client.get("/scan/go?ticket_id=not-a-ticket")
    assert "/scan" in resp.headers["Location"]


def test_render_message_fills_known_placeholders_only():
    out = portal_views.render_message("Found {problem}; ship {tracking}; {other}", {"problem": "Trigger", "tracking": "1Z"})
    assert out == "Found Trigger; ship 1Z; {other}"


def test_csrf_blocks_post_without_token():
    flask_app.config["TESTING"] = True
    flask_app.config["CSRF_CHECK"] = True
    with flask_app.test_client() as c:
        assert c.post("/tech/signout").status_code == 400


def test_ticket_sheet_includes_barcodes(client, monkeypatch):
    import app as app_module

    class SheetZd(FakeZendesk):
        def get_comments(self, ticket_id):
            return [], {}

        def get_ticket_fields(self):
            return {}

        def get_ticket_form(self, form_id):
            return None

    monkeypatch.setattr(app_module, "get_client", lambda: SheetZd())
    monkeypatch.setattr(
        app_module, "summarize_ticket", lambda *a, **k: (_ for _ in ()).throw(Exception("off"))
    )
    resp = client.get("/ticket/1234")
    assert resp.status_code == 200
    assert resp.data.count(b"<svg") >= 2  # Code 128 + QR


# ---------------------------------------------------------------- tech sign-in


@needs_db
def test_wrong_pin_is_rejected(clean_db, client):
    repair_db.create_tech("Nash", "4321")
    tid = repair_db.list_techs()[0]["id"]
    resp = client.post("/tech", data={"tech_id": tid, "pin": "0000"})
    assert resp.status_code == 200 and b"don&#39;t match" in resp.data
    with client.session_transaction() as s:
        assert "tech" not in s


@needs_db
def test_inactive_tech_cannot_sign_in(clean_db, client):
    repair_db.create_tech("Derek", "1111")
    tid = repair_db.list_techs()[0]["id"]
    repair_db.update_tech(tid, "Derek", active=False)
    client.post("/tech", data={"tech_id": tid, "pin": "1111"})
    with client.session_transaction() as s:
        assert "tech" not in s


@needs_db
def test_posting_requires_sign_in(clean_db, client, zd):
    resp = client.post("/t/500/stage/received")
    assert resp.status_code == 302 and "/tech" in resp.headers["Location"]
    assert zd.updates == []


# ---------------------------------------------------------------- incoming + add


@needs_db
def test_incoming_lists_tagged_tickets_not_yet_added(client, zd, tech):
    zd.search_results = [
        {"id": 500, "subject": "Already added", "status": "open", "created_at": "2026-11-01T00:00:00Z"},
        {"id": 501, "subject": "New RMA", "status": "new", "created_at": "2026-11-02T00:00:00Z"},
    ]
    _add(client, 500)
    resp = client.get("/incoming")
    assert resp.status_code == 200
    assert b"New RMA" in resp.data and b"Already added" not in resp.data
    assert zd.queries == ["tags:repair_process -tags:rp-queued status<closed"]


@needs_db
def test_add_to_queue_records_without_emailing(client, zd, tech):
    _add(client, 500)
    rma = repair_db.get_rma(500)
    assert rma["stage"] == "queued" and rma["requester_name"] == "Jane Hunter"
    assert rma["added_by"] == "Kyler"
    assert [e["kind"] for e in repair_db.events_for(500)] == ["added"]
    assert zd.updates == []  # nothing goes to the customer on add
    assert zd.tag_calls == [(500, ["repair-portal", "rp-queued"])]  # tags only, no comment
    resp = client.post("/t/500/add", follow_redirects=True)
    assert b"already in the repair queue" in resp.data


@needs_db
def test_closed_ticket_cannot_be_added(client, zd, tech):
    zd.status = "closed"
    client.post("/t/500/add")
    assert repair_db.get_rma(500) is None


# ---------------------------------------------------------------- stages


@needs_db
def test_stage_requires_ticket_in_queue(client, zd, tech):
    client.post("/t/500/stage/received")
    assert zd.updates == []


@needs_db
def test_received_posts_one_update_and_records(client, zd, tech):
    _add(client)
    client.post("/t/500/stage/received")
    assert len(zd.updates) == 1
    u = zd.updates[0]
    assert u["public"] is True and "arrived" in u["body"]
    assert u["tags"] == ["repair-portal", "rp-received"]
    assert u["custom_fields"] == {}
    rma = repair_db.get_rma(500)
    assert rma["stage"] == "received" and rma["received_at"] is not None
    ev = repair_db.events_for(500)[0]
    assert ev["stage"] == "received" and ev["tech_name"] == "Kyler"
    assert ev["customer_message"] == u["body"]


@needs_db
def test_inspected_needs_problem_and_keeps_notes_internal(client, zd, tech):
    _add(client)
    client.post("/t/500/stage/inspected", data={"problem_notes": "x"})
    assert zd.updates == []
    trigger = _choice("problem", "Trigger")
    client.post(
        "/t/500/stage/inspected",
        data={"problem_id": trigger["id"], "problem_notes": "Sear worn, internal note"},
    )
    assert "found the following: Trigger." in zd.updates[0]["body"]
    assert "Sear worn" not in zd.updates[0]["body"]
    rma = repair_db.get_rma(500)
    assert rma["problem_label"] == "Trigger" and rma["problem_notes"] == "Sear worn, internal note"


@needs_db
def test_repaired_uses_solution_when_work_done_blank(client, zd, tech):
    _add(client)
    sol = _choice("solution", "Adjusted trigger")
    client.post("/t/500/stage/repaired", data={"solution_id": sol["id"], "work_done": ""})
    assert "Work performed: Adjusted trigger." in zd.updates[0]["body"]
    _add(client, 501)
    client.post("/t/501/stage/repaired", data={"solution_id": sol["id"], "work_done": "Reset sear engagement"})
    assert "Work performed: Reset sear engagement." in zd.updates[1]["body"]


@needs_db
def test_shipped_requires_tracking(client, zd, tech):
    _add(client)
    client.post("/t/500/stage/shipped", data={"tracking_number": ""})
    client.post("/t/500/stage/shipped", data={"tracking_number": "<script>"})
    assert zd.updates == []
    client.post("/t/500/stage/shipped", data={"tracking_number": "1Z999AA10123456784"})
    assert "Tracking number: 1Z999AA10123456784." in zd.updates[0]["body"]
    rma = repair_db.get_rma(500)
    assert rma["stage"] == "shipped" and rma["tracking_number"] == "1Z999AA10123456784"


@needs_db
def test_double_tap_does_not_email_twice(client, zd, tech):
    _add(client)
    client.post("/t/500/stage/received")
    resp = client.post("/t/500/stage/received", follow_redirects=True)
    assert len(zd.updates) == 1
    assert b"was just posted" in resp.data


@needs_db
def test_zendesk_failure_saves_nothing(client, zd, tech):
    _add(client)
    zd.fail = True
    resp = client.post("/t/500/stage/received", follow_redirects=True)
    assert b"nothing was sent or saved" in resp.data
    assert repair_db.get_rma(500)["stage"] == "queued"
    assert [e["kind"] for e in repair_db.events_for(500)] == ["added"]


@needs_db
def test_stage_sets_zendesk_field_when_configured(client, zd, tech, monkeypatch):
    monkeypatch.setenv("ZENDESK_STAGE_FIELD_ID", "36000123")
    stage = repair_db.get_stage("received")
    repair_db.update_stage("received", stage["label"], stage["comment_body"], "rma_received")
    try:
        _add(client)
        client.post("/t/500/stage/received")
        assert zd.updates[0]["custom_fields"] == {36000123: "rma_received"}
    finally:
        repair_db.update_stage("received", stage["label"], stage["comment_body"], None)


# ---------------------------------------------------------------- pages


@needs_db
def test_ticket_page_offers_add_then_stages(client, zd, tech):
    resp = client.get("/t/500")
    assert resp.status_code == 200 and b"Add to repair queue" in resp.data
    _add(client)
    resp = client.get("/t/500")
    assert b"Problem found" in resp.data and b"Return tracking number" in resp.data
    assert b"Trigger" in resp.data  # problem drop-down option


@needs_db
def test_queue_board_shows_cards_by_stage(client, zd, tech):
    _add(client, 500)
    _add(client, 501)
    client.post("/t/501/stage/received")
    resp = client.get("/queue")
    assert resp.status_code == 200
    cols = repair_db.board()
    assert [r["ticket_id"] for r in cols["queued"]] == [500]
    assert [r["ticket_id"] for r in cols["received"]] == [501]
    assert b"#500" in resp.data and b"#501" in resp.data


@needs_db
def test_quick_action_and_note_post_and_log(client, zd, tech):
    _add(client)
    action = db.list_quick_actions()[0]
    client.post("/t/500/action", data={"action_id": action["id"]})
    client.post("/t/500/action", data={"note": "Waiting on barrel", "note_visibility": "internal"})
    assert zd.updates[0]["public"] is True and action["tag"] in zd.updates[0]["tags"]
    assert zd.updates[1]["public"] is False and zd.updates[1]["tags"] == ["repair-portal"]
    kinds = [e["kind"] for e in repair_db.events_for(500)]
    assert kinds[:2] == ["note", "action"]
    # a ticket not in the queue still logs to the legacy action log
    client.post("/t/777/action", data={"note": "hi", "note_visibility": "internal"})
    assert db.recent_actions(777)[0]["tech_initials"] == "Kyler"


# ---------------------------------------------------------------- report


@needs_db
def test_report_and_csv(client, zd, tech):
    _add(client)
    client.post("/t/500/stage/received")
    trig = _choice("problem", "Trigger")
    client.post("/t/500/stage/inspected", data={"problem_id": trig["id"]})
    client.post("/t/500/stage/shipped", data={"tracking_number": "1Z999AA10123456784"})
    today = repair_db_today()
    data = repair_db.report(today, today)
    assert data["stage_counts"]["received"] == 1 and data["stage_counts"]["added"] == 1
    assert data["turnaround"]["shipped"] == 1
    assert [(r["label"], r["n"]) for r in data["problems"]] == [("Trigger", 1)]
    assert data["techs"]["Kyler"]["total"] == 4

    resp = client.get("/report/")
    assert resp.status_code == 200 and b"Trigger" in resp.data

    resp = client.get(f"/report/rmas.csv?start={today}&end={today}")
    text = resp.data.decode("utf-8-sig")
    assert text.splitlines()[0].startswith("ticket_id,subject")
    assert "1Z999AA10123456784" in text

    empty = client.get("/report/events.csv?start=2001-01-01&end=2001-01-02").data.decode("utf-8-sig")
    assert empty.strip().startswith("created_at,ticket_id")  # header even with no rows


def repair_db_today():
    import report_views

    return report_views._today()


def test_csv_cell_neutralizes_formulas():
    import report_views

    assert report_views._cell("=HYPERLINK(1)") == "'=HYPERLINK(1)"
    assert report_views._cell("Trigger") == "Trigger"


# ---------------------------------------------------------------- settings


@needs_db
def test_settings_password(clean_db, client, monkeypatch):
    monkeypatch.setenv("ADMIN_PASSWORD", "s3cret")
    resp = client.get("/admin/")
    assert resp.status_code == 302 and "/admin/login" in resp.headers["Location"]
    client.post("/admin/login", data={"password": "nope"})
    assert client.get("/admin/").status_code == 302
    client.post("/admin/login", data={"password": "s3cret"})
    assert client.get("/admin/").status_code == 200


@needs_db
def test_settings_manage_people_and_lists(clean_db, client):
    client.post("/admin/tech", data={"op": "create", "name": "Brooklyn", "pin": "12"})
    assert repair_db.list_techs() == []  # PIN too short
    client.post("/admin/tech", data={"op": "create", "name": "Brooklyn", "pin": "2468"})
    resp = client.post("/admin/tech", data={"op": "create", "name": "brooklyn", "pin": "1357"}, follow_redirects=True)
    assert b"already someone named" in resp.data
    assert [t["name"] for t in repair_db.list_techs()] == ["Brooklyn"]

    client.post("/admin/choice", data={"op": "create", "kind": "problem", "label": "Scope mount"})
    assert any(c["label"] == "Scope mount" for c in repair_db.list_choices("problem"))
    resp = client.post("/admin/choice", data={"op": "create", "kind": "problem", "label": "scope mount"}, follow_redirects=True)
    assert b"already on the list" in resp.data

    resp = client.get("/admin/")
    assert resp.status_code == 200 and b"Brooklyn" in resp.data and b"Scope mount" in resp.data


# ---------------------------------------------------------------- review fixes


@needs_db
def test_earlier_stage_needs_move_back_and_resets_later_times(client, zd, tech, monkeypatch):
    monkeypatch.setattr(portal_views, "DUPLICATE_WINDOW_SECONDS", 0)  # posts here are seconds apart
    _add(client)
    client.post("/t/500/stage/received")
    trig = _choice("problem", "Trigger")
    client.post("/t/500/stage/inspected", data={"problem_id": trig["id"]})
    client.post("/t/500/stage/shipped", data={"tracking_number": "1Z999AA10123456784"})
    sent = len(zd.updates)

    resp = client.post("/t/500/stage/received", follow_redirects=True)
    assert b"already past" in resp.data and len(zd.updates) == sent  # no email without the tick
    assert repair_db.get_rma(500)["stage"] == "shipped"

    client.post("/t/500/stage/received", data={"move_back": "1"})
    rma = repair_db.get_rma(500)
    assert len(zd.updates) == sent + 1
    assert rma["stage"] == "received"
    assert rma["inspected_at"] is None and rma["shipped_at"] is None and rma["tracking_number"] is None
    assert [r["ticket_id"] for r in repair_db.board()["received"]] == [500]
    assert repair_db.report(repair_db_today(), repair_db_today())["open_by_stage"]["received"] == 1


@needs_db
def test_resending_current_stage_keeps_first_time(client, zd, tech, monkeypatch):
    _add(client)
    client.post("/t/500/stage/received")
    first = repair_db.get_rma(500)["received_at"]
    monkeypatch.setattr(portal_views, "DUPLICATE_WINDOW_SECONDS", 0)
    client.post("/t/500/stage/received")
    assert len(zd.updates) == 2
    assert repair_db.get_rma(500)["received_at"] == first


@needs_db
def test_simultaneous_double_tap_emails_once(client, zd, tech):
    _add(client)
    with client.session_transaction() as sess:
        saved = dict(sess)
    zd.delay = 0.4  # the Zendesk call is slow, as in real life

    def tap():
        with flask_app.test_client() as c:
            with c.session_transaction() as sess:
                sess.update(saved)
            c.post("/t/500/stage/received")

    threads = [threading.Thread(target=tap) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(zd.updates) == 1
    assert [e["stage"] for e in repair_db.events_for(500) if e["kind"] == "stage"] == ["received"]


@needs_db
def test_closed_ticket_stage_saved_without_email(client, zd, tech):
    _add(client)
    zd.status = "closed"
    resp = client.get("/t/500")
    assert b"without emailing" in resp.data
    zd.fail = True  # Zendesk refuses comments on closed tickets
    client.post("/t/500/stage/received")
    assert repair_db.get_rma(500)["stage"] == "queued"
    zd.fail = False
    client.post("/t/500/stage/received", data={"record_only": "1"})
    rma = repair_db.get_rma(500)
    ev = repair_db.events_for(500)[0]
    assert rma["stage"] == "received" and ev["customer_message"] is None
    assert ev["detail"]["not_emailed"] is True
    assert zd.updates == []


@needs_db
def test_record_only_refused_for_open_ticket(client, zd, tech):
    _add(client)
    resp = client.post("/t/500/stage/received", data={"record_only": "1"}, follow_redirects=True)
    assert b"post the stage normally" in resp.data
    assert repair_db.get_rma(500)["stage"] == "queued"


@needs_db
def test_pin_lockout_after_repeated_misses(clean_db, client):
    repair_db.create_tech("Harrison", "2468")
    tid = repair_db.list_techs()[0]["id"]
    for _ in range(repair_db.PIN_MAX_FAILS):
        client.post("/tech", data={"tech_id": tid, "pin": "0000"})
    resp = client.post("/tech", data={"tech_id": tid, "pin": "2468"}, follow_redirects=True)
    assert b"paused" in resp.data
    with client.session_transaction() as s:
        assert "tech" not in s
    with psycopg.connect(TEST_DSN, autocommit=True) as conn:  # the pause runs out
        conn.execute("UPDATE repair.tech SET locked_until = now() - interval '1 second'")
    client.post("/tech", data={"tech_id": tid, "pin": "2468"})
    with client.session_transaction() as s:
        assert s["tech"]["name"] == "Harrison"


@needs_db
def test_deactivating_or_new_pin_signs_tech_out(client, zd, tech):
    repair_db.update_tech(tech["id"], "Kyler", active=True, new_pin="9999")
    resp = client.post("/t/500/stage/received")
    assert "/tech" in resp.headers["Location"]  # old sign-in no longer valid
    client.post("/tech", data={"tech_id": tech["id"], "pin": "9999"})
    _add(client)  # signed in again with the new PIN
    repair_db.update_tech(tech["id"], "Kyler", active=False)
    resp = client.post("/t/501/add")
    assert "/tech" in resp.headers["Location"]


@needs_db
def test_admin_session_ends_when_password_changes(clean_db, client, monkeypatch):
    monkeypatch.setenv("ADMIN_PASSWORD", "first")
    client.post("/admin/login", data={"password": "first"})
    assert client.get("/admin/").status_code == 200
    monkeypatch.setenv("ADMIN_PASSWORD", "second")
    assert client.get("/admin/").status_code == 302


def test_admin_login_throttled(client, monkeypatch):
    import auth

    monkeypatch.setenv("ADMIN_PASSWORD", "right")
    monkeypatch.setattr(auth, "_admin_fails", {})
    for _ in range(auth.ADMIN_MAX_FAILS):
        client.post("/admin/login", data={"password": "wrong"})
    resp = client.post("/admin/login", data={"password": "right"}, follow_redirects=True)
    assert b"Too many wrong passwords" in resp.data


@needs_db
def test_shared_secret_is_stable(migrated):
    first = db.shared_secret()
    assert first and len(first) > 40 and db.shared_secret() == first


@needs_db
def test_queue_card_date_ignores_notes(client, zd, tech):
    _add(client)
    client.post("/t/500/stage/received")
    with psycopg.connect(TEST_DSN, autocommit=True) as conn:
        conn.execute("UPDATE repair.rma SET received_at = now() - interval '20 days'")
    client.post("/t/500/action", data={"note": "still waiting", "note_visibility": "internal"})
    card = repair_db.board()["received"][0]
    assert (card["updated_at"] - card["stage_since"]).days >= 19
