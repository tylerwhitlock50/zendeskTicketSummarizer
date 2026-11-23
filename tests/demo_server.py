"""Run the portal locally with a FAKE Zendesk, for trying the screens safely.

Nothing is sent to Zendesk or any customer. Needs an empty throwaway Postgres
database in DEMO_DATABASE_URL (it is migrated, and seeded with one person:
"Demo Tech", PIN 1234).

    $env:DEMO_DATABASE_URL = "postgresql://postgres:portal_dev@localhost:5434/portal_demo"
    venv\\Scripts\\python tests\\demo_server.py
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

from migrations import migrate  # noqa: E402

if migrate.main() != 0:
    sys.exit("migrations failed")

import app as app_module  # noqa: E402
import portal_views  # noqa: E402
import repair_db  # noqa: E402

SUBJECTS = {
    7101: ("Ridgeline won't group under 2 MOA", "Sam Ortiz"),
    7102: ("Bolt sticking on extraction", "Lee Park"),
    7103: ("Trigger creep after 300 rounds", "Dana Wells"),
    7104: ("Stock finish peeling", "Chris Moore"),
}


class DemoZendesk:
    """Answers like Zendesk; logs posts to the console instead of sending them."""

    def get_ticket(self, ticket_id):
        subject, name = SUBJECTS.get(ticket_id, (f"Repair request {ticket_id}", "Demo Customer"))
        ticket = {"id": ticket_id, "subject": subject, "status": "open",
                  "updated_at": "2026-11-23T16:00:00Z", "created_at": "2026-11-18T16:00:00Z",
                  "custom_fields": [], "ticket_form_id": None}
        return ticket, {"id": 1, "name": name, "email": "customer@example.com"}, None

    def post_update(self, ticket_id, body, public=True, tags=None, custom_fields=None):
        print(f"[demo] ticket {ticket_id} {'PUBLIC' if public else 'internal'} tags={tags} "
              f"fields={custom_fields}\n        {body}", flush=True)

    def add_tags(self, ticket_id, tags):
        print(f"[demo] ticket {ticket_id} add tags {tags} (no email)", flush=True)

    def search_export(self, query, max_pages=20):
        for tid, (subject, _) in SUBJECTS.items():
            yield {"id": tid, "subject": subject, "status": "open", "created_at": "2026-11-18T16:00:00Z"}

    def get_comments(self, ticket_id):
        return [], {}

    def get_ticket_fields(self):
        return {}

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

if __name__ == "__main__":
    app_module.app.run(port=5055, debug=False)
