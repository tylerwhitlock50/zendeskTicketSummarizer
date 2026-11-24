"""Run the portal locally with a FAKE Zendesk, for trying the screens safely.

Nothing is sent to Zendesk or any customer. Needs an empty throwaway Postgres
database in DEMO_DATABASE_URL (it is migrated, and seeded with one person:
"Demo Tech", PIN 1234).

    $env:DEMO_DATABASE_URL = "postgresql://postgres:portal_dev@localhost:5434/portal_demo"
    venv\\Scripts\\python tests\\demo_server.py

Optional settings:
    DEMO_PASSWORD   ask every visitor for this password first (use it on any server
                    others can reach; run_demo.sh sets it for you)
    DEMO_HOST       address to listen on, default 127.0.0.1 (0.0.0.0 inside Docker)
    DEMO_PORT       port, default 5055
    DEMO_SAMPLE_HISTORY  0 to start with an empty repair history; by default an empty
                    database gets ~4 months of made-up RMAs (tests/demo_history.py)
    DEMO_BEHIND_PROXY  1 when an HTTPS proxy (Caddy) is in front; see run_demo.sh
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

DSN = os.environ.get("DEMO_DATABASE_URL", "")
if not DSN:
    sys.exit("Set DEMO_DATABASE_URL to an empty throwaway database.")
os.environ["DATABASE_URL"] = DSN
os.environ["RTM_DATABASE_URL"] = ""
os.environ["ZENDESK_SUBDOMAIN"] = "example"
os.environ["ZENDESK_EMAIL"] = "demo@example.com"
os.environ["ZENDESK_API_TOKEN"] = "demo"
os.environ["OPENAI_API_KEY"] = "demo-disabled"  # the AI summary is switched off below


def _wait_for_database(dsn, seconds=60):
    """In Docker the database container may still be starting."""
    import time

    import psycopg

    deadline = time.monotonic() + seconds
    while True:
        try:
            psycopg.connect(dsn, connect_timeout=3).close()
            return
        except psycopg.OperationalError:
            if time.monotonic() > deadline:
                raise
            print("waiting for the database...", flush=True)
            time.sleep(2)


_wait_for_database(DSN)

from migrations import migrate  # noqa: E402

if migrate.main() != 0:
    sys.exit("migrations failed")

import app as app_module  # noqa: E402
import portal_views  # noqa: E402
import repair_db  # noqa: E402
from tests import demo_history  # noqa: E402

SUBJECTS = {
    7101: ("Ridgeline won't group under 2 MOA", "Sam Ortiz",
           "Since my last elk hunt the rifle opens up to 2.5-3 MOA with the same factory ammo that used to "
           "shoot under 1 MOA. Scope was checked by the shop and is fine. Bedding may have shifted?"),
    7102: ("Bolt sticking on extraction", "Lee Park",
           "The bolt is very stiff to lift after firing, and twice a case stayed in the chamber. "
           "Happens with every brand of ammo I tried."),
    7103: ("Trigger creep after 300 rounds", "Dana Wells",
           "Trigger used to break clean at about 3 lb. Now it creeps and feels closer to 5 lb."),
    7104: ("Stock finish peeling", "Chris Moore",
           "The finish on the stock is peeling near the grip after one season. Rifle shoots fine."),
}


# Zendesk ticket fields the demo tickets carry (what CS fills in on the RMA form).
DEMO_FIELDS = {
    9001: {"id": 9001, "title": "Rifle model", "type": "tagger", "active": True,
           "custom_field_options": [{"name": "Ridgeline FFT", "value": "ridgeline_fft"},
                                    {"name": "Mesa", "value": "mesa"},
                                    {"name": "MPR", "value": "mpr"},
                                    {"name": "Traverse", "value": "traverse"},
                                    {"name": "Evoke", "value": "evoke"}]},
    9002: {"id": 9002, "title": "Caliber", "type": "text", "active": True},
    9003: {"id": 9003, "title": "Serial number", "type": "text", "active": True},
    9004: {"id": 9004, "title": "Inbound tracking number", "type": "text", "active": True},
    9005: {"id": 9005, "title": "Purchase date", "type": "date", "active": True},
    9006: {"id": 9006, "title": "Dealer", "type": "text", "active": True},
}
DEMO_VALUES = {
    7101: ("ridgeline_fft", "6.5 Creedmoor", "CA10042", "1Z999AA10123456784", "2024-09-14", "Sportsman's Outpost"),
    7102: ("mesa", "7mm PRC", "CA20817", "776543219876", "2025-03-02", "High Country Arms"),
    7103: ("mpr", "6.5 PRC", "CA31155", "9400111899223344556677", "2023-11-20", "Online"),
    7104: ("traverse", ".300 Win Mag", "CA40990", None, "2025-06-30", "Big Game Supply"),
}


SAMPLE = demo_history.sample_tickets()
_MODEL_VALUE = {o["name"]: o["value"] for o in DEMO_FIELDS[9001]["custom_field_options"]}
for _tid, _t in SAMPLE.items():
    DEMO_VALUES[_tid] = (_MODEL_VALUE.get(_t["model"]), _t["caliber"], _t["serial"], _t["inbound"], None, None)


class DemoZendesk:
    """Answers like Zendesk; logs posts to the console instead of sending them.

    Files attached in the demo are kept in memory so the portal's gallery works.
    """

    def __init__(self):
        self.uploads = {}   # token -> (name, data, content_type)
        self.comments = {}  # ticket_id -> [comment]
        self.next_id = 50000

    def get_ticket(self, ticket_id):
        if ticket_id in SAMPLE:
            t = SAMPLE[ticket_id]
            subject, name, complaint = t["subject"], t["name"], t["complaint"]
        else:
            subject, name, complaint = SUBJECTS.get(
                ticket_id, (f"Repair request {ticket_id}", "Demo Customer", "Please take a look at my rifle.")
            )
        values = DEMO_VALUES.get(ticket_id, (None,) * 6)
        custom = [{"id": fid, "value": v} for fid, v in zip(sorted(DEMO_FIELDS), values)]
        ticket = {"id": ticket_id, "subject": subject, "status": "open", "description": complaint,
                  "updated_at": "2026-11-23T16:00:00Z", "created_at": "2026-11-18T16:00:00Z",
                  "custom_fields": custom, "ticket_form_id": None}
        return ticket, {"id": 1, "name": name, "email": "customer@example.com"}, None

    def upload_file(self, filename, data, content_type):
        token = f"demo-{len(self.uploads) + 1}"
        self.uploads[token] = (filename, data, content_type)
        return token

    def post_update(self, ticket_id, body, public=True, tags=None, custom_fields=None, uploads=None):
        attachments = []
        for tok in uploads or []:
            name, data, ctype = self.uploads[tok]
            self.next_id += 1
            attachments.append({"id": self.next_id, "file_name": name, "content_type": ctype, "size": len(data),
                                "content_url": f"demo://{tok}", "thumbnails": []})
        self.comments.setdefault(ticket_id, []).append(
            {"author_id": 2, "public": public, "created_at": "2026-11-23T20:00:00Z", "attachments": attachments}
        )
        files = f" files={[a['file_name'] for a in attachments]}" if attachments else ""
        print(f"[demo] ticket {ticket_id} {'PUBLIC' if public else 'internal'} tags={tags} "
              f"fields={custom_fields}{files}\n        {body}", flush=True)

    def download(self, content_url, max_bytes=None):
        name, data, ctype = self.uploads[content_url.split("demo://", 1)[1]]
        return data, ctype

    def add_tags(self, ticket_id, tags):
        print(f"[demo] ticket {ticket_id} add tags {tags} (no email)", flush=True)

    def search_export(self, query, max_pages=20):
        for tid, (subject, _name, _c) in SUBJECTS.items():
            yield {"id": tid, "subject": subject, "status": "open", "created_at": "2026-11-18T16:00:00Z"}

    def get_comments(self, ticket_id):
        return list(self.comments.get(ticket_id, [])), {2: {"id": 2, "name": "Repair portal"}}

    def get_ticket_fields(self):
        return DEMO_FIELDS

    def get_ticket_form(self, form_id):
        return None


fake = DemoZendesk()
portal_views._get_zendesk = lambda: fake
app_module.get_client = lambda: fake


def _no_ai(*args, **kwargs):
    raise RuntimeError("off in demo mode")


app_module.summarize_ticket = _no_ai  # never send demo tickets to OpenAI

if not repair_db.list_techs(include_inactive=True):
    repair_db.create_tech("Demo Tech", "1234")

if os.environ.get("DEMO_SAMPLE_HISTORY", "1") != "0":
    added = demo_history.seed(DSN)
    if added:
        print(f"Added {added} sample RMAs (about {demo_history.DAYS} days of made-up history).", flush=True)



class PasswordGate:
    """Ask every visitor for DEMO_PASSWORD (browser sign-in box) before any page.

    Too many wrong tries from one address pause it for 10 minutes.
    """

    MAX_FAILS = 10
    LOCK_SECONDS = 600

    def __init__(self, wsgi_app, password):
        import threading

        self.wsgi_app = wsgi_app
        self.password = password.encode()
        self.fails = {}
        self.lock = threading.Lock()

    def __call__(self, environ, start_response):
        import base64
        import hmac
        import time

        client = environ.get("REMOTE_ADDR", "?")
        now = time.monotonic()
        with self.lock:
            recent = [t for t in self.fails.get(client, []) if now - t < self.LOCK_SECONDS]
            self.fails[client] = recent
        if len(recent) >= self.MAX_FAILS:
            start_response("429 Too Many Requests", [("Content-Type", "text/plain"), ("Retry-After", "600")])
            return [b"Too many wrong passwords. Try again in 10 minutes."]
        header = environ.get("HTTP_AUTHORIZATION", "")
        if header.startswith("Basic "):
            try:
                _user, _, given = base64.b64decode(header[6:]).decode("utf-8").partition(":")
            except Exception:
                given = ""
            if hmac.compare_digest(given.encode(), self.password):
                return self.wsgi_app(environ, start_response)
            with self.lock:
                self.fails.setdefault(client, []).append(now)
        start_response(
            "401 Unauthorized",
            [("Content-Type", "text/plain"), ("WWW-Authenticate", 'Basic realm="Repair portal demo"')],
        )
        return [b"Password required."]


if os.environ.get("DEMO_PASSWORD"):
    app_module.app.wsgi_app = PasswordGate(app_module.app.wsgi_app, os.environ["DEMO_PASSWORD"])
    print("Password gate is on: visitors are asked for DEMO_PASSWORD (any user name).", flush=True)

if os.environ.get("DEMO_BEHIND_PROXY") == "1":
    # Behind Caddy (HTTPS): take the visitor's address and https scheme from the
    # one proxy in front, so the password lockout is per visitor (not per proxy),
    # links/QR codes use https, and session cookies are only sent over HTTPS.
    # Only set this when the app is NOT reachable directly (run_demo.sh does so).
    from werkzeug.middleware.proxy_fix import ProxyFix

    app_module.app.wsgi_app = ProxyFix(app_module.app.wsgi_app, x_for=1, x_proto=1, x_host=1)
    app_module.app.config["SESSION_COOKIE_SECURE"] = True
    app_module.app.config["PREFERRED_URL_SCHEME"] = "https"
    print("Behind the HTTPS proxy: trusting its X-Forwarded-For/Proto; cookies are HTTPS-only.", flush=True)

if __name__ == "__main__":
    app_module.app.run(
        host=os.environ.get("DEMO_HOST", "127.0.0.1"),
        port=int(os.environ.get("DEMO_PORT", "5055")),
        debug=False,
        threaded=True,
    )
