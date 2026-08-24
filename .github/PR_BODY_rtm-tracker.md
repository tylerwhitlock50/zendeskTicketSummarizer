## Summary

Extends the Zendesk ticket-print app into an internal **RTM (rifle return/RMA) tracker**, per JP's directive to formalize RTM capture and monthly trend review. The existing ticket-print feature is unchanged.

- **Ticket-first intake** (CS receiving flow): enter a Zendesk ticket ID/URL → subject and requester shown, ticket linked, and the serial auto-filled from the ticket's serial custom field when present. Scanning/typing a serial then auto-fills rifle info (model, caliber, barrel length, build/ship date, original customer) from Infor VISUAL (read-only), plus prior-RTM history and repeat-return flag from Postgres. Manual-entry fallback when VISUAL is unreachable.
- **Workflow**: Received → In Inspection → In Repair → QC/Test → Ready → Shipped → Closed state machine (with no-fault skip and rework loop), full status history.
- **Status board** (`/rtm/board`): wall-display tracker with Received / In Process / Waiting / Ready / Shipped columns, auto-refreshing every 60s. Any RTM can be flagged "waiting on…" (parts, customer) from its detail page, which moves it to the Waiting column with the reason on the card.
- **Labor & parts**: per-tech clock in/out sessions (touch time vs. calendar time), part lines validated against VISUAL with unit cost snapshotted at entry. Repair cost = parts + hours × configurable loaded rate (`rtm.config`).
- **Judgment fields**: root cause / responsibility / resolution as controlled-vocabulary button groups (seeded per JP's categories), plus round count and ammo.
- **Monthly reports** (`/rtm/reports` + CSV): RTM count and **rate** by model/caliber (denominator = VISUAL shipped units), root-cause Pareto, responsibility/resolution splits, avg/total repair cost, avg touch hours and calendar days, repeat-return list.
- **Tablet UI**: large touch targets, one task per screen, plain HTML/CSS/vanilla JS.

## Architecture

- `visual_client.py` — read-only VISUAL (SQL Server/VECA) via pymssql; all four queries validated live (TRACE serial chain, Z_PRODUCT_DETAIL attributes, PART_SITE costs, SHIPPER→CUST_ORDER_LINE shipped-units denominator).
- `rtm_db.py` + `migrations/` — new `rtm` schema in the existing DigitalOcean Postgres; psycopg v3, plain SQL, numbered migrations applied at container start.
- `rtm_views.py` / `rtm_reports.py` — Flask blueprints under `/rtm`.
- Docker: entrypoint runs migrations then gunicorn; `docker-compose.dev.yml` adds a local Postgres for dev.

## Testing

- 11 smoke tests (`tests/smoke_rtm.py`) green against a real local Postgres: intake snapshot + repeat detection, state-machine enforcement, double clock-in rejection, cost snapshotting and math, close-out validation, report math, VISUAL-outage degradation, ticket-print regression.
- Full lifecycle manually verified in-browser (intake → transitions → clock in/out → findings → close → reports).

## Before merge/deploy

- [ ] VISUAL read-only SQL login + DO Postgres URL in the server's `.env`
- [ ] Category vocabulary + labor rate sign-off (JP/Colton/Trevor; seed lists are the proposal)
- [ ] Replace placeholder techs (Colton/Trevor) with the real bench tech list

🤖 Generated with [Claude Code](https://claude.com/claude-code)
