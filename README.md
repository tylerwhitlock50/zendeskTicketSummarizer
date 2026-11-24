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
   With `REPAIR_INTAKE_ENABLED` on, adding a ticket also emails the customer a
   private link to the **intake form** (see "Customer intake form" below).
3. **Stage screen**: scan the sheet (or the rifle's serial, or the box's
   tracking label) to open `/t/<id>` and post a stage. Each form is a QC step:
   - **Received**: chamber/magazine safety check, serial scanned from the rifle
     and checked against the ticket, inbound tracking checked against the ticket,
     model / caliber / part number confirmed, accessories, arrival condition.
     A serial that doesn't match is saved as an internal note and flagged for CS
     (the customer is not emailed).
   - **Inspected**: inspection checklist, headspace, trigger pull as found,
     problems found, complaint match.
   - **Repair complete**: repairs, work done, parts replaced, root cause and
     coverage (warranty / customer pay / goodwill; internal), trigger pull after.
   - **Final test**: final safety and function check plus the accuracy test.
   - **Shipped**: the serial going in the box must match the rifle received,
     unloaded / no-ammo confirmation, return tracking (carrier detected). It is
     blocked until the final check passes unless a manager OKs "Ship anyway".

   Each stage is ONE Zendesk ticket update: a public comment, the tags
   `repair-portal` + `rp-<stage>`, and optionally an "RMA Stage" field. Zendesk's
   standard "Notify requester and CCs of comment update" trigger emails the customer.
4. **Repair record**: every add, stage, quick action and note is saved in
   Postgres (schema `repair`) with who and when, plus the form answers.
5. **Delivery**: shipped RMAs are tracked (EasyPost or Shippo, via the
   `delivery-checker` service). When the carrier shows the rifle delivered, a
   "delivered" public comment emails the customer, once. Without a tracking
   service, techs press **Mark delivered** on the ticket.
6. **Final reports** (`/final-reports`): every repair gets a customer-facing
   report drafted from its record (internal notes, root cause and coverage left
   out). Review it, edit if needed, and send: one public comment.
7. **Queue** (`/queue`): RMAs by stage. **Weekly report** (`/report`): stage
   counts, turnaround, problems, repairs, root causes, coverage, QC checks,
   deliveries and work by person, with CSV downloads of RMAs (including the
   ticket's product fields, for matching to products) and all activity.

8. **A&D log** (`/ad/`): the ATF acquisition & disposition record for every
   firearm that passes through the portal. **Received** opens an entry (date,
   serial, make/model/caliber, received from: the requester's name plus the
   ticket's address and dealer FFL fields); **Shipped** closes it (date, shipped
   to, serial at pack-out, tracking). Views: what's in the building now,
   acquisitions, dispositions, the bound book for a period, entries that need
   review (e.g. no address), and search by serial or ticket. Print or download CSV.
   Entries are never deleted: re-posted stages and the compliance officer's
   corrections (Settings password + name/PIN, with a reason) are kept with the
   old and new values. Moving a shipped RMA back to a repair stage clears the
   disposition (it never left); posting Received again opens a new entry.
   Map the **Customer address** and **Dealer FFL number** Zendesk fields in
   Settings, and set the manufacturer / firearm type written on new entries.
   Only rifles scanned through the portal are in this log.

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
| `TRACKING_PROVIDER` | `easypost` or `shippo` to detect deliveries automatically. Unset → "Mark delivered" by hand |
| `EASYPOST_API_KEY` / `SHIPPO_API_KEY` | The tracking service's API key |
| `REPAIR_INTAKE_ENABLED` | `1` / `true` / `yes` / `on` to email the customer the intake form link when a ticket is added to the queue. Default off |
| `REPAIR_INTAKE_GO_LIVE_AT` | Only tickets created in Zendesk on or after this date/time get a link, e.g. `2026-12-01T08:00:00Z` (no time zone = UTC). Unset → no cutoff. A value that can't be read → no links are sent |
| `REPAIR_INTAKE_BASE_URL` | Public address of the intake form, e.g. `https://repairs.yourcompany.com`. Unset → no link is sent (`PORTAL_BASE_URL` is the shop-network address and is never used for this) |
| `PORTAL_PUBLIC_ONLY` | `1` on the internet-facing copy only: it serves the customer intake form (`/repair/intake/*`) and nothing else. Unset on the staff portal |
| `ZENDESK_INTAKE_SERIAL_FIELD_ID` / `ZENDESK_INTAKE_MODEL_FIELD_ID` / `ZENDESK_INTAKE_CALIBER_FIELD_ID` | Optional ids of Zendesk text fields to fill from the customer's answers. Blank → that field is skipped |
| `REPAIR_INTAKE_REMINDER_HOURS` | Hours after the link before one reminder is sent (e.g. `48`). Default `0` = no reminder |

With `DATABASE_URL` set, create the tables (also run automatically on container
start):

```powershell
venv\Scripts\python migrations\migrate.py
```

Then open **Settings** (`/admin/`) and:

- add the people who post (name + 4–8 digit PIN);
- replace the starter **Problems**, **Solutions** and **Root causes** lists with the Repair Center's;
- check **Zendesk ticket fields**: which ticket fields hold the serial number,
  model, caliber, part number and inbound tracking (matched by title until you pick);
- adjust each stage's customer message, the **delivered** message and the
  **final report template** (placeholders are listed next to each).

### Zendesk checklist before going live

1. Admin Center → Objects and rules → Triggers: confirm **Notify requester and
   CCs of comment update** is active and unmodified. It is what emails the customer.
2. Add the `repair_process` tag to the CS approval macro.
3. Keep RMA tickets out of **Closed** while in repair: Zendesk can't add comments
   to closed tickets. Use Pending or On-hold.
4. Optional: create an "RMA Stage" drop-down field, put its id in
   `ZENDESK_STAGE_FIELD_ID`, and enter each option's value on the stage in Settings.
5. Post one stage on a test ticket you own and confirm the email arrives.

### Customer intake form

Off until `REPAIR_INTAKE_ENABLED` is set. When it is on:

- **Add to repair queue** posts one public comment on the ticket with a private
  link (`/repair/intake/<token>`) and the tag `repair_intake_sent`. Zendesk
  emails it to the customer. One link per ticket; it works for 30 days.
- The customer fills in the rifle details, what's going on, anything specific
  they want looked at, and up to 6 photos (20 MB each). Tick boxes for the
  common problems (accuracy, pressure signs, feeding, ...) make the ticket easy
  to sort; edit the list under Settings. The form asks for no name, address
  or phone number: contact details stay with customer service in Zendesk.
  No sign-in is needed: the link is the key.
- The answers are saved in the portal first, then added to the same ticket as
  a comment from the customer, with the photos and the tag
  `repair_intake_received`. If Zendesk refuses, the answers stay saved and the
  portal tries again.
- The ticket page has a **Customer intake form** panel: where the form stands,
  the answers (signed-in staff only), **Send link** / **Send a new link** and
  **Retry Zendesk**. A new link replaces the old one. The queue shows which
  tickets have a form out or in, and lists the ones that need a look, with
  **Retry all**.
- Tickets already in the queue when the form is switched on get no link unless
  a tech presses **Send link**. Solved and closed tickets never get one.
- The model, serial and caliber the customer types do not change the repair
  record or the A&D log. The tech still verifies them at **Received**.

Before turning it on:

1. Tell CS and the techs: **Add to repair queue** now emails the customer.
2. The form has to be reachable from the customer's phone, and the rest of the
   portal must not be. Run a second copy of the app for the internet with
   `PORTAL_PUBLIC_ONLY=1` (`docker-compose.public.yml`), pointed at the same
   database, e.g. on `repairs.yourcompany.com`. That copy serves the customer
   form and nothing else: every staff page answers 404. Set
   `REPAIR_INTAKE_BASE_URL` on the staff portal to its public https address.
   The staff portal itself has no login on its read pages, so keep it on the
   shop network or behind a VPN / access proxy, never open on the internet.
3. Zendesk must email the requester for the link comment: the standard
   comment-update trigger does it, or add a trigger on the tag
   `repair_intake_sent`. Add a test ticket you own and confirm the email arrives.
4. The answers are posted as the customer, so the API user must be allowed to
   set a comment's author. Fill in the form on the test
   ticket and confirm the comment and photos appear on it.
5. Keep the `delivery-checker` container running. It also retries links and
   answers that Zendesk did not take, every 30 minutes. Without it, retries
   happen only when someone presses the buttons.

## Run

```powershell
venv\Scripts\python app.py
```

- http://localhost:5000: ticket lookup → printable sheet
- http://localhost:5000/queue: repair queue board
- http://localhost:5000/incoming: tagged tickets waiting to be added
- http://localhost:5000/scan: scan station: the one-pager barcode, a rifle serial or a box's tracking label
- http://localhost:5000/final-reports: final repair reports to review and send
- http://localhost:5000/t/&lt;ticket_id&gt;: stage screen (what the QR opens)
- http://localhost:5000/report/: weekly repair report + CSV downloads
- http://localhost:5000/ad/: ATF A&D log (in the building, acquisitions, dispositions, bound book)
- http://localhost:5000/admin/: Settings

Keep the app on the shop network. Every form is CSRF-protected and posting
requires a name + PIN sign-in, but PINs are short by design.

### Try it without touching Zendesk

`tests/demo_server.py` runs the app against a fake Zendesk (posts are printed
to the console, never sent) and a throwaway database. It seeds "Demo Tech", PIN 1234,
and, on an empty database, about four months of made-up repair history (tickets 6001
and up, `tests/demo_history.py`) so the report, queue and final reports have something
to show. Tickets 7101-7104 are left for you to add from Incoming. Set
`DEMO_SAMPLE_HISTORY=0` to start empty. The customer intake form is on in the
demo: adding one of those tickets prints its link in the console; open it to
fill in the form as the customer would.

```powershell
$env:DEMO_DATABASE_URL = "postgresql://postgres:portal_dev@localhost:5433/portal_demo"
venv\Scripts\python tests\demo_server.py   # http://localhost:5055
```

To show it to people from a server (for example a throwaway DigitalOcean
droplet), clone the repo and run `run_demo.sh`. On a fresh Ubuntu droplet it:

- installs Docker and Compose v2 if they're missing;
- asks for a password that every visitor must enter (or generates one);
- turns on the firewall (SSH, 80 and 443 only) and fail2ban for SSH;
- starts the demo with its own throwaway database (never published; your real
  `.env` is never read) behind **HTTPS**: Caddy gets a real certificate for
  `<droplet-ip-with-dashes>.sslip.io`, a free name that points at the droplet;
- waits until the site answers, then prints the address.

```bash
sudo ./run_demo.sh            # start or restart; prints https://…sslip.io/incoming
sudo ./run_demo.sh status     # is it up?
sudo ./run_demo.sh logs       # follow the logs
sudo ./run_demo.sh stop       # stop; the demo database is deleted (certificate kept)
sudo ./run_demo.sh purge      # stop and delete the saved certificate too
```

As root you can set options in front of the command: your own domain with
`DEMO_DOMAIN=demo.example.com ./run_demo.sh` (point its DNS at the droplet
first), or plain HTTP with `DEMO_HTTPS=0 ./run_demo.sh`.

## Deploy with Docker

With a filled-in `.env` next to `docker-compose.yml`:

```bash
docker compose up -d --build
```

This also starts `delivery-checker`, which checks return tracking every 30
minutes (`python delivery.py --loop 1800`); that part idles until
`TRACKING_PROVIDER` is set. The same loop retries customer intake links and
answers that Zendesk did not take. Run one pass by hand with `python delivery.py`.

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
