# Zendesk Ticket Print + Repair Portal

A small Flask app for the repair shop. Zendesk keeps the customer conversation
and sends the emails; this app keeps the shop-side repair record.

1. **One-pager**: look up a Zendesk ticket, pull the conversation and custom
   fields, summarize with OpenAI, and print a one-page sheet that travels with
   the rifle. The sheet carries a **Code 128 barcode** (the bare ticket ID, for
   handheld scanners) and a **QR code** (opens the ticket in the portal on a phone).
2. **Incoming**: CS tags an approved RMA ticket `repair_process` in Zendesk.
   `/incoming` runs a Zendesk search for that tag and lists tickets not yet in
   the queue; adding one starts its repair record (nothing is sent to the customer).
3. **Stage screen**: scan the sheet to open `/t/<id>` and post a stage:
   **Received**, **Inspected** (problem drop-down + internal notes),
   **Repair complete** (solution drop-down + work done), **Shipped** (tracking
   number). Each stage is ONE Zendesk ticket update: a public comment, the tags
   `repair-portal` + `rp-<stage>`, and optionally an "RMA Stage" field. Zendesk's
   standard "Notify requester and CCs of comment update" trigger emails the customer.
4. **Repair record**: every add, stage, quick action and note is saved in
   Postgres (schema `repair`) with who and when, plus the form answers.
5. **Queue** (`/queue`): RMAs by stage. **Report** (`/report`): a weekly view of
   stage counts, turnaround, problems, repairs and work by person, with CSV
   downloads of RMAs and all activity.

People sign in on a shop device with their **name and PIN** (set up in
Settings). They don't need Zendesk seats: posts go through the API user.

## Setup

```powershell
python -m venv venv
venv\Scripts\pip install -r requirements.txt
copy .env.example .env   # then fill in your credentials
```

Values in `.env`:

| Variable | Description |
|---|---|
| `ZENDESK_SUBDOMAIN` | `yourcompany` for `https://yourcompany.zendesk.com` |
| `ZENDESK_EMAIL` | Email of the agent/admin the API token belongs to |
| `ZENDESK_API_TOKEN` | Admin Center → Apps and integrations → APIs → Zendesk API |
| `OPENAI_API_KEY` | OpenAI API key (for the one-pager summary) |
| `OPENAI_MODEL` | Optional, defaults to `gpt-4o-mini` |
| `DATABASE_URL` | Postgres for the repair record and settings. Unset → one-pager only. (`RTM_DATABASE_URL` still works as a legacy name.) |
| `PORTAL_BASE_URL` | Absolute URL of this app as reachable from a phone (for the printed QR code). Unset → the request host is used. |
| `PORTAL_TAG` | Base tag added on every portal post, default `repair-portal` |
| `INTAKE_TAG` | Tag that puts a ticket on the Incoming list, default `repair_process` |
| `ZENDESK_STAGE_FIELD_ID` | Optional id of a Zendesk "RMA Stage" drop-down field; each stage sets it to the option value configured in Settings |
| `ADMIN_PASSWORD` | Password for Settings. Unset → Settings are open to anyone on the network |
| `TECH_SESSION_HOURS` | How long a name + PIN sign-in lasts on a device, default 12 |
| `FLASK_SECRET_KEY` | Random string in production (sessions, sign-ins, CSRF) |

With `DATABASE_URL` set, create the tables (also run automatically on container
start):

```powershell
venv\Scripts\python migrations\migrate.py
```

Then open **Settings** (`/admin/`) and:

- add the people who post (name + 4–8 digit PIN);
- replace the starter **Problems** and **Solutions** lists with the Repair Center's;
- adjust each stage's customer message. Placeholders: `{problem}`, `{solution}`,
  `{work_done}`, `{tracking}`.

### Zendesk checklist before going live

1. Admin Center → Objects and rules → Triggers: confirm **Notify requester and
   CCs of comment update** is active and unmodified. It is what emails the customer.
2. Add the `repair_process` tag to the CS approval macro.
3. Keep RMA tickets out of **Closed** while in repair: Zendesk can't add comments
   to closed tickets. Use Pending or On-hold.
4. Optional: create an "RMA Stage" drop-down field, put its id in
   `ZENDESK_STAGE_FIELD_ID`, and enter each option's value on the stage in Settings.
5. Post one stage on a test ticket you own and confirm the email arrives.

## Run

```powershell
venv\Scripts\python app.py
```

- http://localhost:5000: ticket lookup → printable sheet
- http://localhost:5000/queue: repair queue board
- http://localhost:5000/incoming: tagged tickets waiting to be added
- http://localhost:5000/scan: scan station (scanner types the ID + Enter)
- http://localhost:5000/t/&lt;ticket_id&gt;: stage screen (what the QR opens)
- http://localhost:5000/report/: weekly repair report + CSV downloads
- http://localhost:5000/admin/: Settings

Keep the app on the shop network. Every form is CSRF-protected and posting
requires a name + PIN sign-in, but PINs are short by design.

### Try it without touching Zendesk

`tests/demo_server.py` runs the app against a fake Zendesk (posts are printed
to the console, never sent) and a throwaway database. It seeds "Demo Tech", PIN 1234.

```powershell
$env:DEMO_DATABASE_URL = "postgresql://postgres:portal_dev@localhost:5433/portal_demo"
venv\Scripts\python tests\demo_server.py   # http://localhost:5055
```

## Deploy with Docker

With a filled-in `.env` next to `docker-compose.yml`:

```bash
docker compose up -d --build
```

Dev overlay with a local Postgres (port 5433):

```bash
docker compose -f docker-compose.yml -f docker-compose.dev.yml up -d --build
```

The app is served by gunicorn on port 5000; migrations run on container start.
The `.env` file is injected at runtime by compose — credential changes only
need `docker compose restart`, not a rebuild.

## Tests

```powershell
$env:TEST_DATABASE_URL = "postgresql://postgres:portal_dev@localhost:5433/portal_test"
venv\Scripts\python -m pytest tests\
```

Zendesk is faked. The database tests need `TEST_DATABASE_URL` pointing at an
**empty throwaway** database: it is wiped and migrated at the start of the run.
Without it, only the database-free tests run.

## History

This app once contained a full RTM workflow tracker (branch `rtm-tracker`
era). It was replaced by the scan portal, and then by this repair queue, which
keeps Zendesk as the customer-facing system. Migrations `001`–`003` are kept so
existing databases stay consistent; the old `rtm` schema is left untouched.
