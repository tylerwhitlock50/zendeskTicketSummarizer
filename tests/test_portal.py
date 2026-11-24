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

RECEIVED = {"arrival_damage": "no"}


def inspected():
    """Answers every inspection point OK and the complaint question, for stage posts."""
    points = repair_db.list_choices("inspection_point")
    return {**{f"point_{p['id']}": "ok" for p in points}, "complaint_match": "yes"}


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
        self.uploaded = {}
        self.comments = {}

    def get_ticket(self, ticket_id):
        ticket = {
            "id": ticket_id,
            "subject": "Rifle will not group",
            "status": self.status,
            "updated_at": "2026-11-20T10:00:00Z",
            "created_at": "2026-11-01T10:00:00Z",
            "custom_fields": [],
            "description": "My rifle won't group under 3 MOA since the last trip.",
        }
        requester = {"id": 1, "name": "Jane Hunter", "email": "jane@example.com"}
        return ticket, requester, None

    def upload_file(self, filename, data, content_type):
        token = f"tok{len(self.uploaded) + 1}"
        self.uploaded[token] = (filename, data, content_type)
        return token

    def get_comments(self, ticket_id):
        return list(self.comments.get(ticket_id, [])), {7: {"id": 7, "name": "Kyler"}}

    def download(self, content_url, max_bytes=None):
        for name, data, ctype in self.uploaded.values():
            if content_url.endswith("/" + name):
                return data, ctype
        raise portal_views.ZendeskError("not found", status=404)

    def post_update(self, ticket_id, body, public=True, tags=None, custom_fields=None, uploads=None):
        if self.fail:
            raise portal_views.ZendeskError("Zendesk API error 422: closed", status=422)
        if self.delay:
            time.sleep(self.delay)
        attachments = []
        for tok in uploads or []:
            name, data, ctype = self.uploaded[tok]
            attachments.append({"id": 9000 + len(self.uploaded) * 10 + len(attachments), "file_name": name,
                                "content_type": ctype, "size": len(data),
                                "content_url": f"https://example.zendesk.com/attachments/{name}", "thumbnails": []})
        self.comments.setdefault(ticket_id, []).append(
            {"author_id": 7, "public": public, "created_at": "2026-11-23T20:00:00Z", "attachments": attachments}
        )
        self.updates.append(
            {
                "uploads": list(uploads or []),
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
    resp = client.post("/t/500/stage/received", data=RECEIVED)
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
    client.post("/t/500/stage/received", data=RECEIVED)
    assert zd.updates == []


@needs_db
def test_received_posts_one_update_and_records(client, zd, tech):
    _add(client)
    client.post("/t/500/stage/received", data=RECEIVED)
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
    client.post("/t/500/stage/inspected", data={**inspected(), "problem_notes": "x"})
    assert zd.updates == []
    trigger = _choice("problem", "Trigger")
    client.post(
        "/t/500/stage/inspected",
        data={**inspected(), "problem_ids": [trigger["id"]], "problem_notes": "Sear worn, internal note"},
    )
    assert "Findings: Trigger." in zd.updates[0]["body"]
    assert "Sear worn" not in zd.updates[0]["body"]
    rma = repair_db.get_rma(500)
    assert rma["problem_summary"] == "Trigger" and rma["problem_notes"] == "Sear worn, internal note"


@needs_db
def test_repaired_uses_solution_when_work_done_blank(client, zd, tech):
    _add(client)
    sol = _choice("solution", "Adjusted trigger")
    client.post("/t/500/stage/repaired", data={"solution_ids": [sol["id"]], "work_done": ""})
    assert "Work performed: Adjusted trigger." in zd.updates[0]["body"]
    _add(client, 501)
    client.post("/t/501/stage/repaired", data={"solution_ids": [sol["id"]], "work_done": "Reset sear engagement"})
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
def test_repair_report_shows_forms_but_not_internal_notes(client, zd, tech):
    assert client.get("/t/500/report").status_code == 302  # not in the queue yet
    _add(client)
    client.post("/t/500/stage/received", data=RECEIVED)
    trigger = _choice("problem", "Trigger")
    client.post(
        "/t/500/stage/inspected",
        data={**inspected(), "problem_ids": [trigger["id"]], "problem_notes": "Sear worn, internal note"},
    )
    client.post("/t/500/stage/shipped", data={"tracking_number": "1Z999AA10123456784"})
    resp = client.get("/t/500/report")
    assert resp.status_code == 200
    page = resp.get_data(as_text=True)
    assert "Repair service report" in page and "Ticket" in page
    assert "Trigger" in page and "1Z999AA10123456784" in page
    assert "Sear worn" not in page
    assert "/t/500/report" in client.get("/t/500").get_data(as_text=True)


@needs_db
def test_double_tap_does_not_email_twice(client, zd, tech):
    _add(client)
    client.post("/t/500/stage/received", data=RECEIVED)
    resp = client.post("/t/500/stage/received", data=RECEIVED, follow_redirects=True)
    assert len(zd.updates) == 1
    assert b"was just posted" in resp.data


@needs_db
def test_zendesk_failure_saves_nothing(client, zd, tech):
    _add(client)
    zd.fail = True
    resp = client.post("/t/500/stage/received", data=RECEIVED, follow_redirects=True)
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
        client.post("/t/500/stage/received", data=RECEIVED)
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
    assert b"Problems found" in resp.data and b"Return tracking number" in resp.data
    assert b"Trigger" in resp.data  # problem checklist option


@needs_db
def test_queue_board_shows_cards_by_stage(client, zd, tech):
    _add(client, 500)
    _add(client, 501)
    client.post("/t/501/stage/received", data=RECEIVED)
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


@needs_db
def test_stage_preview_matches_the_post_and_sends_nothing(client, zd, tech):
    _add(client)
    resp = client.post("/t/500/stage/received/preview", data={**RECEIVED, "file_names": "arrived.jpg"})
    data = resp.get_json()
    assert data["ok"] is True and zd.updates == [] and repair_db.get_rma(500)["stage"] == "queued"
    post = data["posts"][0]
    assert post["public"] is True and post["files"] == ["arrived.jpg"]
    client.post("/t/500/stage/received", data=RECEIVED)
    assert zd.updates[0]["body"] == post["body"]


@needs_db
def test_stage_preview_reports_form_errors(client, zd, tech):
    _add(client)
    data = client.post("/t/500/stage/shipped/preview", data={"tracking_number": ""}).get_json()
    assert data["ok"] is False and "tracking" in data["error"]
    data = client.post("/t/500/stage/received/preview", data={**RECEIVED, "file_names": "x.exe"}).get_json()
    assert data["ok"] is False and "x.exe" in data["error"]
    assert zd.updates == []


@needs_db
def test_preview_needs_sign_in(clean_db, client, zd):
    resp = client.post("/t/500/action/preview", data={"note": "hi"})
    assert resp.status_code == 403 and resp.get_json()["ok"] is False


@needs_db
def test_action_preview_shows_button_and_note_as_separate_posts(client, zd, tech):
    _add(client)
    action = db.list_quick_actions()[0]
    data = client.post(
        "/t/500/action/preview",
        data={"action_id": action["id"], "note": "Barrel on order", "note_visibility": "internal"},
    ).get_json()
    assert [p["public"] for p in data["posts"]] == [bool(action["public"]), False]
    assert data["posts"][0]["body"] == action["comment_body"] and data["posts"][1]["body"] == "Barrel on order"
    data = client.post("/t/500/action/preview", data={"note": "Hi", "note_visibility": "public"}).get_json()
    assert data["posts"] == [{"public": True, "body": "Hi", "files": []}]
    assert zd.updates == []


# ---------------------------------------------------------------- report


@needs_db
def test_report_and_csv(client, zd, tech):
    _add(client)
    client.post("/t/500/stage/received", data=RECEIVED)
    trig = _choice("problem", "Trigger")
    client.post("/t/500/stage/inspected", data={**inspected(), "problem_ids": [trig["id"]]})
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
    client.post("/t/500/stage/received", data=RECEIVED)
    trig = _choice("problem", "Trigger")
    client.post("/t/500/stage/inspected", data={**inspected(), "problem_ids": [trig["id"]]})
    client.post("/t/500/stage/shipped", data={"tracking_number": "1Z999AA10123456784"})
    sent = len(zd.updates)

    resp = client.post("/t/500/stage/received", data=RECEIVED, follow_redirects=True)
    assert b"already past" in resp.data and len(zd.updates) == sent  # no email without the tick
    assert repair_db.get_rma(500)["stage"] == "shipped"

    client.post("/t/500/stage/received", data={**RECEIVED, "move_back": "1"})
    rma = repair_db.get_rma(500)
    assert len(zd.updates) == sent + 1
    assert rma["stage"] == "received"
    assert rma["inspected_at"] is None and rma["shipped_at"] is None and rma["tracking_number"] is None
    assert [r["ticket_id"] for r in repair_db.board()["received"]] == [500]
    assert repair_db.report(repair_db_today(), repair_db_today())["open_by_stage"]["received"] == 1


@needs_db
def test_resending_current_stage_keeps_first_time(client, zd, tech, monkeypatch):
    _add(client)
    client.post("/t/500/stage/received", data=RECEIVED)
    first = repair_db.get_rma(500)["received_at"]
    monkeypatch.setattr(portal_views, "DUPLICATE_WINDOW_SECONDS", 0)
    client.post("/t/500/stage/received", data=RECEIVED)
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
            c.post("/t/500/stage/received", data=RECEIVED)

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
    client.post("/t/500/stage/received", data=RECEIVED)
    assert repair_db.get_rma(500)["stage"] == "queued"
    zd.fail = False
    client.post("/t/500/stage/received", data={**RECEIVED, "record_only": "1"})
    rma = repair_db.get_rma(500)
    ev = repair_db.events_for(500)[0]
    assert rma["stage"] == "received" and ev["customer_message"] is None
    assert ev["detail"]["not_emailed"] is True
    assert zd.updates == []


@needs_db
def test_record_only_refused_for_open_ticket(client, zd, tech):
    _add(client)
    resp = client.post("/t/500/stage/received", data={**RECEIVED, "record_only": "1"}, follow_redirects=True)
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
    resp = client.post("/t/500/stage/received", data=RECEIVED)
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
    client.post("/t/500/stage/received", data=RECEIVED)
    with psycopg.connect(TEST_DSN, autocommit=True) as conn:
        conn.execute("UPDATE repair.rma SET received_at = now() - interval '20 days'")
    client.post("/t/500/action", data={"note": "still waiting", "note_visibility": "internal"})
    card = repair_db.board()["received"][0]
    assert (card["updated_at"] - card["stage_since"]).days >= 19


# ---------------------------------------------------------------- multi-select + Other


@needs_db
def test_inspection_takes_several_problems_and_other(client, zd, tech):
    _add(client)
    trig = _choice("problem", "Trigger")
    bolt = _choice("problem", "Bolt or action")
    client.post(
        "/t/500/stage/inspected",
        data={**inspected(), "problem_ids": [trig["id"], bolt["id"]], "problem_other": "  Cracked   scope base "},
    )
    assert "Findings: Bolt or action, Trigger, Cracked scope base." in zd.updates[0]["body"] or \
        "Findings: Trigger, Bolt or action, Cracked scope base." in zd.updates[0]["body"]
    rma = repair_db.get_rma(500)
    assert sorted(rma["problem_ids"]) == sorted([trig["id"], bolt["id"]])
    assert rma["problem_other"] == "Cracked scope base"
    assert rma["problem_summary"].endswith("Cracked scope base")
    # not added to the list unless ticked
    assert not any(c["label"] == "Cracked scope base" for c in repair_db.list_choices("problem"))


@needs_db
def test_other_alone_is_enough(client, zd, tech):
    _add(client)
    client.post("/t/500/stage/inspected", data={**inspected(), "problem_other": "Muzzle brake loose"})
    assert "Findings: Muzzle brake loose." in zd.updates[0]["body"]


@needs_db
def test_other_can_be_added_to_the_list(client, zd, tech):
    _add(client)
    client.post(
        "/t/500/stage/inspected",
        data={**inspected(), "problem_other": "Muzzle brake loose", "problem_add": "1"},
    )
    added = _choice("problem", "Muzzle brake loose")
    rma = repair_db.get_rma(500)
    assert rma["problem_ids"] == [added["id"]] and rma["problem_other"] is None
    # typing it again (any case) re-uses the same list item
    _add(client, 501)
    client.post("/t/501/stage/inspected", data={**inspected(), "problem_other": "muzzle BRAKE loose", "problem_add": "1"})
    assert [c["label"].lower() for c in repair_db.list_choices("problem")].count("muzzle brake loose") == 1
    assert repair_db.get_rma(501)["problem_ids"] == [added["id"]]


@needs_db
def test_repair_complete_is_multi_select_too(client, zd, tech):
    _add(client)
    rebed = _choice("solution", "Re-bedded action")
    trig = _choice("solution", "Adjusted trigger")
    client.post(
        "/t/500/stage/repaired",
        data={"solution_ids": [rebed["id"], trig["id"]], "solution_other": "Lapped lugs", "solution_add": "1"},
    )
    body = zd.updates[0]["body"]
    assert "Work performed: " in body and "Re-bedded action" in body and "Lapped lugs" in body
    rma = repair_db.get_rma(500)
    assert len(rma["solution_ids"]) == 3 and rma["solution_other"] is None
    _add(client, 501)
    resp = client.post("/t/501/stage/repaired", data={}, follow_redirects=True)
    assert b"Tick at least one repair done" in resp.data


@needs_db
def test_reposting_inspection_replaces_picks(client, zd, tech, monkeypatch):
    monkeypatch.setattr(portal_views, "DUPLICATE_WINDOW_SECONDS", 0)
    _add(client)
    trig = _choice("problem", "Trigger")
    bolt = _choice("problem", "Bolt or action")
    sol = _choice("solution", "Adjusted trigger")
    client.post("/t/500/stage/inspected", data={**inspected(), "problem_ids": [trig["id"], bolt["id"]]})
    client.post("/t/500/stage/repaired", data={"solution_ids": [sol["id"]]})
    client.post("/t/500/stage/inspected", data={**inspected(), "problem_ids": [bolt["id"]], "move_back": "1"})
    rma = repair_db.get_rma(500)
    assert rma["problem_ids"] == [bolt["id"]]
    assert rma["solution_ids"] == [sol["id"]]  # the other list is untouched


@needs_db
def test_report_counts_each_pick_and_other_text(client, zd, tech):
    trig = _choice("problem", "Trigger")
    bolt = _choice("problem", "Bolt or action")
    for tid, data in (
        (500, {"problem_ids": [trig["id"], bolt["id"]]}),
        (501, {"problem_ids": [trig["id"]], "problem_other": "Scope base"}),
        (502, {"problem_other": "scope base"}),
    ):
        _add(client, tid)
        client.post(f"/t/{tid}/stage/inspected", data={**inspected(), **data})
    today = repair_db_today()
    counts = {r["label"]: r["n"] for r in repair_db.report(today, today)["problems"]}
    assert counts["Trigger"] == 2 and counts["Bolt or action"] == 1
    assert counts.get("Other: Scope base", 0) + counts.get("Other: scope base", 0) == 2
    assert "(nothing recorded)" not in counts
    csv_text = client.get(f"/report/rmas.csv?start={today}&end={today}").data.decode("utf-8-sig")
    assert "problems" in csv_text.splitlines()[0] and "Scope base" in csv_text


@needs_db
def test_inactive_choice_cannot_be_ticked(client, zd, tech):
    _add(client)
    old = next(c for c in repair_db.list_choices("problem", include_inactive=True) if c["label"] == "Other")
    assert old["active"] is False  # the seeded "Other" item is replaced by the free-text box
    resp = client.post("/t/500/stage/inspected", data={**inspected(), "problem_ids": [old["id"]]}, follow_redirects=True)
    assert b"Tick at least one problem found" in resp.data
    assert zd.updates == []


# ---------------------------------------------------------------- stage forms v2


def _img(fmt="JPEG"):
    import io

    from PIL import Image

    if fmt == "HEIF":
        from pillow_heif import register_heif_opener

        register_heif_opener()
    buf = io.BytesIO()
    Image.new("RGB", (8, 8), (200, 30, 30)).save(buf, fmt)
    return buf.getvalue()


def _file(name, data):
    import io

    return (io.BytesIO(data), name)


@needs_db
def test_received_lists_accessories_condition_and_photos(client, zd, tech):
    _add(client)
    scope = _choice("accessory", "Scope")
    sling = _choice("accessory", "Sling")
    resp = client.post(
        "/t/500/stage/received",
        data={"accessory_ids": [scope["id"], sling["id"]], "arrival_damage": "yes", "arrival_notes": ""},
        follow_redirects=True,
    )
    assert b"Describe the damage" in resp.data and zd.updates == []
    client.post(
        "/t/500/stage/received",
        data={
            "accessory_ids": [scope["id"], sling["id"]],
            "accessory_other": "Spare bolt",
            "arrival_damage": "yes",
            "arrival_notes": "Case latch broken in transit",
            "files": [_file("rifle.jpg", _img()), _file("box.heic", _img("HEIF"))],
        },
        content_type="multipart/form-data",
    )
    u = zd.updates[0]
    assert "Items received with it: Scope, Sling, Spare bolt." in u["body"]
    assert "We noted the following on arrival: Case latch broken in transit." in u["body"]
    names = [zd.uploaded[t][0] for t in u["uploads"]]
    assert names == ["rifle.jpg", "box.jpg"]  # HEIC converted to JPEG
    assert zd.uploaded[u["uploads"][1]][2] == "image/jpeg"
    rma = repair_db.get_rma(500)
    assert rma["arrival_damage"] is True and rma["accessory_summary"] == "Scope, Sling, Spare bolt"
    assert repair_db.events_for(500)[0]["detail"]["files"] == ["rifle.jpg", "box.jpg"]


@needs_db
def test_received_without_damage_has_no_arrival_note(client, zd, tech):
    _add(client)
    client.post("/t/500/stage/received", data={"arrival_damage": "no"})
    assert "Items received with it: none." in zd.updates[0]["body"]
    assert "noted the following" not in zd.updates[0]["body"]


@needs_db
def test_unsupported_file_type_is_refused(client, zd, tech):
    _add(client)
    resp = client.post(
        "/t/500/stage/received",
        data={**RECEIVED, "files": [_file("run.exe", b"MZ")]},
        content_type="multipart/form-data",
        follow_redirects=True,
    )
    assert b"isn&#39;t a supported file type" in resp.data and zd.updates == []


@needs_db
def test_inspection_checklist_required_and_sent_to_customer(client, zd, tech):
    _add(client)
    trig = _choice("problem", "Trigger")
    points = repair_db.list_choices("inspection_point")
    assert len(points) == 11
    partial = {f"point_{p['id']}": "ok" for p in points[:-1]}
    resp = client.post(
        "/t/500/stage/inspected",
        data={**partial, "complaint_match": "yes", "problem_ids": [trig["id"]]},
        follow_redirects=True,
    )
    assert b"Mark every inspection point" in resp.data and zd.updates == []

    answers = {f"point_{p['id']}": "ok" for p in points}
    trigger_point = next(p for p in points if p["label"].startswith("Trigger"))
    answers[f"point_{trigger_point['id']}"] = "issue"
    answers[f"point_note_{trigger_point['id']}"] = "creep, 6.5 lb"
    answers[f"point_{points[-1]['id']}"] = "na"
    client.post(
        "/t/500/stage/inspected",
        data={**answers, "complaint_match": "partly", "problem_ids": [trig["id"]]},
    )
    body = zd.updates[0]["body"]
    assert "inspected your rifle (11-point inspection):" in body
    assert "- Trigger: pull weight and function: Issue found (creep, 6.5 lb)" in body
    assert "- Barrel: bore, crown and muzzle device: OK" in body
    assert "Not applicable" in body and "Findings: Trigger." in body
    rma = repair_db.get_rma(500)
    assert rma["complaint_match"] == "partly" and len(rma["inspection"]) == 11
    today = repair_db_today()
    issues = repair_db.report(today, today)["point_issues"]
    assert [(r["label"], r["n"]) for r in issues] == [(trigger_point["label"], 1)]


@needs_db
def test_inspection_needs_complaint_answer(client, zd, tech):
    _add(client)
    data = inspected()
    data.pop("complaint_match")
    resp = client.post("/t/500/stage/inspected", data={**data, "problem_other": "x"}, follow_redirects=True)
    assert b"match the customer" in resp.data and zd.updates == []


@needs_db
def test_accuracy_pass_emails_customer(client, zd, tech):
    _add(client)
    client.post(
        "/t/500/stage/tested",
        data={"test_required": "yes", "test_result": "pass", "group_size": "0.75", "group_unit": "moa",
              "test_distance": "100", "test_ammo": "Hornady 143gr ELD-X"},
    )
    u = zd.updates[0]
    assert u["public"] is True and "0.75 MOA group at 100 yards with Hornady 143gr ELD-X" in u["body"]
    assert u["tags"] == ["repair-portal", "rp-tested"]
    rma = repair_db.get_rma(500)
    assert rma["stage"] == "tested" and rma["test_result"] == "pass" and float(rma["group_size"]) == 0.75
    assert [r["ticket_id"] for r in repair_db.board()["tested"]] == [500]


@needs_db
def test_accuracy_fail_is_internal_and_flags(client, zd, tech):
    _add(client)
    client.post(
        "/t/500/stage/tested",
        data={"test_required": "yes", "test_result": "fail", "group_size": "2.5", "group_unit": "in",
              "test_distance": "100", "test_ammo": "Federal GMM"},
    )
    u = zd.updates[0]
    assert u["public"] is False and "FAILED" in u["body"] and '2.5" at 100 yards' in u["body"]
    rma = repair_db.get_rma(500)
    assert rma["flag"] == "Failed accuracy test"
    assert repair_db.events_for(500)[0]["customer_message"] is None


@needs_db
def test_accuracy_not_required_is_internal_and_clears_old_result(client, zd, tech, monkeypatch):
    monkeypatch.setattr(portal_views, "DUPLICATE_WINDOW_SECONDS", 0)
    _add(client)
    client.post(
        "/t/500/stage/tested",
        data={"test_required": "yes", "test_result": "pass", "group_size": "0.8", "group_unit": "moa",
              "test_distance": "100", "test_ammo": "Federal GMM"},
    )
    client.post("/t/500/stage/tested", data={"test_required": "no"})
    u = zd.updates[1]
    assert u["public"] is False and "not required" in u["body"]
    assert u["tags"] == ["repair-portal", "rp-tested"]  # Zendesk views/stage field still see the stage
    rma = repair_db.get_rma(500)
    assert rma["stage"] == "tested" and rma["test_required"] is False
    assert rma["group_size"] is None and rma["test_ammo"] is None and rma["test_distance"] is None


@needs_db
def test_accuracy_validates_numbers(client, zd, tech):
    _add(client)
    base = {"test_required": "yes", "test_result": "pass", "group_unit": "moa", "test_ammo": "x"}
    resp = client.post("/t/500/stage/tested", data={**base, "group_size": "abc"}, follow_redirects=True)
    assert b"Enter the group size" in resp.data
    resp = client.post("/t/500/stage/tested", data={**base, "group_size": "1", "test_ammo": ""}, follow_redirects=True)
    assert b"ammunition" in resp.data
    assert zd.updates == []


@needs_db
def test_other_updates_flag_and_clear(client, zd, tech, monkeypatch):
    monkeypatch.setattr(portal_views, "DUPLICATE_WINDOW_SECONDS", 0)
    _add(client)
    parts = next(a for a in db.list_quick_actions() if a["label"] == "Awaiting parts")
    client.post("/t/500/action", data={"action_id": parts["id"], "note": "Barrel on order, ETA Dec 5",
                                        "note_visibility": "public"})
    u = zd.updates[0]
    assert u["public"] is True and "waiting on parts" in u["body"] and "ETA Dec 5" not in u["body"]
    assert zd.updates[1]["public"] is True and zd.updates[1]["body"] == "Barrel on order, ETA Dec 5"
    assert repair_db.get_rma(500)["flag"] == "Awaiting parts"
    assert b"Awaiting parts" in client.get("/queue").data
    client.post("/t/500/clear-flag")
    assert repair_db.get_rma(500)["flag"] is None
    client.post("/t/500/action", data={"action_id": parts["id"]})
    client.post("/t/500/stage/received", data=RECEIVED)  # posting a stage clears the flag too
    assert repair_db.get_rma(500)["flag"] is None


@needs_db
def test_internal_update_button_and_file_only_note(client, zd, tech):
    _add(client)
    review = next(a for a in db.list_quick_actions() if a["label"] == "Under review")
    db.update_quick_action(review["id"], review["label"], review["comment_body"], review["tag"],
                           review["position"], True, public=False)
    client.post("/t/500/action", data={"action_id": review["id"]})
    assert zd.updates[0]["public"] is False
    client.post(
        "/t/500/action",
        data={"note_visibility": "internal", "files": [_file("parts.pdf", b"%PDF-1.4 test")]},
        content_type="multipart/form-data",
    )
    assert zd.updates[1]["uploads"] and zd.updates[1]["public"] is False


@needs_db
def test_video_link(client, zd, tech):
    _add(client)
    resp = client.post("/t/500/stage/received", data={**RECEIVED, "video_url": "not a url"}, follow_redirects=True)
    assert b"video link" in resp.data and zd.updates == []
    client.post("/t/500/stage/received", data={**RECEIVED, "video_url": "https://youtu.be/abc123"})
    assert zd.updates[0]["body"].endswith("Video: https://youtu.be/abc123")
    assert b"https://youtu.be/abc123" in client.get("/t/500").data


@needs_db
def test_gallery_and_file_proxy(client, zd, tech):
    _add(client)
    client.post(
        "/t/500/stage/received",
        data={**RECEIVED, "files": [_file("rifle.jpg", _img()), _file("report.xlsx", b"PK\x03\x04xx")]},
        content_type="multipart/form-data",
    )
    page = client.get("/t/500").data
    assert b"rifle.jpg" in page and b"report.xlsx" in page and b"Customer's complaint" in page
    att = zd.comments[500][0]["attachments"]
    img = client.get(f"/t/500/file/{att[0]['id']}")
    assert img.status_code == 200 and img.mimetype == "image/jpeg"
    assert img.headers["Content-Disposition"].startswith("inline")
    assert img.headers["Content-Security-Policy"] == "sandbox"
    assert img.headers["Cache-Control"] == "no-store"
    xls = client.get(f"/t/500/file/{att[1]['id']}")
    assert xls.headers["Content-Disposition"].startswith("attachment")
    assert xls.mimetype == "application/octet-stream"
    assert client.get("/t/500/file/123456").status_code == 404
    client.post("/tech/signout")
    assert client.get(f"/t/500/file/{att[0]['id']}").status_code == 403


def test_initials_filter():
    import app as app_module

    assert app_module.initials("Kyler Smith") == "KS"
    assert app_module.initials("Brooklyn") == "BR"
    assert app_module.initials("") == ""


@needs_db
def test_settings_lists_include_inspection_and_accessories(clean_db, client):
    page = client.get("/admin/").data
    assert b"Inspection checklist" in page and b"Items received with it" in page
    client.post("/admin/choice", data={"op": "create", "kind": "inspection_point", "label": "Headspace check"})
    assert any(c["label"] == "Headspace check" for c in repair_db.list_choices("inspection_point"))
    client.post("/admin/choice", data={"op": "create", "kind": "bogus", "label": "x"})
    assert not any(c["label"] == "x" for k in repair_db.CHOICE_KINDS for c in repair_db.list_choices(k))



# ---------------------------------------------------------------- review fixes (v2)


@needs_db
def test_internal_note_with_public_button_stays_internal(client, zd, tech):
    _add(client)
    hold = next(a for a in db.list_quick_actions() if a["label"] == "On hold")
    client.post("/t/500/action", data={"action_id": hold["id"], "note": "Customer owes $85, do not ship",
                                        "note_visibility": "internal"})
    assert zd.updates[0]["public"] is True and "do not ship" not in zd.updates[0]["body"]
    assert zd.updates[1]["public"] is False and "do not ship" in zd.updates[1]["body"]


@needs_db
def test_failed_test_blocks_shipping_without_override(client, zd, tech):
    _add(client)
    client.post(
        "/t/500/stage/tested",
        data={"test_required": "yes", "test_result": "fail", "group_size": "3", "group_unit": "moa",
              "test_distance": "100", "test_ammo": "Federal GMM"},
    )
    page = client.get("/t/500").data
    assert b"Ship anyway" in page
    resp = client.post("/t/500/stage/shipped", data={"tracking_number": "1Z999AA10123456784"},
                       follow_redirects=True)
    assert b"failed its accuracy test" in resp.data and len(zd.updates) == 1
    client.post("/t/500/stage/shipped", data={"tracking_number": "1Z999AA10123456784", "ship_failed": "1"})
    assert repair_db.get_rma(500)["stage"] == "shipped" and len(zd.updates) == 2


@needs_db
def test_add_to_list_rolls_back_when_zendesk_refuses(client, zd, tech):
    _add(client)
    zd.fail = True
    client.post("/t/500/stage/inspected",
                data={**inspected(), "problem_other": "Cracked recoil lug", "problem_add": "1"})
    assert not any(c["label"] == "Cracked recoil lug" for c in repair_db.list_choices("problem"))
    zd.fail = False
    client.post("/t/500/stage/inspected",
                data={**inspected(), "problem_other": "Cracked recoil lug", "problem_add": "1"})
    added = _choice("problem", "Cracked recoil lug")
    assert repair_db.get_rma(500)["problem_ids"] == [added["id"]]
    assert "Findings: Cracked recoil lug." in zd.updates[0]["body"]


@needs_db
def test_empty_inspection_checklist_is_refused(client, zd, tech):
    _add(client)
    points = repair_db.list_choices("inspection_point")
    try:
        for p in points:
            repair_db.update_choice(p["id"], p["label"], p["position"], False)
        resp = client.post("/t/500/stage/inspected", data={"complaint_match": "yes", "problem_other": "x"},
                           follow_redirects=True)
        assert b"inspection checklist is empty" in resp.data and zd.updates == []
    finally:
        for p in points:
            repair_db.update_choice(p["id"], p["label"], p["position"], True)


@needs_db
def test_file_proxy_reuses_the_page_lookup(client, zd, tech, monkeypatch):
    _add(client)
    client.post(
        "/t/500/stage/received",
        data={**RECEIVED, "files": [_file("a.jpg", _img()), _file("b.jpg", _img())]},
        content_type="multipart/form-data",
    )
    calls = []
    real = zd.get_comments
    monkeypatch.setattr(zd, "get_comments", lambda tid: calls.append(tid) or real(tid))
    client.get("/t/500")
    for a in zd.comments[500][0]["attachments"]:
        assert client.get(f"/t/500/file/{a['id']}").status_code == 200
    assert calls == [500]  # one listing for the page; thumbnails come from the cache
