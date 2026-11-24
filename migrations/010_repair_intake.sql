-- Customer repair-intake form. When a ticket is added to the repair queue the
-- portal posts a public Zendesk comment with a private link ("we're preparing
-- to receive your firearm; anything specific you'd like us to look at?"). The
-- customer's answers are saved here first, then written back to the same ticket.
--
-- One intake per ticket. Only the SHA-256 of the link token is stored, so a
-- lost link is replaced (Send a new link), never re-shown. Photos wait in
-- repair.intake_file until Zendesk has them (its upload tokens expire after an
-- hour, so a retry needs the bytes), then the rows are deleted.
-- This migration adds no rows: tickets already in the queue never get a link
-- unless a tech sends one by hand.

CREATE TABLE repair.intake (
    id                serial PRIMARY KEY,
    ticket_id         bigint      NOT NULL REFERENCES repair.rma (ticket_id) ON DELETE CASCADE,
    token_hash        text        NOT NULL CHECK (length(token_hash) = 64),  -- sha256 hex of the link token
    -- pending_link = row made, link comment not confirmed on the ticket
    -- link_sent    = public comment with the link is on the ticket
    -- submitted    = customer's answers saved here, not yet in Zendesk
    -- synced       = answers and photos are on the ticket
    -- sync_failed  = write-back failed; next_attempt_at NULL = needs staff
    -- expired      = link ran out before the customer answered
    status            text        NOT NULL DEFAULT 'pending_link'
                                  CHECK (status IN ('pending_link', 'link_sent', 'submitted',
                                                    'synced', 'sync_failed', 'expired')),
    expires_at        timestamptz NOT NULL,
    created_by        text,                   -- tech who added the ticket / sent the link
    link_sent_at      timestamptz,
    link_send_count   integer     NOT NULL DEFAULT 0,
    reminder_sent_at  timestamptz,
    submitted_at      timestamptz,
    synced_at         timestamptz,
    answers           jsonb       NOT NULL DEFAULT '{}'::jsonb,
    -- Retry bookkeeping for whichever Zendesk call this row is waiting on.
    attempts          integer     NOT NULL DEFAULT 0,
    next_attempt_at   timestamptz,            -- NULL = no automatic retry
    claimed_until     timestamptz,            -- a sync is running right now; others skip
    last_error        text,
    created_at        timestamptz NOT NULL DEFAULT now(),
    updated_at        timestamptz NOT NULL DEFAULT now()
);
-- One intake per Zendesk ticket: two taps on "Add to queue" send one email.
CREATE UNIQUE INDEX intake_ticket_uq ON repair.intake (ticket_id);
CREATE UNIQUE INDEX intake_token_hash_uq ON repair.intake (token_hash);
CREATE INDEX intake_status_idx ON repair.intake (status, next_attempt_at);

-- Photos and PDFs from the form, kept only until they are on the Zendesk ticket.
CREATE TABLE repair.intake_file (
    id            serial PRIMARY KEY,
    intake_id     integer     NOT NULL REFERENCES repair.intake (id) ON DELETE CASCADE,
    position      integer     NOT NULL DEFAULT 0,
    filename      text        NOT NULL,       -- made by the portal, not the customer's name
    content_type  text        NOT NULL,       -- from the file's own bytes
    size_bytes    integer     NOT NULL,
    data          bytea       NOT NULL,
    created_at    timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX intake_file_intake_idx ON repair.intake_file (intake_id, position);

-- New event kind: intake link sent / customer form received.
ALTER TABLE repair.event DROP CONSTRAINT event_kind_check;
ALTER TABLE repair.event ADD CONSTRAINT event_kind_check
    CHECK (kind IN ('added', 'stage', 'action', 'note', 'delivery', 'report', 'intake'));

-- "What best describes it?" tick boxes on the customer form, so the team can
-- sort incoming rifles at a glance. Editable under Settings like the other lists.
ALTER TABLE repair.choice DROP CONSTRAINT choice_kind_check;
ALTER TABLE repair.choice ADD CONSTRAINT choice_kind_check
    CHECK (kind IN ('problem', 'solution', 'accessory', 'inspection_point', 'root_cause', 'concern'));

INSERT INTO repair.choice (kind, label, position) VALUES
    ('concern', 'Accuracy', 10),
    ('concern', 'Pressure signs', 20),
    ('concern', 'Feeding', 30),
    ('concern', 'Ejection or extraction', 40),
    ('concern', 'Failure to fire', 50),
    ('concern', 'Trigger or safety', 60),
    ('concern', 'Bolt or action', 70),
    ('concern', 'Stock or chassis', 80),
    ('concern', 'Barrel or muzzle device', 90),
    ('concern', 'Finish or cosmetic', 100),
    ('concern', 'Other', 110);
