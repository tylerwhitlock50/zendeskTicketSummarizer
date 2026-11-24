-- ATF acquisition & disposition (A&D) log for firearms that pass through the
-- repair portal: one entry per time a serial number comes into the building
-- (Received) and leaves it (Shipped), with where it came from and went.
--
-- Entries are written in the same transaction as the stage post, never
-- deleted, and every change after the fact (a re-posted stage, a stage moved
-- back, an edit by the compliance officer) is kept in repair.atf_change with
-- the old and new values, who and why.

CREATE TABLE repair.atf_entry (
    id                      serial PRIMARY KEY,
    ticket_id               bigint      NOT NULL REFERENCES repair.rma (ticket_id),
    -- Firearm description
    manufacturer            text,
    model                   text,
    caliber                 text,
    firearm_type            text,
    serial_number           text,
    -- Acquisition: the Received scan
    acquired_at             timestamptz NOT NULL,
    acquired_from_name      text,
    acquired_from_address   text,
    acquired_from_ffl       text,       -- when it came from a dealer / FFL holder
    acquired_tracking       text,
    acquired_by             text,       -- who scanned it in
    -- Disposition: the Shipped scan (NULL while it is in the building)
    disposed_at             timestamptz,
    disposed_to_name        text,
    disposed_to_address     text,
    disposed_to_ffl         text,
    disposed_serial         text,       -- serial scanned at pack-out
    disposed_tracking       text,
    disposed_carrier        text,
    disposed_by             text,
    -- 'portal' = written by a stage post; 'backfill' = rebuilt from records made
    -- before this log existed; 'manual' = added by the compliance officer.
    source                  text        NOT NULL DEFAULT 'portal'
                                        CHECK (source IN ('portal', 'backfill', 'manual')),
    review_note             text,       -- why an entry needs the officer's attention
    notes                   text,
    created_at              timestamptz NOT NULL DEFAULT now(),
    updated_at              timestamptz NOT NULL DEFAULT now()
);
-- A ticket's rifle is in the building at most once at a time.
CREATE UNIQUE INDEX atf_entry_open_uq ON repair.atf_entry (ticket_id) WHERE disposed_at IS NULL;
CREATE INDEX atf_entry_ticket_idx ON repair.atf_entry (ticket_id, acquired_at DESC);
CREATE INDEX atf_entry_acquired_idx ON repair.atf_entry (acquired_at);
CREATE INDEX atf_entry_disposed_idx ON repair.atf_entry (disposed_at);
CREATE INDEX atf_entry_serial_idx ON repair.atf_entry (upper(regexp_replace(COALESCE(serial_number, ''), '[[:space:]-]+', '', 'g')));

-- Every change to an entry after it was written.
CREATE TABLE repair.atf_change (
    id          serial PRIMARY KEY,
    entry_id    integer     NOT NULL REFERENCES repair.atf_entry (id),
    changed_at  timestamptz NOT NULL DEFAULT now(),
    changed_by  text,
    reason      text        NOT NULL,
    before      jsonb       NOT NULL DEFAULT '{}'::jsonb,   -- only the fields that changed
    after       jsonb       NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX atf_change_entry_idx ON repair.atf_change (entry_id, changed_at);

-- Customer address and dealer FFL from the Zendesk ticket (see Settings → Zendesk fields).
ALTER TABLE repair.rma
    ADD COLUMN zd_address  text,
    ADD COLUMN zd_ffl      text;

-- Backfill from the repair records made before this log: every RMA that was
-- scanned in. The customer's address isn't known for these, so they are
-- flagged for review.
INSERT INTO repair.atf_entry (
    ticket_id, manufacturer, model, caliber, firearm_type, serial_number,
    acquired_at, acquired_from_name, acquired_tracking,
    disposed_at, disposed_to_name, disposed_serial, disposed_tracking, disposed_carrier,
    source, review_note)
SELECT r.ticket_id, 'Christensen Arms',
       COALESCE(NULLIF(r.model, ''), r.zd_model),
       COALESCE(NULLIF(r.caliber, ''), r.zd_caliber),
       'Rifle',
       COALESCE(NULLIF(r.serial_number, ''), r.zd_serial),
       r.received_at, r.requester_name, COALESCE(NULLIF(r.inbound_tracking, ''), r.zd_inbound_tracking),
       CASE WHEN r.stage = 'shipped' THEN r.shipped_at END,
       CASE WHEN r.stage = 'shipped' THEN r.requester_name END,
       CASE WHEN r.stage = 'shipped' THEN r.ship_serial END,
       CASE WHEN r.stage = 'shipped' THEN r.tracking_number END,
       CASE WHEN r.stage = 'shipped' THEN r.return_carrier END,
       'backfill',
       'Rebuilt from the repair record when the A&D log was added; add the address(es).'
FROM repair.rma r
WHERE r.received_at IS NOT NULL;
