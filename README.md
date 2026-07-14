# Zendesk Ticket Print

A small Flask app that looks up a Zendesk ticket, pulls the full conversation and
custom field data, summarizes it with OpenAI, and renders a printable one-page
sheet with the issue summary, customer information, ticket ID, and product
information.

## Setup

```powershell
python -m venv venv
venv\Scripts\pip install -r requirements.txt
copy .env.example .env   # then fill in your credentials
```

Required values in `.env`:

| Variable | Description |
|---|---|
| `ZENDESK_SUBDOMAIN` | `yourcompany` for `https://yourcompany.zendesk.com` |
| `ZENDESK_EMAIL` | Email of the agent/admin the API token belongs to |
| `ZENDESK_API_TOKEN` | Admin Center → Apps and integrations → APIs → Zendesk API |
| `OPENAI_API_KEY` | OpenAI API key |
| `OPENAI_MODEL` | Optional, defaults to `gpt-4o-mini` |

## Run

```powershell
venv\Scripts\python app.py
```

Open http://localhost:5000, enter a ticket ID (or paste a ticket URL), and use
**Print / Save as PDF** on the sheet.

## How it works

- `zendesk_client.py` calls the Zendesk API: the ticket (with sideloaded
  requester + organization), all comments (paginated, with sideloaded authors),
  the ticket field definitions (to translate custom field IDs/tags into
  human-readable names via `custom_field_options`), and the ticket form (for
  field ordering). Field definitions are cached for 10 minutes.
- `summarizer.py` builds a plain-text transcript of the conversation (middle
  truncated past ~12k chars) and asks OpenAI (JSON mode) for an issue summary,
  key points, current status, and product context. If the OpenAI call fails,
  the sheet still renders with a notice.
- `app.py` orchestrates the calls and renders `templates/ticket_sheet.html`,
  styled by `static/print.css` for US Letter printing.
