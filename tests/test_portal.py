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

RECEIVED = {"arrival_damage": "no", "arrival_clear": "clear", "serial_number": "CA12345",
            "model": "Ridgeline", "caliber": "6.5 Creedmoor"}
SHIP_OK = {"ship_serial": "CA12345", "ship_unloaded": "1"}
SHIPPED = {**SHIP_OK, "ship_failed": "1"}  # these tests ship without a final test (manager OK)
FINAL = {"function_check": "pass", "test_required": "no"}


def inspected():
    """Answers every inspection point OK and the complaint question, for stage posts."""
    points = repair_db.list_choices("inspection_point")
    return {**{f"point_{p['id']}": "ok" for p in points}, "complaint_match": "yes", "headspace": "pass"}


def repaired():
    """The root cause and coverage every Repair complete post needs."""
    cause = repair_db.list_choices("root_cause")[0]
    return {"root_cause_id": cause["id"], "coverage": "warranty"}


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
        self.custom_fields = []  # [{"id", "value"}] on every ticket
        self.tags = []
        self.created_at = "2026-11-01T10:00:00Z"
        self.fail_status = 422    # status of the error raised while self.fail is set
        self.fail_fields = False  # reject (422) any update that sets custom fields
        self.fields = {
            301: {"id": 301, "title": "Serial number", "active": True},
            302: {"id": 302, "title": "Model", "active": True,
                  "custom_field_options": [{"name": "Ridgeline FFT", "value": "ridgeline_fft"}]},
            303: {"id": 303, "title": "Caliber", "active": True},
            304: {"id": 304, "title": "Inbound tracking number", "active": True},
        }

    def get_ticket(self, ticket_id):
        ticket = {
            "id": ticket_id,
            "subject": "Rifle will not group",
            "status": self.status,
            "updated_at": "2026-11-20T10:00:00Z",
            "created_at": self.created_at,
            "custom_fields": list(self.custom_fields),
            "tags": list(self.tags),
            "requester_id": 1,
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

    def post_update(self, ticket_id, body, public=True, tags=None, custom_fields=None, uploads=None,
                    html_body=None, author_id=None):
        if self.fail:
            raise portal_views.ZendeskError(f"Zendesk API error {self.fail_status}: closed", status=self.fail_status)
        if self.fail_fields and custom_fields:
            raise portal_views.ZendeskError("Zendesk API error 422: bad field value", status=422)
        if self.delay:
            time.sleep(self.delay)
        attachments = []
        for tok in uploads or []:
            name, data, ctype = self.uploaded[tok]
            attachments.append({"id": 9000 + len(self.uploaded) * 10 + len(attachments), "file_name": name,
                                "content_type": ctype, "size": len(data),
                                "content_url": f"https://example.zendesk.com/attachments/{name}", "thumbnails": []})
        self.comments.setdefault(ticket_id, []).append(
            {"author_id": author_id or 7, "public": public, "created_at": "2026-11-23T20:00:00Z",
             "body": body, "html_body": html_body, "attachments": attachments}
        )
        self.tags.extend(t for t in tags or [] if t not in self.tags)
        self.updates.append(
            {
                "uploads": list(uploads or []),
                "ticket_id": ticket_id,
                "body": body,
                "html_body": html_body,
                "author_id": author_id,
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

    def get_ticket_fields(self):
        return self.fields


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
            "TRUNCATE repair.event, repair.intake, repair.rma, repair.tech, repair.setting, portal.action_log "
            "RESTART IDENTITY CASCADE"
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


@needs_db
def test_scan_go_parses_id_and_urls(clean_db, client, zd):
    resp = client.get("/scan/go?ticket_id=1234")
    assert resp.status_code == 302 and resp.headers["Location"].endswith("/t/1234")
    resp = client.get("/scan/go?ticket_id=%231234")
    assert resp.headers["Location"].endswith("/t/1234")
    resp = client.get("/scan/go?ticket_id=https://example.zendesk.com/agent/tickets/987")
    assert resp.headers["Location"].endswith("/t/987")
    resp = client.get("/scan/go?ticket_id=not-a-ticket")
    assert "/scan" in resp.headers["Location"] and "Nothing+matches" in resp.headers["Location"]
    assert zd.queries == ['"not-a-ticket"']
    # A long all-digit scan is a tracking number, never read as a ticket ID.
    resp = client.get("/scan/go?ticket_id=776543219876")
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
    client.post("/t/500/stage/repaired", data={**repaired(), "solution_ids": [sol["id"]], "work_done": ""})
    assert "Work performed: Adjusted trigger." in zd.updates[0]["body"]
    _add(client, 501)
    client.post("/t/501/stage/repaired", data={**repaired(), "solution_ids": [sol["id"]], "work_done": "Reset sear engagement"})
    assert "Work performed: Reset sear engagement." in zd.updates[1]["body"]


@needs_db
def test_shipped_requires_tracking(client, zd, tech):
    _add(client)
    client.post("/t/500/stage/shipped", data={**SHIPPED, "tracking_number": ""})
    client.post("/t/500/stage/shipped", data={**SHIPPED, "tracking_number": "<script>"})
    assert zd.updates == []
    client.post("/t/500/stage/shipped", data={**SHIPPED, "tracking_number": "1Z999AA10123456784"})
    body = zd.updates[0]["body"]
    assert "via UPS, tracking number 1Z999AA10123456784.\nTrack it here: https://www.ups.com/track?tracknum=" in body
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
    client.post("/t/500/stage/shipped", data={**SHIPPED, "tracking_number": "1Z999AA10123456784"})
    resp = client.get("/t/500/report")
    assert resp.status_code == 200
    page = resp.get_data(as_text=True)
    assert "Repair service report" in page and "Ticket" in page
    assert "Trigger" in page and "1Z999AA10123456784" in page
    assert "CA12345" in page  # bench-verified serial from the Received form
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
    data = client.post("/t/500/stage/shipped/preview", data={**SHIPPED, "tracking_number": ""}).get_json()
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
    client.post("/t/500/stage/shipped", data={**SHIPPED, "tracking_number": "1Z999AA10123456784"})
    today = repair_db_today()
    data = repair_db.report(today, today)
    assert data["stage_counts"]["received"] == 1 and data["stage_counts"]["added"] == 1
    assert data["turnaround"]["shipped"] == 1
    assert [(r["label"], r["n"]) for r in data["problems"]] == [("Trigger", 1)]
    assert data["techs"]["Kyler"]["total"] == 4

    resp = client.get("/report/")
    assert resp.status_code == 200 and b"Trigger" in resp.data
    assert b'id="report-data"' in resp.data and b"chart-activity" in resp.data

    charts = repair_db.report_charts(today, today)
    assert charts["days"] == [today.isoformat()]
    assert charts["daily"]["added"] == [1] and charts["daily"]["shipped"] == [1]
    assert sum(charts["spread"]["shop"]) == 1 and sum(charts["spread"]["total"]) == 1
    assert all(sum(v) == 0 for v in charts["aging"].values())  # it shipped, so nothing is open
    assert charts["previous"]["counts"] == {}

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
    client.post("/t/500/stage/shipped", data={**SHIPPED, "tracking_number": "1Z999AA10123456784"})
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
        data={**repaired(), "solution_ids": [rebed["id"], trig["id"]], "solution_other": "Lapped lugs", "solution_add": "1"},
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
    client.post("/t/500/stage/repaired", data={**repaired(), "solution_ids": [sol["id"]]})
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
        data={**RECEIVED, "accessory_ids": [scope["id"], sling["id"]], "arrival_damage": "yes", "arrival_notes": ""},
        follow_redirects=True,
    )
    assert b"Describe the damage" in resp.data and zd.updates == []
    client.post(
        "/t/500/stage/received",
        data={
            **RECEIVED,
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
    client.post("/t/500/stage/received", data=RECEIVED)
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
        data={**answers, "complaint_match": "partly", "headspace": "pass", "problem_ids": [trig["id"]]},
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
        data={"function_check": "pass", "test_required": "yes", "test_result": "pass", "group_size": "0.75", "group_unit": "moa",
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
        data={"function_check": "pass", "test_required": "yes", "test_result": "fail", "group_size": "2.5", "group_unit": "in",
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
        data={"function_check": "pass", "test_required": "yes", "test_result": "pass", "group_size": "0.8", "group_unit": "moa",
              "test_distance": "100", "test_ammo": "Federal GMM"},
    )
    client.post("/t/500/stage/tested", data={"function_check": "pass", "test_required": "no"})
    u = zd.updates[1]
    assert u["public"] is False and "not required" in u["body"]
    assert u["tags"] == ["repair-portal", "rp-tested"]  # Zendesk views/stage field still see the stage
    rma = repair_db.get_rma(500)
    assert rma["stage"] == "tested" and rma["test_required"] is False
    assert rma["group_size"] is None and rma["test_ammo"] is None and rma["test_distance"] is None


@needs_db
def test_accuracy_validates_numbers(client, zd, tech):
    _add(client)
    base = {"function_check": "pass", "test_required": "yes", "test_result": "pass", "group_unit": "moa", "test_ammo": "x"}
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
        data={"function_check": "pass", "test_required": "yes", "test_result": "fail", "group_size": "3", "group_unit": "moa",
              "test_distance": "100", "test_ammo": "Federal GMM"},
    )
    page = client.get("/t/500").data
    assert b"Ship anyway" in page
    resp = client.post("/t/500/stage/shipped", data={**SHIP_OK, "tracking_number": "1Z999AA10123456784"},
                       follow_redirects=True)
    assert b"failed its accuracy test" in resp.data and len(zd.updates) == 1
    client.post("/t/500/stage/shipped", data={**SHIP_OK, "tracking_number": "1Z999AA10123456784", "ship_failed": "1"})
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


# ---------------------------------------------------------------- QC pass: product data, verification, delivery, reports

import delivery  # noqa: E402
import tracking  # noqa: E402

ZD_RIFLE = [{"id": 301, "value": "CA-12345"}, {"id": 302, "value": "ridgeline_fft"},
            {"id": 303, "value": "6.5 Creedmoor"}, {"id": 304, "value": "1Z999AA10123456784"}]


def test_carrier_detection_and_links():
    assert tracking.detect_carrier("1Z 999 AA1 0123456784") == "ups"
    assert tracking.detect_carrier("776543219876") == "fedex"
    assert tracking.detect_carrier("9400111899223344556677") == "usps"
    assert tracking.detect_carrier("hello") is None
    assert tracking.tracking_url("1Z999AA10123456784").startswith("https://www.ups.com/")
    assert tracking.tracking_url("hello") == ""


def _ship_ready(client, zd):
    """Add, receive (serial verified) and pass the final test."""
    _add(client)
    client.post("/t/500/stage/received", data={**RECEIVED, "serial_number": "ca 12345"})
    client.post("/t/500/stage/tested", data=FINAL)


@needs_db
def test_ticket_fields_copied_for_product_mapping(client, zd, tech):
    zd.custom_fields = ZD_RIFLE + [{"id": 999, "value": "Dealer X"}]
    zd.fields[999] = {"id": 999, "title": "Dealer", "active": True}
    _add(client)
    rma = repair_db.get_rma(500)
    assert rma["zd_serial"] == "CA-12345" and rma["zd_model"] == "Ridgeline FFT"
    assert rma["zd_caliber"] == "6.5 Creedmoor" and rma["zd_inbound_tracking"] == "1Z999AA10123456784"
    assert {"title": "Dealer", "value": "Dealer X"} in rma["ticket_fields"]
    page = client.get("/t/500").data
    assert b"Ridgeline FFT" in page and b"CA-12345" in page and b"Dealer X" in page
    # CS corrects the ticket: the copy follows the next time the ticket is opened.
    zd.custom_fields = [{"id": 301, "value": "CA-99999"}]
    client.get("/t/500")
    assert repair_db.get_rma(500)["zd_serial"] == "CA-99999"


@needs_db
def test_field_mapping_in_settings_overrides_title_match(client, zd, tech):
    zd.fields[305] = {"id": 305, "title": "Rifle S/N (warranty card)", "active": True}
    zd.custom_fields = ZD_RIFLE + [{"id": 305, "value": "WC-777"}]
    resp = client.post("/admin/fields", data={"field_serial": "305"})
    assert resp.status_code == 302
    _add(client)
    assert repair_db.get_rma(500)["zd_serial"] == "WC-777"
    assert b"Rifle S/N (warranty card)" in client.get("/admin/").data


@needs_db
def test_received_checks_serial_against_ticket(client, zd, tech):
    zd.custom_fields = ZD_RIFLE
    _add(client)
    resp = client.post("/t/500/stage/received", data={**RECEIVED, "serial_number": "CA54321"}, follow_redirects=True)
    assert b"doesn&#39;t match the ticket" in resp.data and zd.updates == []
    client.post("/t/500/stage/received", data={**RECEIVED, "serial_number": "CA54321", "serial_confirm": "1"})
    u = zd.updates[0]
    assert u["public"] is False and "does not match the ticket (CA-12345)" in u["body"]
    rma = repair_db.get_rma(500)
    assert rma["serial_match"] is False and rma["flag"] == "Serial mismatch" and rma["stage"] == "received"
    assert repair_db.events_for(500)[0]["customer_message"] is None


@needs_db
def test_received_serial_match_ignores_spaces_and_dashes(client, zd, tech):
    zd.custom_fields = ZD_RIFLE
    _add(client)
    client.post("/t/500/stage/received", data={**RECEIVED, "serial_number": "ca 12345",
                                                "inbound_tracking": "1Z999AA10123456784"})
    assert zd.updates[0]["public"] is True
    rma = repair_db.get_rma(500)
    assert rma["serial_match"] is True and rma["inbound_match"] is True and rma["flag"] is None
    assert rma["model"] == "Ridgeline" and rma["caliber"] == "6.5 Creedmoor"


@needs_db
def test_received_requires_safety_check_and_flags_loaded(client, zd, tech):
    _add(client)
    data = dict(RECEIVED)
    del data["arrival_clear"]
    resp = client.post("/t/500/stage/received", data=data, follow_redirects=True)
    assert b"Safety first" in resp.data and zd.updates == []
    client.post("/t/500/stage/received", data={**RECEIVED, "arrival_clear": "loaded"})
    rma = repair_db.get_rma(500)
    assert rma["arrived_loaded"] is True and rma["flag"] == "Arrived loaded"


@needs_db
def test_repair_complete_needs_root_cause_and_coverage(client, zd, tech):
    _add(client)
    sol = _choice("solution", "Adjusted trigger")
    resp = client.post("/t/500/stage/repaired", data={"solution_ids": [sol["id"]]}, follow_redirects=True)
    assert b"root cause" in resp.data and zd.updates == []
    client.post("/t/500/stage/repaired", data={**repaired(), "solution_ids": [sol["id"]],
                                                "parts_used": "TRG-0042 x1", "trigger_pull_after": "3.25"})
    rma = repair_db.get_rma(500)
    assert rma["root_cause"] and rma["coverage"] == "warranty" and float(rma["trigger_pull_after"]) == 3.25
    assert "Root cause" not in zd.updates[0]["body"]  # internal only


@needs_db
def test_failed_function_check_is_internal_and_blocks_shipping(client, zd, tech):
    _add(client)
    client.post("/t/500/stage/tested", data={"function_check": "fail", "test_required": "no",
                                              "function_notes": "Safety doesn't engage"})
    u = zd.updates[0]
    assert u["public"] is False and "Safety doesn't engage" in u["body"]
    assert repair_db.get_rma(500)["flag"] == "Failed final check"
    resp = client.post("/t/500/stage/shipped", data={**SHIP_OK, "tracking_number": "1Z999AA10123456784"},
                       follow_redirects=True)
    assert b"failed its final safety" in resp.data and len(zd.updates) == 1


@needs_db
def test_shipping_checks_the_serial_going_in_the_box(client, zd, tech):
    zd.custom_fields = ZD_RIFLE
    _ship_ready(client, zd)
    n = len(zd.updates)
    resp = client.post("/t/500/stage/shipped", data={**SHIP_OK, "ship_serial": "CA99999",
                                                       "tracking_number": "1Z999AA10123456784"}, follow_redirects=True)
    assert b"Wrong rifle?" in resp.data and len(zd.updates) == n
    resp = client.post("/t/500/stage/shipped", data={"ship_serial": "CA12345", "tracking_number": "1Z999AA10123456784"},
                       follow_redirects=True)
    assert b"unloaded" in resp.data and len(zd.updates) == n
    client.post("/t/500/stage/shipped", data={**SHIP_OK, "tracking_number": "1Z999AA10123456784"})
    rma = repair_db.get_rma(500)
    assert rma["stage"] == "shipped" and rma["return_carrier"] == "ups" and rma["ship_override"] is False
    assert "https://www.ups.com/track?tracknum=1Z999AA10123456784" in zd.updates[-1]["body"]


@needs_db
def test_scan_finds_rma_by_serial_or_tracking(client, zd, tech):
    zd.custom_fields = ZD_RIFLE
    _add(client)
    resp = client.get("/scan/go?ticket_id=CA12345")  # ticket says CA-12345
    assert resp.headers["Location"].endswith("/t/500")
    resp = client.get("/scan/go?ticket_id=1Z999AA10123456784")  # the box's label
    assert resp.headers["Location"].endswith("/t/500")
    resp = client.get("/scan/go?ticket_id=500")  # the one-pager barcode
    assert resp.headers["Location"].endswith("/t/500")
    zd.search_results = [{"id": 812, "subject": "x", "status": "open", "created_at": "2026-11-01T00:00:00Z"}]
    resp = client.get("/scan/go?ticket_id=ZZ-UNKNOWN-1")  # not in the queue: Zendesk search
    assert resp.headers["Location"].endswith("/t/812")


@needs_db
def test_manual_mark_delivered_emails_once(client, zd, tech):
    _ship_ready(client, zd)
    client.post("/t/500/stage/shipped", data={**SHIP_OK, "tracking_number": "1Z999AA10123456784"})
    n = len(zd.updates)
    preview = client.post("/t/500/delivery/preview", data={"email": "1"}).get_json()
    assert preview["ok"] and preview["posts"][0]["public"] and len(zd.updates) == n
    client.post("/t/500/delivery/mark", data={"email": "1"})
    u = zd.updates[-1]
    assert u["public"] is True and "rp-delivered" in u["tags"] and "delivered on" in u["body"]
    assert u["body"] == preview["posts"][0]["body"]
    client.post("/t/500/delivery/mark", data={"email": "1"})
    assert len(zd.updates) == n + 1
    assert repair_db.get_rma(500)["delivered_at"] is not None
    assert repair_db.events_for(500)[0]["kind"] == "delivery"


@needs_db
def test_tracking_check_emails_when_delivered(client, zd, tech, monkeypatch):
    _ship_ready(client, zd)
    client.post("/t/500/stage/shipped", data={**SHIP_OK, "tracking_number": "1Z999AA10123456784"})
    n = len(zd.updates)
    monkeypatch.setenv("TRACKING_PROVIDER", "easypost")
    monkeypatch.setenv("EASYPOST_API_KEY", "test")
    answers = iter([
        {"status": "in_transit", "detail": "Departed facility", "delivered_at": None},
        {"status": "delivered", "detail": "Front door", "delivered_at": "2026-11-20T18:30:00Z"},
    ])
    monkeypatch.setattr(tracking, "check", lambda number, carrier: next(answers))
    assert delivery.check_all(zd, recheck_minutes=0) == {"in_transit": 1}
    assert len(zd.updates) == n and repair_db.get_rma(500)["delivery_status"] == "in_transit"
    assert delivery.check_all(zd, recheck_minutes=0) == {"delivered": 1}
    u = zd.updates[-1]
    assert u["public"] and "November 20" in u["body"] and "UPS" in u["body"]
    assert delivery.check_all(zd, recheck_minutes=0) == {}  # delivered RMAs aren't checked again
    assert len(zd.updates) == n + 1


@needs_db
def test_delivered_auto_email_can_be_turned_off(client, zd, tech, monkeypatch):
    _ship_ready(client, zd)
    client.post("/t/500/stage/shipped", data={**SHIP_OK, "tracking_number": "1Z999AA10123456784"})
    n = len(zd.updates)
    repair_db.save_settings({"delivered_auto": "0"})
    monkeypatch.setenv("TRACKING_PROVIDER", "easypost")
    monkeypatch.setenv("EASYPOST_API_KEY", "test")
    monkeypatch.setattr(tracking, "check", lambda n_, c: {"status": "delivered", "detail": "", "delivered_at": None})
    try:
        delivery.check_all(zd, recheck_minutes=0)
    finally:
        repair_db.save_settings({"delivered_auto": "1"})
    assert len(zd.updates) == n and repair_db.get_rma(500)["delivered_at"] is not None


@needs_db
def test_new_tracking_number_resets_delivery(client, zd, tech, monkeypatch):
    monkeypatch.setattr(portal_views, "DUPLICATE_WINDOW_SECONDS", 0)
    _ship_ready(client, zd)
    client.post("/t/500/stage/shipped", data={**SHIP_OK, "tracking_number": "1Z999AA10123456784"})
    client.post("/t/500/delivery/mark", data={})
    assert repair_db.get_rma(500)["delivered_at"] is not None
    client.post("/t/500/stage/shipped", data={**SHIP_OK, "tracking_number": "1Z999AA10123456784"})
    assert repair_db.get_rma(500)["delivered_at"] is not None  # same label: unchanged
    client.post("/t/500/stage/shipped", data={**SHIP_OK, "tracking_number": "776543219876"})
    rma = repair_db.get_rma(500)
    assert rma["delivered_at"] is None and rma["return_carrier"] == "fedex"


@needs_db
def test_final_report_review_edit_and_send(client, zd, tech):
    zd.custom_fields = ZD_RIFLE
    _add(client)
    client.post("/t/500/stage/received", data={**RECEIVED, "serial_number": "CA12345"})
    trig = _choice("problem", "Trigger")
    client.post("/t/500/stage/inspected", data={**inspected(), "problem_ids": [trig["id"]],
                                                 "problem_notes": "SECRET internal note"})
    sol = _choice("solution", "Adjusted trigger")
    client.post("/t/500/stage/repaired", data={**repaired(), "solution_ids": [sol["id"]], "parts_used": "TRG-0042"})
    queue = repair_db.final_report_queue()
    assert [r["ticket_id"] for r in queue["in_progress"]] == [500] and queue["ready"] == []
    client.post("/t/500/stage/tested", data=FINAL)
    client.post("/t/500/stage/shipped", data={**SHIP_OK, "tracking_number": "1Z999AA10123456784"})
    assert [r["ticket_id"] for r in repair_db.final_report_queue()["ready"]] == [500]
    assert b"Final reports" in client.get("/final-reports").data

    page = client.get("/t/500/final-report").data.decode()
    assert "Serial number: CA12345" in page and "Parts replaced: TRG-0042" in page
    assert "Ridgeline 6.5 Creedmoor" in page and "1Z999AA10123456784" in page
    draft = page.split('class="report-editor"', 1)[1]
    assert "SECRET" not in draft.split("</textarea>", 1)[0]  # internal notes stay out of the report

    n = len(zd.updates)
    edited = "Repair report for ticket #500\n\nAll fixed. Thanks for your patience with us."
    client.post("/t/500/final-report/save", data={"op": "save", "body": edited})
    assert repair_db.get_final_report(500)["body"] == edited and len(zd.updates) == n
    preview = client.post("/t/500/final-report/preview", data={"body": edited}).get_json()
    assert preview["ok"] and preview["posts"][0]["body"] == edited and len(zd.updates) == n
    client.post("/t/500/final-report/send", data={"body": edited})
    u = zd.updates[-1]
    assert u["public"] and u["body"] == edited and "rp-final-report" in u["tags"]
    client.post("/t/500/final-report/send", data={"body": edited})
    assert len(zd.updates) == n + 1  # never twice
    assert repair_db.final_report_queue()["sent"][0]["ticket_id"] == 500
    assert repair_db.events_for(500)[0]["kind"] == "report"
    # A corrected report can be sent after re-opening it.
    client.post("/t/500/final-report/save", data={"op": "reopen"})
    client.post("/t/500/final-report/send", data={"body": edited + " Correction."})
    assert len(zd.updates) == n + 2


@needs_db
def test_final_report_rebuild_discards_edits(client, zd, tech):
    _add(client)
    sol = _choice("solution", "Adjusted trigger")
    client.post("/t/500/stage/repaired", data={**repaired(), "solution_ids": [sol["id"]]})
    client.post("/t/500/final-report/save", data={"op": "save", "body": "My own words for this report, edited."})
    assert b"My own words" in client.get("/t/500/final-report").data
    client.post("/t/500/final-report/save", data={"op": "rebuild"})
    page = client.get("/t/500/final-report").data
    assert b"My own words" not in page and b"Work performed: Adjusted trigger" in page


@needs_db
def test_report_and_csv_include_qc_fields(client, zd, tech):
    zd.custom_fields = ZD_RIFLE
    _ship_ready(client, zd)
    resp = client.get("/report/")
    assert b"Root causes" in resp.data and b"Arrived loaded" in resp.data
    csv_text = client.get("/report/rmas.csv").data.decode("utf-8-sig")
    header = csv_text.splitlines()[0]
    for col in ("model", "caliber", "serial_number", "ticket_serial", "serial_match", "root_cause",
                "function_check", "delivered_at", "zendesk_fields"):
        assert col in header
    assert "Ridgeline" in csv_text and "CA12345" in csv_text


# ---------------------------------------------------------------- ATF A&D log

import atf_log  # noqa: E402

ZD_ADDRESS = [{"id": 306, "value": "12 Elk Rd, Gunnison CO 81230"}]


def _ad_fields(zd):
    zd.fields[306] = {"id": 306, "title": "Shipping address", "active": True}
    zd.fields[307] = {"id": 307, "title": "Requester email address", "active": True}
    zd.fields[308] = {"id": 308, "title": "Dealer FFL", "active": True}


@needs_db
def test_ad_received_opens_and_shipped_closes_entry(client, zd, tech, monkeypatch):
    monkeypatch.setattr(portal_views, "DUPLICATE_WINDOW_SECONDS", 0)
    _ad_fields(zd)
    zd.custom_fields = ZD_ADDRESS + [{"id": 307, "value": "jane@example.com"},
                                     {"id": 308, "value": "9-84-000-01-2A-12345"}]
    _add(client)
    assert atf_log.on_hand() == []  # added to the queue, not yet in the building
    client.post("/t/500/stage/received", data={**RECEIVED, "inbound_tracking": "1Z999AA10123456784"})
    [e] = atf_log.on_hand()
    assert e["serial_number"] == "CA12345" and e["model"] == "Ridgeline" and e["caliber"] == "6.5 Creedmoor"
    assert e["manufacturer"] == "Christensen Arms" and e["firearm_type"] == "Rifle"
    assert e["acquired_from_name"] == "Jane Hunter" and e["acquired_by"] == "Kyler"
    assert e["acquired_from_address"] == "12 Elk Rd, Gunnison CO 81230"  # not the email-address field
    assert e["acquired_from_ffl"] == "9-84-000-01-2A-12345" and e["review_note"] is None
    assert e["acquired_tracking"] == "1Z999AA10123456784"

    client.post("/t/500/stage/shipped", data={**SHIPPED, "tracking_number": "1Z999AA10123456785"})
    assert atf_log.on_hand() == []
    [e] = atf_log.entries_for_ticket(500)
    assert e["disposed_at"] is not None and e["disposed_to_name"] == "Jane Hunter"
    assert e["disposed_to_address"] == "12 Elk Rd, Gunnison CO 81230"
    assert e["disposed_serial"] == "CA12345" and e["disposed_tracking"] == "1Z999AA10123456785"
    assert e["disposed_by"] == "Kyler"
    today = repair_db_today()
    tz = repair_db.REPORT_TZ
    assert [r["id"] for r in atf_log.acquisitions(today, today, tz)] == [e["id"]]
    assert [r["id"] for r in atf_log.dispositions(today, today, tz)] == [e["id"]]
    assert atf_log.counts(today, today, tz) == {"on_hand": 0, "acquired": 1, "disposed": 1, "needs_review": 0}

    # Re-posting Shipped with a new label corrects the disposition, keeps its date, and logs it.
    first = e["disposed_at"]
    client.post("/t/500/stage/shipped", data={**SHIPPED, "tracking_number": "1Z999AA10123456786"})
    [e] = atf_log.entries_for_ticket(500)
    assert e["disposed_at"] == first and e["disposed_tracking"] == "1Z999AA10123456786"
    [c] = atf_log.changes_for(e["id"])
    assert c["before"] == {"disposed_tracking": "1Z999AA10123456785"} and c["changed_by"] == "Kyler"


@needs_db
def test_ad_received_repost_corrects_instead_of_duplicating(client, zd, tech, monkeypatch):
    monkeypatch.setattr(portal_views, "DUPLICATE_WINDOW_SECONDS", 0)
    _add(client)
    client.post("/t/500/stage/received", data={**RECEIVED, "serial_number": "CA12354"})
    client.post("/t/500/stage/received", data=RECEIVED)  # typo fixed
    [e] = atf_log.entries_for_ticket(500)
    assert e["serial_number"] == "CA12345" and e["disposed_at"] is None
    [c] = atf_log.changes_for(e["id"])
    assert c["before"] == {"serial_number": "CA12354"} and c["after"] == {"serial_number": "CA12345"}
    assert e["review_note"].startswith("No customer address")  # no address field on this ticket


@needs_db
def test_ad_move_back_from_shipped(client, zd, tech, monkeypatch):
    monkeypatch.setattr(portal_views, "DUPLICATE_WINDOW_SECONDS", 0)
    _add(client)
    client.post("/t/500/stage/received", data=RECEIVED)
    client.post("/t/500/stage/shipped", data={**SHIPPED, "tracking_number": "1Z999AA10123456784"})
    # Shipped by mistake: moving back to a repair stage clears the disposition (it never left).
    client.post("/t/500/stage/inspected", data={**inspected(), "problem_ids": [_choice("problem", "Trigger")["id"]],
                                                "move_back": "1"})
    [e] = atf_log.entries_for_ticket(500)
    assert e["disposed_at"] is None and e["disposed_tracking"] is None
    [c] = atf_log.changes_for(e["id"])
    assert "Moved back from Shipped" in c["reason"] and c["before"]["disposed_tracking"] == "1Z999AA10123456784"
    client.post("/t/500/stage/shipped", data={**SHIPPED, "tracking_number": "1Z999AA10123456785"})
    [e] = atf_log.entries_for_ticket(500)
    assert e["disposed_tracking"] == "1Z999AA10123456785"
    # The customer sends it back: Received again is a new acquisition; the first entry stays closed.
    client.post("/t/500/stage/received", data={**RECEIVED, "move_back": "1"})
    newer, older = atf_log.entries_for_ticket(500)
    assert newer["disposed_at"] is None and older["disposed_at"] is not None
    assert [r["id"] for r in atf_log.on_hand()] == [newer["id"]]


@needs_db
def test_ad_shipped_without_received_is_flagged(client, zd, tech):
    zd.custom_fields = ZD_RIFLE
    _add(client)
    client.post("/t/500/stage/shipped",
                data={**SHIPPED, "ship_serial": "CA-12345", "tracking_number": "1Z999AA10123456784"})
    [e] = atf_log.entries_for_ticket(500)
    assert e["disposed_at"] is not None and e["serial_number"] == "CA-12345"
    assert "without a Received scan" in e["review_note"]


@needs_db
def test_ad_pages_csv_search_and_officer_edit(client, zd, tech, monkeypatch):
    monkeypatch.setattr(portal_views, "DUPLICATE_WINDOW_SECONDS", 0)
    _add(client)
    client.post("/t/500/stage/received", data=RECEIVED)
    _add(client, 501)
    client.post("/t/501/stage/received", data={**RECEIVED, "serial_number": "CA77777"})
    client.post("/t/501/stage/shipped",
                data={**SHIPPED, "ship_serial": "CA77777", "tracking_number": "1Z999AA10123456784"})

    page = client.get("/ad/").data.decode()
    assert "In the building now" in page and "CA12345" in page and "CA77777" not in page
    book = client.get("/ad/?view=book").data.decode()
    assert "CA12345" in book and "CA77777" in book
    found = client.get("/ad/?q=ca-777").data.decode()
    assert "CA77777" in found and "CA12345" not in found
    assert "CA77777" in client.get("/ad/?q=501").data.decode()

    csv_text = client.get("/ad/log.csv?view=book").data.decode("utf-8-sig")
    header = csv_text.splitlines()[0]
    assert header.startswith("entry_id,ticket_id,manufacturer,model,caliber,type,serial_number,acquired_at")
    assert "CA77777" in csv_text and "Disposed" in csv_text and "In building" in csv_text

    [e] = atf_log.entries_for_ticket(500)
    assert e["review_note"]
    entry_page = client.get(f"/ad/{e['id']}").data.decode()
    assert "Correct this entry" in entry_page and "No corrections" in entry_page

    resp = client.post(f"/ad/{e['id']}", data={"acquired_from_address": "12 Elk Rd", "reason": ""},
                       follow_redirects=True)
    assert b"Say why" in resp.data and atf_log.get_entry(e["id"])["acquired_from_address"] is None
    client.post(f"/ad/{e['id']}", data={"acquired_from_address": "12 Elk Rd, Gunnison CO",
                                        "disposed_to_name": "Someone else",  # not shipped: ignored
                                        "reason": "From the box label", "clear_review": "1"})
    e2 = atf_log.get_entry(e["id"])
    assert e2["acquired_from_address"] == "12 Elk Rd, Gunnison CO" and e2["review_note"] is None
    assert e2["disposed_to_name"] is None
    [c] = atf_log.changes_for(e["id"])
    assert c["reason"] == "From the box label" and c["changed_by"] == "Kyler"
    assert set(c["after"]) == {"acquired_from_address", "review_note"}
    assert "From the box label" in client.get(f"/ad/{e['id']}").data.decode()


@needs_db
def test_ad_edit_needs_settings_password_and_sign_in(client, zd, tech, monkeypatch):
    _add(client)
    client.post("/t/500/stage/received", data=RECEIVED)
    [e] = atf_log.entries_for_ticket(500)
    monkeypatch.setenv("ADMIN_PASSWORD", "s3cret")
    resp = client.post(f"/ad/{e['id']}", data={"model": "X", "reason": "test"})
    assert resp.status_code == 302 and "/admin/login" in resp.headers["Location"]
    assert atf_log.get_entry(e["id"])["model"] == "Ridgeline"
    assert b"Settings password" in client.get(f"/ad/{e['id']}").data
    monkeypatch.setenv("ADMIN_PASSWORD", "")
    client.post("/tech/signout")
    resp = client.post(f"/ad/{e['id']}", data={"model": "X", "reason": "test"})
    assert resp.status_code == 302 and atf_log.get_entry(e["id"])["model"] == "Ridgeline"


@needs_db
def test_ad_settings_defaults(client, zd, tech):
    client.post("/admin/atf", data={"atf_manufacturer": "Other Maker", "atf_firearm_type": "Receiver"})
    _add(client)
    client.post("/t/500/stage/received", data=RECEIVED)
    [e] = atf_log.entries_for_ticket(500)
    assert e["manufacturer"] == "Other Maker" and e["firearm_type"] == "Receiver"


@needs_db
def test_demo_sample_history(client, zd, tech):
    from datetime import timedelta

    from tests import demo_history

    assert demo_history.seed(TEST_DSN) == 150
    assert demo_history.seed(TEST_DSN) == 0  # only ever fills an empty database
    assert [t["name"] for t in repair_db.list_techs()] == ["Kyler"]  # sample techs stay off sign-in
    assert repair_db.final_reports_ready_count() == demo_history.REPORTS_WAITING
    assert not repair_db.rma_ids([7101, 7102, 7103, 7104])  # left for visitors to add
    today = repair_db_today()
    data = repair_db.report(today - timedelta(days=89), today)
    assert data["turnaround"]["shipped"] > 50 and data["problems"] and data["techs"]
    charts = repair_db.report_charts(today - timedelta(days=89), today)
    assert sum(charts["daily"]["shipped"]) > 50
    assert any(n for counts in charts["aging"].values() for n in counts[3:])  # some open ones are late
    # The A&D log has what the Received / Shipped scans would have written.
    received = [r for r in repair_db.board(shipped_days=365).values() for r in r if r["received_at"]]
    on_hand = atf_log.on_hand()
    assert on_hand and all(e["acquired_from_address"] and e["serial_number"] for e in on_hand)
    assert len(on_hand) == sum(1 for r in received if r["stage"] != "shipped")
    assert not atf_log.needs_review()
    for path in ("/report/", "/queue", "/final-reports", "/t/6001", "/t/6001/report", "/ad/"):
        assert client.get(path).status_code == 200, path


# ---------------------------------------------------------------- repair intake

import hashlib  # noqa: E402
import re  # noqa: E402
from datetime import datetime, timezone  # noqa: E402

import intake  # noqa: E402
import intake_views  # noqa: E402
from flask.testing import FlaskClient  # noqa: E402

ANSWERS = {
    "model": "Ridgeline FFT", "serial": "CA12345", "caliber": "6.5 PRC",
    "issue": "Groups opened up to 3 MOA.\nStarted after a fall.",
    "check_specific": "Please check the crown",
    "concerns": ["Accuracy", "Stock or chassis"],
}


@pytest.fixture
def intake_on(monkeypatch):
    """Intake form switched on, with a public address and a go-live date before the fake tickets."""
    monkeypatch.setenv("REPAIR_INTAKE_ENABLED", "1")
    monkeypatch.setenv("REPAIR_INTAKE_BASE_URL", "https://repairs.example.com")
    monkeypatch.setenv("REPAIR_INTAKE_GO_LIVE_AT", "2026-10-01T00:00:00Z")
    for name in ("REPAIR_INTAKE_REMINDER_HOURS", "ZENDESK_INTAKE_SERIAL_FIELD_ID",
                 "ZENDESK_INTAKE_MODEL_FIELD_ID", "ZENDESK_INTAKE_CALIBER_FIELD_ID"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(intake_views, "_hits", {})  # the rate limiter starts empty


class _Browser(FlaskClient):
    """A second browser for tests that already hold the signed-in ``client``.

    ``client`` is entered as a context manager, so it keeps its last request's
    app context alive, and a request from another client would reuse it and
    with it ``g`` (where auth caches the signed-in tech). Each request here
    gets its own app context, as it does in production.
    """

    def open(self, *args, **kwargs):
        with flask_app.app_context():
            return super().open(*args, **kwargs)


def _browser():
    return _Browser(flask_app, flask_app.response_class, use_cookies=True)


@pytest.fixture
def public():
    """A customer's browser: no tech signed in."""
    return _browser()


def _link_token(zd):
    """The token in the most recent link the fake Zendesk was asked to email."""
    for update in reversed(zd.updates):
        found = re.search(r"/repair/intake/([A-Za-z0-9_-]{43})(?![A-Za-z0-9_-])", update["body"] or "")
        if found:
            return found.group(1)
    raise AssertionError("no intake link was posted")


def _sql(query, params=()):
    with psycopg.connect(TEST_DSN, autocommit=True) as conn:
        return conn.execute(query, params).fetchall() if query.lstrip().upper().startswith("SELECT") \
            else conn.execute(query, params)


def _linked(client, zd, ticket_id=500):
    """Add a ticket with the form on; returns the customer's URL path."""
    _add(client, ticket_id)
    return f"/repair/intake/{_link_token(zd)}"


def _submit(public, url, **overrides):
    data = {**ANSWERS, **overrides}
    return public.post(url, data=data, content_type="multipart/form-data")


def _failed_submit(client, zd, public, status=502):
    """A submitted intake whose first write-back Zendesk refused. Returns the row."""
    url = _linked(client, zd)
    zd.fail, zd.fail_status = True, status
    assert _submit(public, url, photos=[_file("a.jpg", _img())]).status_code == 303
    zd.fail = False
    row = intake.for_ticket(500)
    assert row["status"] == "sync_failed"
    return row


@needs_db
def test_intake_off_by_default(client, zd, tech, monkeypatch):
    monkeypatch.delenv("REPAIR_INTAKE_ENABLED", raising=False)
    _add(client)
    assert zd.updates == []
    assert zd.tag_calls == [(500, ["repair-portal", "rp-queued"])]
    assert intake.for_ticket(500) is None
    page = client.get("/t/500").get_data(as_text=True)
    assert "Customer intake form" not in page
    assert "form sent" not in client.get("/queue").get_data(as_text=True)


@needs_db
def test_add_posts_intake_link(client, zd, tech, intake_on):
    assert b"The customer is emailed a link to the intake form." in client.get("/t/500").data
    resp = client.post("/t/500/add", follow_redirects=True)
    assert b"The customer was emailed the intake form link." in resp.data
    assert len(zd.updates) == 1
    u = zd.updates[0]
    assert u["public"] is True and u["tags"] == ["repair-portal", "repair_intake_sent"]
    assert u["html_body"] is None and u["author_id"] is None and u["uploads"] == []
    token = _link_token(zd)
    assert f"https://repairs.example.com/repair/intake/{token}\n" in u["body"]
    assert u["body"].startswith("Hi Jane,") and "preparing to receive your firearm" in u["body"]
    assert "works for 30 days" in u["body"] and "{" not in u["body"]
    assert zd.tag_calls == [(500, ["repair-portal", "rp-queued"])]  # the queue tag call is unchanged

    row = intake.for_ticket(500)
    assert row["status"] == "link_sent" and row["link_send_count"] == 1 and row["link_sent_at"] is not None
    assert row["token_hash"] == hashlib.sha256(token.encode()).hexdigest()
    assert token not in str(row)  # only the hash is kept
    assert row["created_by"] == "Kyler" and row["last_error"] is None and row["next_attempt_at"] is None
    days = (row["expires_at"] - datetime.now(timezone.utc)).total_seconds() / 86400
    assert 29.9 < days < 30.1
    ev = repair_db.events_for(500)[0]
    assert ev["kind"] == "intake" and ev["label"] == "Intake form link sent" and ev["tech_name"] == "Kyler"
    assert repair_db.get_rma(500)["intake_status"] == "link_sent"


@needs_db
@pytest.mark.parametrize("case, warned", [
    ("go_live_later", False), ("go_live_garbage", True), ("solved", False), ("tagged", False),
    ("no_base_url", True),
])
def test_link_guards(client, zd, tech, intake_on, monkeypatch, case, warned):
    if case == "go_live_later":
        monkeypatch.setenv("REPAIR_INTAKE_GO_LIVE_AT", "2026-11-15")  # the ticket is from Nov 1
    elif case == "go_live_garbage":
        monkeypatch.setenv("REPAIR_INTAKE_GO_LIVE_AT", "next tuesday")
    elif case == "solved":
        zd.status = "solved"
    elif case == "tagged":
        zd.tags = ["repair_intake_sent"]
    else:
        monkeypatch.delenv("REPAIR_INTAKE_BASE_URL")
        monkeypatch.delenv("PORTAL_BASE_URL", raising=False)
    resp = client.post("/t/500/add", follow_redirects=True)
    assert repair_db.get_rma(500) is not None  # the add itself always goes through
    assert zd.updates == [] and intake.for_ticket(500) is None
    assert b"emailed the intake form link" not in resp.data
    assert (b"The intake form link was not sent to the customer" in resp.data) is warned
    assert b"Customer intake form" in resp.data and b"No link has been sent for this ticket." in resp.data


@needs_db
def test_go_live_unset_sends(client, zd, tech, intake_on, monkeypatch):
    monkeypatch.delenv("REPAIR_INTAKE_GO_LIVE_AT")
    zd.created_at = "2019-03-01T00:00:00Z"  # no cutoff: the ticket's age does not matter
    _add(client)
    assert len(zd.updates) == 1 and intake.for_ticket(500)["status"] == "link_sent"


@needs_db
def test_portal_base_url_is_never_used_for_the_link(client, zd, tech, intake_on, monkeypatch):
    """PORTAL_BASE_URL is the shop-network address: a customer could not open a link built from it."""
    monkeypatch.delenv("REPAIR_INTAKE_BASE_URL")
    monkeypatch.setenv("PORTAL_BASE_URL", "https://portal.example.com/")
    resp = _add(client)
    assert zd.updates == [] and intake.for_ticket(500) is None
    assert repair_db.get_rma(500) is not None  # the ticket is still queued


@needs_db
def test_link_sent_once(client, zd, tech, intake_on):
    _add(client)
    resp = client.post("/t/500/add", follow_redirects=True)
    assert b"already in the repair queue" in resp.data
    assert len(zd.updates) == 1
    ticket, requester, _org = zd.get_ticket(500)
    assert intake.send_link(zd, ticket, requester, tech) == "already_tagged"
    # Even with the tag gone (two techs adding at once never see it), the row is the fence.
    zd.tags.clear()
    ticket, requester, _org = zd.get_ticket(500)
    assert intake.send_link(zd, ticket, requester, tech) == "exists"
    assert len(zd.updates) == 1 and intake.for_ticket(500)["link_send_count"] == 1


@needs_db
def test_link_failure_keeps_rma(client, zd, tech, intake_on):
    zd.fail, zd.fail_status = True, 503
    resp = client.post("/t/500/add", follow_redirects=True)
    assert b"Added ticket #500 to the repair queue" in resp.data
    assert b"The intake form link was not sent to the customer" in resp.data
    assert repair_db.get_rma(500)["stage"] == "queued" and zd.updates == []
    row = intake.for_ticket(500)
    assert row["status"] == "pending_link" and "503" in row["last_error"]
    assert row["attempts"] == 1 and row["next_attempt_at"] is not None and row["link_send_count"] == 0
    assert b"Customer intake forms that need a look" in client.get("/queue").data
    assert b"<strong>not</strong> reached the customer" in client.get("/t/500").data

    zd.fail = False
    resp = client.post("/t/500/intake/send", follow_redirects=True)
    assert b"Sent the customer a link to the intake form" in resp.data
    assert len(zd.updates) == 1
    # Nothing reached the customer before, so this is the first-link wording.
    assert "preparing to receive your firearm" in zd.updates[0]["body"]
    assert "no longer works" not in zd.updates[0]["body"]
    row = intake.for_ticket(500)
    assert row["status"] == "link_sent" and row["last_error"] is None and row["next_attempt_at"] is None
    assert row["token_hash"] == intake.hash_token(_link_token(zd))
    assert b"Customer intake forms that need a look" not in client.get("/queue").data


@needs_db
def test_link_permanent_failure_is_not_retried(client, zd, tech, intake_on):
    zd.fail, zd.fail_status = True, 403
    _add(client)
    row = intake.for_ticket(500)
    assert row["status"] == "pending_link" and row["next_attempt_at"] is None
    zd.fail = False
    assert intake.sweep(zd) == {} and zd.updates == []


@needs_db
def test_link_retried_by_sweep(client, zd, tech, intake_on):
    zd.fail, zd.fail_status = True, 503
    _add(client)
    first_hash = intake.for_ticket(500)["token_hash"]
    zd.fail = False
    assert intake.sweep(zd) == {} and zd.updates == []  # backoff not up yet
    _sql("UPDATE repair.intake SET next_attempt_at = now() - interval '1 minute'")
    assert intake.sweep(zd) == {"links": 1}
    assert len(zd.updates) == 1 and zd.updates[0]["tags"] == ["repair-portal", "repair_intake_sent"]
    row = intake.for_ticket(500)
    assert row["status"] == "link_sent" and row["token_hash"] == intake.hash_token(_link_token(zd))
    assert row["token_hash"] != first_hash  # the first token was never emailed and is unknown now
    assert intake.sweep(zd) == {} and len(zd.updates) == 1


@needs_db
def test_link_lost_response_recovered(client, zd, tech, intake_on):
    zd.fail, zd.fail_status = True, 503
    _add(client)
    before = intake.for_ticket(500)
    assert before["status"] == "pending_link"
    # The PUT did land (the tag is on the ticket); only its answer was lost.
    zd.fail = False
    zd.tags = ["repair-portal", "repair_intake_sent"]
    _sql("UPDATE repair.intake SET next_attempt_at = now() - interval '1 minute'")
    assert intake.sweep(zd) == {"links": 1}
    row = intake.for_ticket(500)
    assert row["status"] == "link_sent" and row["link_sent_at"] is not None
    assert zd.updates == []  # no second email
    assert row["token_hash"] == before["token_hash"]  # the emailed link still works


@needs_db
def test_resend_rotates_token(client, zd, tech, intake_on, public):
    old = _linked(client, zd)
    assert public.get(old).status_code == 200

    # A send needs a signed-in tech.
    resp = public.post("/t/500/intake/send")
    assert resp.status_code == 302 and len(zd.updates) == 1

    resp = client.post("/t/500/intake/send", follow_redirects=True)
    assert b"Sent the customer a link to the intake form" in resp.data
    assert len(zd.updates) == 2
    u = zd.updates[1]
    assert u["public"] is True and "no longer works" in u["body"]
    new = f"/repair/intake/{_link_token(zd)}"
    assert new != old
    row = intake.for_ticket(500)
    assert row["token_hash"] == intake.hash_token(new.rsplit("/", 1)[1])
    assert row["status"] == "link_sent" and row["link_send_count"] == 2
    assert repair_db.events_for(500)[0]["label"] == "New intake form link sent"

    gone = public.get(old)
    assert gone.status_code == 404 and b"This link is no longer active" in gone.data
    assert public.get(new).status_code == 200

    # Once the customer has answered there is nothing to resend.
    assert _submit(public, new).status_code == 303
    sent = len(zd.updates)
    resp = client.post("/t/500/intake/send", follow_redirects=True)
    assert b"already sent the form" in resp.data
    assert len(zd.updates) == sent and intake.for_ticket(500)["token_hash"] == row["token_hash"]


@needs_db
def test_failed_resend_is_reported_and_can_be_repeated(client, zd, tech, intake_on, public):
    old = _linked(client, zd)
    zd.fail, zd.fail_status = True, 503
    resp = client.post("/t/500/intake/send", follow_redirects=True)
    assert b"so the link was not sent" in resp.data and b"503" in resp.data
    row = intake.for_ticket(500)
    assert row["status"] == "pending_link" and row["link_send_count"] == 1 and len(zd.updates) == 1
    # A staff send is never retried behind their back: the sweep can't tell whether it landed.
    assert row["next_attempt_at"] is None
    assert intake.sweep(zd, force=True) == {} and len(zd.updates) == 1
    assert b"link not sent" in client.get("/queue").data
    zd.fail = False
    client.post("/t/500/intake/send")
    assert len(zd.updates) == 2 and "no longer works" in zd.updates[1]["body"]
    assert intake.for_ticket(500)["status"] == "link_sent"
    assert public.get(old).status_code == 404
    assert public.get(f"/repair/intake/{_link_token(zd)}").status_code == 200


@needs_db
def test_resend_refused_for_solved_ticket_and_unqueued_ticket(client, zd, tech, intake_on):
    resp = client.post("/t/777/intake/send", follow_redirects=True)  # never added
    assert b"in the repair queue. Add it first." in resp.data
    assert zd.updates == [] and intake.for_ticket(777) is None
    _add(client)
    zd.status = "solved"
    resp = client.post("/t/500/intake/send", follow_redirects=True)
    assert b"solved or closed in Zendesk" in resp.data
    assert len(zd.updates) == 1


@needs_db
def test_staff_send_creates_the_intake_for_older_tickets(client, zd, tech, monkeypatch):
    _add(client)  # added while the form was off
    assert intake.for_ticket(500) is None
    monkeypatch.setenv("REPAIR_INTAKE_ENABLED", "1")
    monkeypatch.setenv("REPAIR_INTAKE_BASE_URL", "https://repairs.example.com")
    monkeypatch.setenv("REPAIR_INTAKE_GO_LIVE_AT", "2027-01-01")  # the button ignores the cutoff
    assert b"No link has been sent for this ticket." in client.get("/t/500").data
    client.post("/t/500/intake/send")
    assert len(zd.updates) == 1 and "preparing to receive your firearm" in zd.updates[0]["body"]
    assert intake.for_ticket(500)["status"] == "link_sent"


@needs_db
def test_public_form_get(client, zd, tech, intake_on, public):
    zd.custom_fields = ZD_RIFLE
    url = _linked(client, zd)
    resp = public.get(url)
    page = resp.get_data(as_text=True)
    assert resp.status_code == 200
    assert "Tell us about your rifle" in page
    assert "preparing to receive your firearm" in page
    assert 'value="CA-12345"' in page and 'value="Ridgeline FFT"' in page and 'value="6.5 Creedmoor"' in page
    assert "Jane Hunter" not in page and "jane@example.com" not in page  # no contact details on the form
    assert 'enctype="multipart/form-data"' in page and 'name="photos"' in page
    # A customer page: no staff navigation, names or links into the portal.
    for staff_only in ("Kyler", "Sign out", "/queue", "/scan", "/tech", "/admin", "#500", "Incoming"):
        assert staff_only not in page, staff_only
    assert resp.headers["Cache-Control"] == "no-store"
    assert resp.headers["Referrer-Policy"] == "no-referrer"
    assert resp.headers["X-Robots-Tag"] == "noindex"
    assert "Set-Cookie" not in resp.headers  # the link is the key: no session, no cookie

    malformed = public.get("/repair/intake/not-a-token")
    unknown = public.get("/repair/intake/" + "A" * 43)
    _sql("UPDATE repair.intake SET expires_at = now() - interval '1 minute'")
    expired = public.get(url)
    assert (malformed.status_code, unknown.status_code, expired.status_code) == (404, 404, 410)
    for dead in (malformed, unknown, expired):
        text = dead.get_data(as_text=True)
        assert "This link is no longer active" in text and "reply to the email" in text
        assert "#500" not in text and 'name="serial"' not in text
        assert dead.headers["Cache-Control"] == "no-store"
    assert malformed.data == unknown.data == expired.data  # a guesser learns nothing from the page


@needs_db
def test_public_pages_when_database_is_down(client, zd, tech, intake_on, public, monkeypatch):
    url = _linked(client, zd)
    monkeypatch.setattr(db, "available", lambda: False)
    for resp in (public.get(url), _submit(public, url)):
        assert resp.status_code == 503 and b"We can&#39;t open the form right now" in resp.data
    monkeypatch.undo()
    assert intake.for_ticket(500)["status"] == "link_sent"


@needs_db
def test_submit_validation(client, zd, tech, intake_on, public):
    url = _linked(client, zd)
    resp = public.post(url, data={"model": "Ridgeline <FFT>", "issue": "  ", "ammo": "Hornady ELD-X",
                                  "concerns": ["Feeding", "Not on the list"],
                                  "optic": "Leupold VX-5"}, content_type="multipart/form-data")
    page = resp.get_data(as_text=True)
    assert resp.status_code == 422
    assert "Almost there." in page
    assert 'value="Ridgeline &lt;FFT&gt;"' in page and 'value="Hornady ELD-X"' in page and 'value="Leupold VX-5"' in page
    assert "Please fill in: Serial number" in page and "Please fill in: What&#39;s going on" in page
    assert "Please fill in: Model" not in page and "Please fill in: Ammunition" not in page
    assert 'value="Feeding" checked' in page and page.count(" checked") == 1  # ticks survive the re-render
    row = intake.for_ticket(500)
    assert row["status"] == "link_sent" and row["answers"] == {} and row["submitted_at"] is None
    assert len(zd.updates) == 1  # only the link


@needs_db
def test_submit_syncs(client, zd, tech, intake_on, public):
    url = _linked(client, zd)
    resp = _submit(public, url, photos=[_file("a.jpg", _img()), _file("b.heic", _img("HEIF"))])
    assert resp.status_code == 303 and resp.headers["Location"].endswith(url)
    done = public.get(url)
    assert done.status_code == 200 and b"Thanks, we have it." in done.data

    row = intake.for_ticket(500)
    assert row["status"] == "synced" and row["synced_at"] is not None and row["submitted_at"] is not None
    assert row["claimed_until"] is None and row["last_error"] is None and row["next_attempt_at"] is None
    assert {k: row["answers"][k] for k in ANSWERS} == ANSWERS
    assert row["answers"]["optic"] == ""

    assert len(zd.updates) == 2
    u = zd.updates[1]
    assert u["body"] is None and u["public"] is True and u["author_id"] == 1
    assert u["tags"] == ["repair-portal", "repair_intake_received"]
    assert u["custom_fields"] == {}  # no ZENDESK_INTAKE_*_FIELD_ID set
    html = u["html_body"]
    assert "<strong>Model:</strong> Ridgeline FFT" in html and "<strong>Serial number:</strong> CA12345" in html
    assert "Groups opened up to 3 MOA.<br>Started after a fall." in html
    assert "Accuracy, Stock or chassis" in html and "Please check the crown" in html
    assert f"Intake form reference RI-{row['id']}" in html and f"<!-- intake:{row['id']} -->" in html
    assert "Optic" not in html  # blank answers are left out
    sent = [zd.uploaded[t] for t in u["uploads"]]
    assert [(name, ctype) for name, _data, ctype in sent] == [("photo-1.jpg", "image/jpeg"),
                                                             ("photo-2.jpg", "image/jpeg")]
    assert sent[0][1] == _img() and sent[1][1][:3] == b"\xff\xd8\xff"  # the HEIC went as a JPEG

    assert _sql("SELECT count(*) AS n FROM repair.intake_file")[0][0] == 0
    ev = repair_db.events_for(500)[0]
    assert (ev["kind"], ev["label"], ev["tech_name"]) == ("intake", "Customer intake form received", "Customer")
    assert repair_db.get_rma(500)["intake_status"] == "synced"
    # The customer's answers do not overwrite what the tech verifies at Received.
    assert repair_db.get_rma(500)["serial_number"] is None


@needs_db
def test_submit_escapes_html(client, zd, tech, intake_on, public):
    url = _linked(client, zd)
    nasty = "<script>alert(1)</script>"
    assert _submit(public, url, issue=nasty, model='<img src=x onerror="x">', ammo="A & B").status_code == 303
    html = zd.updates[-1]["html_body"]
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html and "<script" not in html
    assert "<img" not in html and "&lt;img src=x onerror=&#34;x&#34;&gt;" in html
    assert "A &amp; B" in html
    for page in (public.get(url).get_data(as_text=True), client.get("/t/500").get_data(as_text=True)):
        assert nasty not in page and "<img src=x" not in page
        assert "&lt;script&gt;alert(1)&lt;/script&gt;" in page


def _storage(name, data):
    import io

    from werkzeug.datastructures import FileStorage

    return FileStorage(stream=io.BytesIO(data), filename=name)


def test_photo_rules(monkeypatch):
    jpg, png, webp, pdf = _img(), _img("PNG"), _img("WEBP"), b"%PDF-1.4 test"
    assert [intake.sniff(d) for d in (jpg, png, webp, pdf, _img("HEIF"), b"MZ\x90\x00", b"PK\x03\x04xx", b"")] == \
        ["jpg", "png", "webp", "pdf", "heic", None, None, None]

    uploads, err = intake.read_photos([_storage(f"p{i}.jpg", jpg) for i in range(6)])
    assert err is None and len(uploads) == 6
    uploads, err = intake.read_photos([_storage(f"p{i}.jpg", jpg) for i in range(7)])
    assert uploads == [] and "at most 6" in err

    uploads, err = intake.read_photos([_storage("a.jpg", b"MZ\x90\x00 not a photo")])
    assert uploads == [] and "a.jpg" in err and "isn't a file we can take" in err
    uploads, err = intake.read_photos([_storage("sheet.xlsx", b"PK\x03\x04xx")])
    assert uploads == [] and "sheet.xlsx" in err

    # The type comes from the bytes and the name is ours, whatever the customer's file was called.
    uploads, err = intake.read_photos([
        _storage("target.webp", webp), _storage("receipt.pdf", pdf), _storage("x.pdf", png),
        _storage("../../etc/passwd.JPG", jpg), _storage("empty.jpg", b""), _storage("", b""),
    ])
    assert err is None
    assert [(u.filename, u.content_type) for u in uploads] == [
        ("photo-1.webp", "image/webp"), ("photo-2.pdf", "application/pdf"), ("photo-3.png", "image/png"),
        ("photo-4.jpg", "image/jpeg"),
    ]
    assert uploads[2].data == png

    monkeypatch.setattr(intake, "MAX_PHOTO_BYTES", len(jpg) - 1)
    uploads, err = intake.read_photos([_storage("big.jpg", jpg)])
    assert uploads == [] and "big.jpg" in err and "is over" in err
    monkeypatch.setattr(intake, "MAX_PHOTO_BYTES", len(jpg))
    uploads, err = intake.read_photos([_storage("fits.jpg", jpg)])
    assert err is None and len(uploads) == 1


@needs_db
def test_bad_photo_keeps_the_answers_and_saves_nothing(client, zd, tech, intake_on, public):
    url = _linked(client, zd)
    resp = _submit(public, url, photos=[_file("ok.jpg", _img()), _file("run.jpg", b"MZ\x90\x00")])
    page = resp.get_data(as_text=True)
    assert resp.status_code == 422 and "isn&#39;t a file we can take" in page
    assert 'value="CA12345"' in page and "Started after a fall." in page
    assert intake.for_ticket(500)["status"] == "link_sent" and len(zd.updates) == 1
    assert _sql("SELECT count(*) AS n FROM repair.intake_file")[0][0] == 0
    resp = _submit(public, url, photos=[_file(f"p{i}.jpg", _img()) for i in range(7)])
    assert resp.status_code == 422 and b"at most 6" in resp.data
    assert intake.for_ticket(500)["status"] == "link_sent"


@needs_db
def test_custom_fields(client, zd, tech, intake_on, public, monkeypatch):
    assert intake.field_values(ANSWERS) == {}
    monkeypatch.setenv("ZENDESK_INTAKE_SERIAL_FIELD_ID", "301")
    monkeypatch.setenv("ZENDESK_INTAKE_MODEL_FIELD_ID", "302")
    monkeypatch.setenv("ZENDESK_INTAKE_CALIBER_FIELD_ID", "not-a-number")
    assert intake.field_values(ANSWERS) == {301: "CA12345", 302: "Ridgeline FFT"}
    monkeypatch.setenv("ZENDESK_INTAKE_CALIBER_FIELD_ID", "303")
    url = _linked(client, zd)
    assert _submit(public, url).status_code == 303
    assert zd.updates[-1]["custom_fields"] == {301: "CA12345", 302: "Ridgeline FFT", 303: "6.5 PRC"}
    row = intake.for_ticket(500)
    assert row["status"] == "synced" and row["last_error"] is None


@needs_db
def test_custom_fields_refused_posts_without_them(client, zd, tech, intake_on, public, monkeypatch):
    monkeypatch.setenv("ZENDESK_INTAKE_MODEL_FIELD_ID", "302")  # a drop-down that won't take free text
    zd.fail_fields = True
    url = _linked(client, zd)
    assert _submit(public, url, photos=[_file("a.jpg", _img())]).status_code == 303
    assert len(zd.updates) == 2
    u = zd.updates[-1]
    assert u["custom_fields"] == {} and "Ridgeline FFT" in u["html_body"] and len(u["uploads"]) == 1
    row = intake.for_ticket(500)
    assert row["status"] == "synced"
    assert row["last_error"].startswith("Posted without the Zendesk ticket fields")
    assert b"Posted without the Zendesk ticket fields" in client.get("/t/500").data
    assert b"Customer intake forms that need a look" not in client.get("/queue").data


@needs_db
def test_submitted_is_read_only(client, zd, tech, intake_on, public):
    url = _linked(client, zd)
    assert _submit(public, url).status_code == 303
    resp = public.get(url)
    page = resp.get_data(as_text=True)
    assert resp.status_code == 200 and "Thanks, we have it." in page
    assert "Ridgeline FFT" in page and "CA12345" in page and "Please check the crown" in page
    assert "Accuracy, Stock or chassis" in page
    assert "<form" not in page and 'name="serial"' not in page
    assert resp.headers["Cache-Control"] == "no-store"

    sent = len(zd.updates)
    again = _submit(public, url, serial="CHANGED", optic="Other Scope")
    assert again.status_code in (200, 303)
    if again.status_code == 200:
        assert b"Thanks, we have it." in again.data and b"Other Scope" not in again.data
    assert len(zd.updates) == sent
    row = intake.for_ticket(500)
    assert row["answers"]["serial"] == "CA12345" and row["status"] == "synced"
    assert intake.save_submission(row["id"], {**ANSWERS, "serial": "CHANGED"}, []) is False


@needs_db
@pytest.mark.parametrize("how", ["sweep", "button"])
def test_sync_failure_then_retry(client, zd, tech, intake_on, public, how):
    url = _linked(client, zd)
    zd.fail, zd.fail_status = True, 502
    resp = _submit(public, url, photos=[_file("a.jpg", _img())])
    assert resp.status_code == 303  # the customer never sees the Zendesk problem
    assert b"Thanks, we have it." in public.get(url).data
    row = intake.for_ticket(500)
    assert row["status"] == "sync_failed" and row["attempts"] == 1 and "502" in row["last_error"]
    assert row["next_attempt_at"] is not None and row["claimed_until"] is None
    assert row["file_count"] == 1 and row["answers"]["serial"] == "CA12345"
    assert len(zd.updates) == 1
    assert b"form sync failed" in client.get("/queue").data
    page = client.get("/t/500").data
    assert b"<strong>not</strong> on the Zendesk ticket yet" in page and b"Retry Zendesk" in page
    assert b"Please check the crown" in page  # the answers are safe here meanwhile

    zd.fail = False
    assert intake.sweep(zd) == {}  # not due yet
    assert len(zd.updates) == 1
    if how == "sweep":
        assert intake.sweep(zd, force=True) == {"synced": 1}
    else:
        resp = client.post("/t/500/intake/retry", follow_redirects=True)
        assert b"intake form is on the Zendesk ticket now" in resp.data
    assert len(zd.updates) == 2
    u = zd.updates[1]
    assert zd.uploaded[u["uploads"][0]][0] == "photo-1.jpg" and len(u["uploads"]) == 1
    assert u["author_id"] == 1 and "CA12345" in u["html_body"]
    row = intake.for_ticket(500)
    assert row["status"] == "synced" and row["file_count"] == 0 and row["last_error"] is None
    assert intake.sweep(zd, force=True) == {} and len(zd.updates) == 2


@needs_db
def test_sync_retry_due_without_force(client, zd, tech, intake_on, public):
    row = _failed_submit(client, zd, public)
    _sql("UPDATE repair.intake SET next_attempt_at = now() - interval '1 minute'")
    assert intake.sweep(zd) == {"synced": 1}
    assert intake.for_ticket(500)["status"] == "synced" and row["id"] == intake.for_ticket(500)["id"]


@needs_db
def test_sync_gives_up_after_max_attempts(client, zd, tech, intake_on, public):
    _failed_submit(client, zd, public)
    _sql("UPDATE repair.intake SET attempts = %s", (intake.MAX_ATTEMPTS - 1,))
    zd.fail = True
    assert intake.sweep(zd, force=True) == {"failed": 1}
    row = intake.for_ticket(500)
    assert row["status"] == "sync_failed" and row["next_attempt_at"] is None
    assert row["attempts"] == intake.MAX_ATTEMPTS and row["file_count"] == 1


@needs_db
@pytest.mark.parametrize("seen, expected", [("comment", "already"), ("marker", "already"), ("tag", "already"),
                                            ("other_intake", "synced")])
def test_sync_idempotent(client, zd, tech, intake_on, public, seen, expected):
    row = _failed_submit(client, zd, public)
    _sql("UPDATE repair.intake SET status = 'submitted'")
    if seen == "comment":  # the PUT landed, our own update did not
        zd.comments[500].append({"body": None, "html_body": f"<p>Intake form reference RI-{row['id']}</p>"})
    elif seen == "marker":
        zd.comments[500].append({"body": f"x <!-- intake:{row['id']} -->", "html_body": None})
    elif seen == "tag":
        zd.tags.append("repair_intake_received")
    else:  # RI-1 must not be mistaken for RI-12
        zd.comments[500].append({"body": f"Intake form reference RI-{row['id']}2", "html_body": None})
    sent = len(zd.updates)
    assert intake.sync(zd, row["id"]) == expected
    assert len(zd.updates) == sent + (1 if expected == "synced" else 0)
    after = intake.for_ticket(500)
    assert after["status"] == "synced" and after["file_count"] == 0 and after["claimed_until"] is None
    assert repair_db.events_for(500)[0]["label"] == "Customer intake form received"
    assert intake.sync(zd, row["id"]) == "already"  # a synced row is left alone
    assert len(zd.updates) == sent + (1 if expected == "synced" else 0)


@needs_db
def test_sync_claim(client, zd, tech, intake_on, public):
    row = _failed_submit(client, zd, public)
    sent = len(zd.updates)
    _sql("UPDATE repair.intake SET claimed_until = now() + interval '5 minutes'")
    assert intake.sync(zd, row["id"]) == "busy"
    assert intake.sync(zd, row["id"], force=True) == "busy"  # force never steals a live lease
    assert intake.sweep(zd, force=True) == {}
    resp = client.post("/t/500/intake/retry", follow_redirects=True)
    assert b"Another attempt is running right now" in resp.data
    assert len(zd.updates) == sent and intake.for_ticket(500)["status"] == "sync_failed"
    assert intake.for_ticket(500)["attempts"] == row["attempts"]

    # A worker that died mid-upload leaves a lease that runs out.
    _sql("UPDATE repair.intake SET status = 'submitted', claimed_until = now() - interval '1 minute'")
    assert intake.sweep(zd) == {"synced": 1}
    assert len(zd.updates) == sent + 1 and intake.for_ticket(500)["status"] == "synced"
    assert intake.sync(zd, 987654) == "busy"  # no such intake


@needs_db
def test_sync_closed_ticket(client, zd, tech, intake_on, public):
    url = _linked(client, zd)
    zd.status = "closed"
    assert _submit(public, url, photos=[_file("a.jpg", _img())]).status_code == 303
    assert b"Thanks, we have it." in public.get(url).data
    row = intake.for_ticket(500)
    assert row["status"] == "sync_failed" and row["next_attempt_at"] is None
    assert "closed" in row["last_error"] and row["file_count"] == 1
    assert len(zd.updates) == 1 and zd.uploaded == {}  # nothing uploaded to a ticket that can't take it
    assert intake.sweep(zd) == {}  # no automatic retry
    page = client.get("/queue").get_data(as_text=True)
    assert "Customer intake forms that need a look" in page and "/t/500" in page and "Retry all" in page
    assert "form sync failed" in page
    assert b"The portal has stopped trying" in client.get("/t/500").data

    # Staff reopen the ticket (a follow-up in real life) and press Retry all.
    zd.status = "open"
    resp = client.post("/intake/retry-failed", follow_redirects=True)
    assert b"1 written to Zendesk" in resp.data
    assert intake.for_ticket(500)["status"] == "synced"
    assert b"Customer intake forms that need a look" not in client.get("/queue").data


@needs_db
def test_retry_buttons_need_a_tech(client, zd, tech, intake_on, public):
    _failed_submit(client, zd, public)
    sent = len(zd.updates)
    assert public.post("/t/500/intake/retry").status_code == 302
    assert public.post("/intake/retry-failed").status_code == 302
    assert len(zd.updates) == sent and intake.for_ticket(500)["status"] == "sync_failed"


@needs_db
def test_sweep_expires(client, zd, tech, intake_on, public):
    url = _linked(client, zd)
    zd.tags.clear()  # the fake keeps one tag list for every ticket
    _add(client, 501)  # a second, still-valid link is left alone
    _sql("UPDATE repair.intake SET expires_at = now() - interval '1 hour' WHERE ticket_id = 500")
    assert intake.sweep(zd) == {"expired": 1}
    assert intake.for_ticket(500)["status"] == "expired" and intake.for_ticket(501)["status"] == "link_sent"
    resp = public.get(url)
    assert resp.status_code == 410 and b"This link is no longer active" in resp.data
    assert _submit(public, url).status_code == 410
    assert intake.for_ticket(500)["answers"] == {} and len(zd.updates) == 2
    page = client.get("/t/500").data
    assert b"before the customer answered" in page and b"Send a new link" in page

    # Staff can send a new one.
    client.post("/t/500/intake/send")
    assert intake.for_ticket(500)["status"] == "link_sent"
    assert public.get(f"/repair/intake/{_link_token(zd)}").status_code == 200
    assert public.get(url).status_code == 404


@needs_db
def test_reminder(client, zd, tech, intake_on, monkeypatch):
    _add(client)
    _sql("UPDATE repair.intake SET link_sent_at = now() - interval '49 hours'")
    assert intake.sweep(zd) == {} and len(zd.updates) == 1  # off unless the hours are set
    monkeypatch.setenv("REPAIR_INTAKE_REMINDER_HOURS", "72")
    assert intake.sweep(zd) == {}  # not old enough yet
    monkeypatch.setenv("REPAIR_INTAKE_REMINDER_HOURS", "48")
    assert intake.sweep(zd) == {"reminded": 1}
    assert intake.sweep(zd) == {}  # once only
    assert len(zd.updates) == 2
    u = zd.updates[1]
    assert u["public"] is True and u["tags"] == ["repair-portal"]
    assert u["body"].startswith("Hi Jane,") and "reminder" in u["body"]
    assert "http" not in u["body"] and "/repair/intake/" not in u["body"] and "{" not in u["body"]
    row = intake.for_ticket(500)
    assert row["reminder_sent_at"] is not None and row["status"] == "link_sent"
    assert repair_db.events_for(500)[0]["label"] == "Intake form reminder sent"


@needs_db
def test_no_reminder_once_answered_or_ticket_solved(client, zd, tech, intake_on, public, monkeypatch):
    monkeypatch.setenv("REPAIR_INTAKE_REMINDER_HOURS", "48")
    url = _linked(client, zd)
    zd.tags.clear()  # the fake keeps one tag list for every ticket
    _add(client, 501)
    assert intake.for_ticket(501)["status"] == "link_sent"
    _sql("UPDATE repair.intake SET link_sent_at = now() - interval '49 hours'")
    assert _submit(public, url).status_code == 303  # 500 has answered
    sent = len(zd.updates)
    zd.status = "solved"  # and 501 was solved meanwhile
    assert intake.sweep(zd) == {} and len(zd.updates) == sent
    zd.status = "open"
    assert intake.sweep(zd) == {} and len(zd.updates) == sent  # a skipped reminder is not sent later


@needs_db
def test_rate_limit(client, zd, tech, intake_on, public, monkeypatch):
    url = _linked(client, zd)
    monkeypatch.setattr(intake_views, "LIMIT_TOKEN_POSTS", 3)
    for _ in range(3):
        assert public.post(url, data={"model": "x"}).status_code == 422
    resp = _submit(public, url)
    assert resp.status_code == 429 and b"Please wait a few minutes" in resp.data
    assert resp.headers["Cache-Control"] == "no-store"
    assert intake.for_ticket(500)["status"] == "link_sent"  # the limited post was not processed
    assert public.get(url).status_code == 200  # reading the form is still fine

    # Guessing links is slowed down; a real link is never locked out by it,
    # even though every customer shares the proxy's address.
    monkeypatch.setattr(intake_views, "LIMIT_ADDR", 2)
    monkeypatch.setattr(intake_views, "_hits", {})
    guess = "/repair/intake/" + "B" * 43
    assert [public.get(guess).status_code for _ in range(3)] == [404, 404, 429]
    assert public.get("/repair/intake/nope").status_code == 429
    assert [public.get(url).status_code for _ in range(5)] == [200] * 5
    assert client.get("/queue").status_code == 200  # staff pages are not limited


@needs_db
def test_public_form_needs_no_cookie(client, zd, tech, intake_on, public):
    """The link is the customer's key. A mail-app browser that keeps no cookies can still send the form."""
    url = _linked(client, zd)
    flask_app.config["CSRF_CHECK"] = True
    try:
        cold = _browser()
        assert _submit(cold, url, serial="").status_code == 422
        assert _submit(cold, url).status_code == 303
        assert intake.for_ticket(500)["status"] == "synced"

        # Staff posts are still checked, including the intake buttons.
        for path in ("/tech/signout", "/t/500/intake/send", "/t/500/intake/retry", "/intake/retry-failed"):
            plain = public.post(path)
            assert plain.status_code == 400 and b"Tell us about your rifle" not in plain.data, path
    finally:
        flask_app.config["CSRF_CHECK"] = False


@needs_db
def test_oversize_post_friendly(client, zd, tech, intake_on, public):
    url = _linked(client, zd)
    resp = public.post(url, data=b"x", content_type="multipart/form-data; boundary=x",
                       environ_overrides={"CONTENT_LENGTH": str(intake.MAX_POST_BYTES + 1)})
    assert resp.status_code == 413
    page = resp.get_data(as_text=True)
    assert "Those files are too big to send together" in page and "reply to the email" in page
    assert "120 MB max" not in page  # not the staff wording
    assert intake.for_ticket(500)["status"] == "link_sent"
    # Staff uploads keep their own page.
    assert intake.MAX_POST_BYTES < flask_app.config["MAX_CONTENT_LENGTH"]


@needs_db
def test_post_to_dead_link_is_refused_before_the_body(client, zd, tech, intake_on, public):
    _linked(client, zd)
    flask_app.config["CSRF_CHECK"] = True  # the guard answers before the CSRF check reads the body
    try:
        resp = _submit(public, "/repair/intake/" + "B" * 43)
        assert resp.status_code == 404 and b"This link is no longer active" in resp.data
        assert _submit(public, "/repair/intake/short").status_code == 404
    finally:
        flask_app.config["CSRF_CHECK"] = False
    assert intake.for_ticket(500)["status"] == "link_sent"


@needs_db
def test_staff_views(client, zd, tech, intake_on, public):
    url = _linked(client, zd)
    assert b"form sent" in client.get("/queue").data
    page = client.get("/t/500").get_data(as_text=True)
    assert "Customer intake form" in page and "Waiting for the customer." in page
    assert "Send a new link" in page and "Retry Zendesk" not in page
    assert _link_token(zd) not in page  # the link itself is never shown to staff

    assert _submit(public, url, photos=[_file("a.jpg", _img())]).status_code == 303
    board = client.get("/queue").get_data(as_text=True)
    assert "form in" in board and "form sent" not in board
    assert "Customer intake forms that need a look" not in board
    page = client.get("/t/500").get_data(as_text=True)
    assert "The answers are on the Zendesk ticket" in page
    for shown in ("Accuracy, Stock or chassis", "Please check the crown", "Groups opened up to 3 MOA."):
        assert shown in page, shown
    assert "Send link" not in page and "Send a new link" not in page and "Retry Zendesk" not in page
    assert "photo-1.jpg" in page  # the customer's photo shows with the ticket's other files

    # Not signed in: the panel is there, the answers are not.
    resp = public.get("/t/500")
    page = resp.get_data(as_text=True)
    assert resp.status_code == 200 and "Customer intake form" in page and "Sign in to see the answers." in page
    for hidden in ("Accuracy, Stock or chassis", "Please check the crown"):
        assert hidden not in page, hidden
    assert public.get("/queue").status_code == 200


@needs_db
def test_staff_pages_render_without_an_intake_row(client, zd, tech, intake_on, monkeypatch):
    from tests import demo_history

    assert demo_history.seed(TEST_DSN) == 150
    assert intake.for_ticket(6001) is None and intake.needs_attention() == []
    resp = client.get("/t/6001")
    assert resp.status_code == 200 and b"No link has been sent for this ticket." in resp.data
    assert client.get("/queue").status_code == 200
    monkeypatch.setenv("REPAIR_INTAKE_ENABLED", "0")
    resp = client.get("/t/6001")
    assert resp.status_code == 200 and b"Customer intake form" not in resp.data
    assert client.get("/queue").status_code == 200 and client.get("/t/6001/report").status_code == 200


def test_intake_helpers(monkeypatch):
    raw, hashed = intake.new_token()
    assert intake.TOKEN_RE.match(raw) and hashed == hashlib.sha256(raw.encode()).hexdigest()
    assert intake.new_token()[0] != raw

    monkeypatch.setenv("REPAIR_INTAKE_GO_LIVE_AT", "2026-10-01")
    cutoff, ok = intake.go_live_at()
    assert ok and cutoff == datetime(2026, 10, 1, tzinfo=timezone.utc)  # a bare date is UTC
    monkeypatch.setenv("REPAIR_INTAKE_GO_LIVE_AT", "soon")
    assert intake.go_live_at() == (None, False)
    monkeypatch.delenv("REPAIR_INTAKE_GO_LIVE_AT")
    assert intake.go_live_at() == (None, True)

    for value, on in (("1", True), ("TRUE", True), ("yes", True), ("on", True), ("0", False), ("", False)):
        monkeypatch.setenv("REPAIR_INTAKE_ENABLED", value)
        assert intake.enabled() is on

    answers, errors = intake.validate({"model": "  Ridgeline \n FFT ", "issue": " a\r\nb\x00 ", "serial": "S" * 500})
    assert answers["model"] == "Ridgeline FFT" and answers["issue"] == "a\nb" and len(answers["serial"]) == 120
    assert set(errors) == {"caliber"}
    assert intake.pick_concerns(["Feeding", "<b>", "Accuracy"], ["Accuracy", "Feeding", "Other"]) == ["Accuracy", "Feeding"]
    assert set(answers) == {name for name, *_rest in intake.FIELDS}

    text = intake.comment_text({"id": 7, "answers": {"model": "Ridgeline", "issue": "a\nb"}})
    assert "Model: Ridgeline" in text and text.endswith("Intake form reference RI-7")


@needs_db
def test_form_asks_for_no_contact_details(client, zd, tech, intake_on, public):
    """Name, address and phone stay with customer service: the form never asks, shows or sends them."""
    url = _linked(client, zd)
    page = public.get(url).get_data(as_text=True)
    for field in ("ship_", "name=\"phone\"", "dealer", 'type="tel"', 'type="email"', "Jane Hunter", "jane@example.com"):
        assert field not in page, field
    assert _submit(public, url, ship_street="12 Elk Ridge Rd", phone="435-555-0100", dealer="Scheels").status_code == 303
    row = intake.for_ticket(500)
    assert set(row["answers"]) == {name for name, *_rest in intake.FIELDS} | {"concerns", "photo_count"}
    html = zd.updates[-1]["html_body"]
    for private in ("12 Elk Ridge Rd", "435-555-0100", "Scheels"):
        assert private not in html and private not in str(row["answers"])


@needs_db
def test_concern_tick_boxes(client, zd, tech, intake_on, public):
    url = _linked(client, zd)
    page = public.get(url).get_data(as_text=True)
    for label in ("Accuracy", "Pressure signs", "Feeding", "Ejection or extraction", "Stock or chassis"):
        assert f'name="concerns" value="{label}"' in page
    assert " checked" not in page

    assert _submit(public, url, concerns=["Stock or chassis", "Accuracy", "<script>"]).status_code == 303
    row = intake.for_ticket(500)
    assert row["answers"]["concerns"] == ["Accuracy", "Stock or chassis"]  # list order, unknown values dropped
    html = zd.updates[-1]["html_body"]
    assert "<strong>Concerns:</strong> Accuracy, Stock or chassis" in html
    assert html.index("Concerns:") < html.index("Model:")  # first thing the agent reads
    assert "Accuracy, Stock or chassis" in client.get("/t/500").get_data(as_text=True)

    # The list is the editable one under Settings; none ticked is fine.
    assert 'Customer concerns' in client.get("/admin/").get_data(as_text=True)


@needs_db
def test_refused_resend_keeps_the_customers_link(client, zd, tech, intake_on, public):
    """Zendesk said no, so no new email exists: the link the customer has must keep working."""
    url = _linked(client, zd)
    before = intake.for_ticket(500)["token_hash"]
    zd.fail, zd.fail_status = True, 422
    resp = client.post("/t/500/intake/send", follow_redirects=True)
    zd.fail = False
    row = intake.for_ticket(500)
    assert row["status"] == "link_sent" and row["token_hash"] == before and row["last_error"]
    assert len(zd.updates) == 1 and public.get(url).status_code == 200
    # The next press works and replaces the link as usual.
    client.post("/t/500/intake/send")
    assert intake.for_ticket(500)["token_hash"] != before and public.get(url).status_code == 404


@needs_db
def test_confirmation_keeps_the_photo_count(client, zd, tech, intake_on, public):
    url = _linked(client, zd)
    assert _submit(public, url, photos=[_file("a.jpg", _img()), _file("b.jpg", _img())]).status_code == 303
    assert intake.for_ticket(500)["status"] == "synced"
    assert "2 attached" in public.get(url).get_data(as_text=True)
    assert "photo_count" not in zd.updates[-1]["html_body"]


@needs_db
def test_public_only_mode_serves_nothing_but_the_form(client, zd, tech, intake_on, public, monkeypatch):
    """repairs.example.com: the internet-facing copy shows the customer form and nothing else."""
    url = _linked(client, zd)
    monkeypatch.setenv("PORTAL_PUBLIC_ONLY", "1")
    for path in ("/", "/queue", "/incoming", "/scan", "/t/500", "/ticket/500", "/t/500/report", "/report/",
                 "/final-reports", "/ad/", "/admin/", "/tech", "/lookup?ticket=500", "/repair/intake"):
        for who in (public, client):  # even a signed-in tech's cookie opens nothing here
            resp = who.get(path)
            assert resp.status_code == 404 and b"Repair portal" not in resp.data, path
    for path in ("/t/500/add", "/t/500/intake/send", "/t/500/intake/retry", "/intake/retry-failed",
                 "/tech", "/admin/choice", "/t/500/stage/received"):
        assert client.post(path, data={"pin": "1234"}).status_code == 404, path
    assert len(zd.updates) == 1

    assert public.get("/healthz").status_code == 200
    assert public.get("/static/portal.css").status_code == 200
    assert public.get(url).status_code == 200
    assert _submit(public, url).status_code == 303
    assert intake.for_ticket(500)["status"] == "synced"

    monkeypatch.delenv("PORTAL_PUBLIC_ONLY")
    assert client.get("/queue").status_code == 200


@needs_db
def test_replaced_link_cannot_submit_even_mid_upload(client, zd, tech, intake_on, public):
    """The post was accepted on the old link, then staff replaced it before the answers were saved."""
    old_url = _linked(client, zd)
    row = intake.find_by_token(old_url.rsplit("/", 1)[1])  # what guard_request resolved before the upload
    client.post("/t/500/intake/send")                      # staff send a new link meanwhile
    assert intake.save_submission(row["id"], dict(ANSWERS), [], token_hash=row["token_hash"]) is False
    now = intake.for_ticket(500)
    assert now["status"] == "link_sent" and not now["answers"]
    new_url = "/repair/intake/" + _link_token(zd)
    assert new_url != old_url and _submit(public, new_url).status_code == 303
    assert intake.for_ticket(500)["status"] == "synced"


@needs_db
def test_link_retry_saves_its_hash_before_posting(client, zd, tech, intake_on, public, monkeypatch):
    """If the worker dies after Zendesk took the retried link, the stored hash must be the emailed one."""
    zd.fail, zd.fail_status = True, 503
    _add(client)
    zd.fail = False
    assert intake.for_ticket(500)["status"] == "pending_link"
    _sql("UPDATE repair.intake SET next_attempt_at = now() - interval '1 minute'")

    posted = {}
    real_post = zd.post_update

    def post_then_die(ticket_id, body, **kw):
        real_post(ticket_id, body, **kw)  # Zendesk has the comment and the tag...
        posted["token"] = re.search(r"/repair/intake/([A-Za-z0-9_-]{43})", body).group(1)
        raise KeyboardInterrupt  # ...and the process is killed before it can record that

    monkeypatch.setattr(zd, "post_update", post_then_die)
    ticket, requester, _org = zd.get_ticket(500)
    with pytest.raises(KeyboardInterrupt):
        intake.resend_link(zd, ticket, requester)
    monkeypatch.undo()
    monkeypatch.setenv("REPAIR_INTAKE_ENABLED", "1")
    monkeypatch.setenv("REPAIR_INTAKE_BASE_URL", "https://repairs.example.com")

    row = intake.for_ticket(500)
    assert row["status"] == "pending_link" and row["token_hash"] == intake.hash_token(posted["token"])
    _sql("UPDATE repair.intake SET next_attempt_at = now() - interval '1 minute'")
    sent = len(zd.updates)
    assert intake.sweep(zd).get("links") == 1 and len(zd.updates) == sent  # recovered, no second email
    assert intake.for_ticket(500)["status"] == "link_sent"
    assert public.get("/repair/intake/" + posted["token"]).status_code == 200  # the emailed link works


@needs_db
def test_staff_resend_that_dies_does_not_bless_an_unsent_link(client, zd, tech, intake_on, public, monkeypatch):
    """First link landed (response lost). A staff resend dies before posting. The sweep must not
    then mark the never-emailed replacement as sent."""
    real_post = zd.post_update

    def lands_but_times_out(ticket_id, body, **kw):
        real_post(ticket_id, body, **kw)
        raise ZendeskError("timed out")

    monkeypatch.setattr(zd, "post_update", lands_but_times_out)
    _add(client)
    assert intake.for_ticket(500)["status"] == "pending_link" and "repair_intake_sent" in zd.tags

    def dies(*_a, **_kw):
        raise KeyboardInterrupt

    monkeypatch.setattr(zd, "post_update", dies)
    ticket, requester, _org = zd.get_ticket(500)
    with pytest.raises(KeyboardInterrupt):
        intake.resend_link(zd, ticket, requester, tech, manual=True)
    monkeypatch.setattr(zd, "post_update", real_post)

    row = intake.for_ticket(500)
    assert row["status"] == "pending_link" and row["next_attempt_at"] is None
    assert intake.sweep(zd, force=True).get("links") is None
    assert intake.for_ticket(500)["status"] == "pending_link"  # still shown to staff as not sent
    assert any(r["ticket_id"] == 500 for r in intake.needs_attention()) or row["last_error"] is None


@needs_db
def test_resend_while_the_customer_submits_keeps_their_answers(client, zd, tech, intake_on, public, monkeypatch):
    url = _linked(client, zd)
    row = intake.find_by_token(url.rsplit("/", 1)[1])
    real_post = zd.post_update
    state = {"done": False}

    def customer_submits_first(ticket_id, body, **kw):
        if not state["done"]:
            state["done"] = True
            assert intake.save_submission(row["id"], dict(ANSWERS), [], token_hash=row["token_hash"])
        return real_post(ticket_id, body, **kw)

    monkeypatch.setattr(zd, "post_update", customer_submits_first)
    ticket, requester, _org = zd.get_ticket(500)
    assert intake.resend_link(zd, ticket, requester, tech, manual=True) == "submitted"
    now = intake.for_ticket(500)
    assert now["status"] == "submitted" and now["answers"]["serial"] == "CA12345"
    assert now["token_hash"] == row["token_hash"]
    labels = [e["label"] for e in repair_db.events_for(500)]
    assert "New intake form link sent" not in labels
