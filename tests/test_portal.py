"""Portal + one-pager tests with a faked Zendesk client and an in-memory DB layer."""

import os
import sys
from datetime import datetime, timezone

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("ZENDESK_SUBDOMAIN", "example")
os.environ.setdefault("ZENDESK_EMAIL", "test@example.com")
os.environ.setdefault("ZENDESK_API_TOKEN", "token")
os.environ.setdefault("OPENAI_API_KEY", "test")

from app import app as flask_app  # noqa: E402
import portal_views  # noqa: E402


class FakeZendesk:
    def __init__(self):
        self.comments = []
        self.tags = []

    def get_ticket(self, ticket_id):
        ticket = {
            "id": ticket_id,
            "subject": "Rifle will not group",
            "status": "open",
            "updated_at": "2026-11-20T10:00:00Z",
            "created_at": "2026-11-01T10:00:00Z",
            "custom_fields": [],
        }
        requester = {"id": 1, "name": "Jane Hunter", "email": "jane@example.com"}
        return ticket, requester, None

    def add_comment(self, ticket_id, body, public=True):
        self.comments.append({"ticket_id": ticket_id, "body": body, "public": public})

    def add_tags(self, ticket_id, tags):
        self.tags.append({"ticket_id": ticket_id, "tags": list(tags)})

    def search_export(self, query, max_pages=20):
        yield {
            "id": 101,
            "subject": "Tagged ticket",
            "status": "open",
            "updated_at": "2026-11-21T09:00:00Z",
            "tags": ["repair-portal", "rp-arrived"],
        }


class FakeDb:
    """Stands in for the db module: one quick action, log captured in memory."""

    ACTION = {
        "id": 1,
        "label": "Arrived",
        "comment_body": "Your firearm has arrived.",
        "tag": "rp-arrived",
        "position": 10,
        "active": True,
    }

    def __init__(self):
        self.log = []

    def available(self):
        return True

    def list_quick_actions(self, include_inactive=False):
        return [dict(self.ACTION)]

    def get_quick_action(self, action_id):
        return dict(self.ACTION) if action_id == 1 else None

    def recent_actions(self, ticket_id, limit=20):
        return [
            {
                "action_label": "Arrived",
                "note_excerpt": None,
                "tech_initials": "TW",
                "created_at": datetime(2026, 11, 20, 10, 0, tzinfo=timezone.utc),
            }
        ]

    def log_action(self, ticket_id, action_label, note_excerpt, tech_initials):
        self.log.append((ticket_id, action_label, note_excerpt, tech_initials))


@pytest.fixture
def fake_zd(monkeypatch):
    zd = FakeZendesk()
    monkeypatch.setattr(portal_views, "_get_zendesk", lambda: zd)
    return zd


@pytest.fixture
def fake_db(monkeypatch):
    fdb = FakeDb()
    monkeypatch.setattr(portal_views, "db", fdb)
    return fdb


@pytest.fixture
def client():
    flask_app.config["TESTING"] = True
    with flask_app.test_client() as c:
        yield c


def test_index_renders(client):
    resp = client.get("/")
    assert resp.status_code == 200
    assert b"Scan portal" in resp.data


def test_scan_go_parses_id_and_urls(client):
    resp = client.get("/scan/go?ticket_id=1234")
    assert resp.status_code == 302 and resp.headers["Location"].endswith("/t/1234")
    resp = client.get("/scan/go?ticket_id=https://example.zendesk.com/agent/tickets/987")
    assert resp.headers["Location"].endswith("/t/987")
    resp = client.get("/scan/go?ticket_id=not-a-ticket")
    assert "/scan" in resp.headers["Location"]


def test_action_page_renders(client, fake_zd, fake_db):
    resp = client.get("/t/1234")
    assert resp.status_code == 200
    assert b"Rifle will not group" in resp.data
    assert b"Arrived" in resp.data  # quick action button
    assert b"Custom note" in resp.data


def test_quick_action_posts_public_comment_and_tags(client, fake_zd, fake_db):
    resp = client.post("/t/1234/action", data={"action_id": "1", "initials": "tw"})
    assert resp.status_code == 302
    assert fake_zd.comments == [
        {"ticket_id": 1234, "body": "Your firearm has arrived.", "public": True}
    ]
    assert fake_zd.tags == [{"ticket_id": 1234, "tags": ["repair-portal", "rp-arrived"]}]
    assert fake_db.log == [(1234, "Arrived", None, "tw")]


def test_custom_note_defaults_internal(client, fake_zd, fake_db):
    client.post(
        "/t/55/action",
        data={"note": "Waiting on a replacement barrel", "note_visibility": "internal"},
    )
    assert fake_zd.comments[0]["public"] is False
    assert fake_zd.tags == [{"ticket_id": 55, "tags": ["repair-portal"]}]
    assert fake_db.log[0][1] == "Internal note"


def test_custom_note_public(client, fake_zd, fake_db):
    client.post("/t/55/action", data={"note": "Hello!", "note_visibility": "public"})
    assert fake_zd.comments[0]["public"] is True


def test_empty_post_is_rejected(client, fake_zd, fake_db):
    client.post("/t/55/action", data={})
    assert fake_zd.comments == [] and fake_zd.tags == []


def test_report_groups_by_tag(client, fake_zd, fake_db):
    resp = client.get("/report")
    assert resp.status_code == 200
    assert b"Tagged ticket" in resp.data
    assert b"Arrived" in resp.data


def test_ticket_sheet_includes_barcodes(client, fake_zd, monkeypatch):
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
