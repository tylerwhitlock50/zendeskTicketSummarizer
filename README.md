# Zendesk Ticket Print + Scan Portal

A small Flask app for the repair shop, with Zendesk as the system of record:

1. **One-pager** — look up a Zendesk ticket, pull the conversation and custom
   fields, summarize with OpenAI, and print a one-page sheet for the physical
   file. The sheet carries a **Code 128 barcode** (the bare ticket ID, for
   handheld scanners) and a **QR code** (opens the scan portal on a phone).
2. **Scan portal** — scan the barcode to open the ticket and press a quick
   action: each button posts a configurable **public comment** to the Zendesk
   ticket ("your firearm has arrived", "being worked on", …) and tags the
   ticket. A free-form note (internal or public) is also available.
3. **Tag report** — every portal touch adds a base tag (default
   `repair-portal`) plus a per-action tag (`rp-arrived`, `rp-in-work`, …), so
   `/report` can pull live from Zendesk and show where tagged tickets stand.

Workflow state (statuses, queues, assignments) lives in Zendesk, not here.

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
| `DATABASE_URL` | Postgres for the portal's quick-action config + audit log. Unset → one-pager only. (`RTM_DATABASE_URL` still works as a legacy name.) |
| `PORTAL_BASE_URL` | Absolute URL of this app as reachable from a phone (for the printed QR code). Unset → the request host is used. |
| `PORTAL_TAG` | Base tag added on every portal touch, default `repair-portal` |
| `FLASK_SECRET_KEY` | Random string in production (sessions + flash messages) |

With `DATABASE_URL` set, create the tables (also run automatically on container
start):

```powershell
venv\Scripts\python migrations\migrate.py
```

The portal seeds four starter actions (Arrived / Being worked on / Being tested
/ Awaiting processing). Edit them — labels, customer-facing comment text, tags,
order — at **`/admin/actions`**; no deploy needed.

## Run

```powershell
venv\Scripts\python app.py
```

- http://localhost:5000 — ticket lookup → printable sheet (**Print / Save as PDF**)
- http://localhost:5000/scan — scan station (scanner types the ID + Enter)
- http://localhost:5000/t/&lt;ticket_id&gt; — action page (what the QR opens)
- http://localhost:5000/report — live tag report from Zendesk
- http://localhost:5000/admin/actions — quick-action settings

Comments post as the Zendesk user who owns the API token. The portal records
optional tech initials in its local audit log only — they never appear in the
customer-facing comment. Note: `/admin/actions` has no login; keep the app on
the shop LAN or add auth before exposing it.

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
venv\Scripts\python -m pytest tests\
```

Zendesk and the database are faked; no network or Postgres needed.

## History

This app previously contained a full RTM (return-to-manufacturer) workflow
tracker (worklist, status board, intake, labor clocking, parts, reports). It
was removed in favor of letting Zendesk manage workflow — see git history
(branch `rtm-tracker` era) if you ever need it. Migrations `001`–`003` are kept
so existing databases stay consistent; the old `rtm` schema is left untouched.
