-- Repair record: the shop-side data for each RMA.
-- Zendesk keeps the customer conversation and sends the emails; these tables
-- keep who did what, when, and what was found, for reporting.

CREATE SCHEMA IF NOT EXISTS repair;

-- Technicians and shipping staff who post from the scan portal. They sign in
-- with a name and PIN (hashed); they never need a Zendesk seat.
CREATE TABLE repair.tech (
    id          serial PRIMARY KEY,
    name        text        NOT NULL,
    pin_hash    text        NOT NULL,
    active      boolean     NOT NULL DEFAULT true,
    failed_pins integer     NOT NULL DEFAULT 0,   -- wrong PINs in a row
    locked_until timestamptz,                     -- sign-in paused after too many
    created_at  timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX tech_name_uq ON repair.tech (lower(name));

-- One random session-signing key shared by every app worker, created on first
-- start when FLASK_SECRET_KEY is not set (so there is never a guessable default).
CREATE TABLE repair.app_secret (
    id      integer PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    secret  text    NOT NULL
);

-- Drop-down lists for the inspection (problem) and repair (solution) forms.
CREATE TABLE repair.choice (
    id          serial PRIMARY KEY,
    kind        text        NOT NULL CHECK (kind IN ('problem', 'solution')),
    label       text        NOT NULL,
    position    integer     NOT NULL DEFAULT 0,
    active      boolean     NOT NULL DEFAULT true
);
CREATE UNIQUE INDEX choice_kind_label_uq ON repair.choice (kind, lower(label));

-- The four scan stages. Keys are fixed in code (each has its own form);
-- labels and the customer message are editable in Settings.
CREATE TABLE repair.stage (
    key            text PRIMARY KEY CHECK (key IN ('received', 'inspected', 'repaired', 'shipped')),
    label          text    NOT NULL,
    comment_body   text    NOT NULL,
    zendesk_value  text,            -- option value of the Zendesk "RMA Stage" drop-down, if used
    position       integer NOT NULL
);

-- One row per RMA (a Zendesk ticket added to the repair queue).
CREATE TABLE repair.rma (
    ticket_id        bigint PRIMARY KEY,
    subject          text,
    requester_name   text,
    requester_email  text,
    stage            text        NOT NULL DEFAULT 'queued',
    added_at         timestamptz NOT NULL DEFAULT now(),
    added_by         text,
    received_at      timestamptz,
    inspected_at     timestamptz,
    repaired_at      timestamptz,
    shipped_at       timestamptz,
    problem_id       integer REFERENCES repair.choice (id),
    problem_notes    text,
    solution_id      integer REFERENCES repair.choice (id),
    work_done        text,
    tracking_number  text,
    updated_at       timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX rma_stage_idx ON repair.rma (stage);

-- Every scan and form, with who and when. detail holds the form answers.
CREATE TABLE repair.event (
    id              serial PRIMARY KEY,
    ticket_id       bigint      NOT NULL REFERENCES repair.rma (ticket_id),
    kind            text        NOT NULL CHECK (kind IN ('added', 'stage', 'action', 'note')),
    stage           text,
    label           text        NOT NULL,
    tech_id         integer REFERENCES repair.tech (id),
    tech_name       text,
    detail          jsonb       NOT NULL DEFAULT '{}'::jsonb,
    customer_message text,
    created_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX event_ticket_idx ON repair.event (ticket_id, created_at DESC);
CREATE INDEX event_created_idx ON repair.event (created_at);

INSERT INTO repair.stage (key, label, comment_body, position) VALUES
    ('received', 'Received',
     'Good news: your rifle has arrived at our Repair Center and is checked in. We will email you again once our technicians have inspected it.',
     10),
    ('inspected', 'Inspected',
     'Update: our technicians have inspected your rifle and found the following: {problem}. We are starting the repair now and will email you when it is complete.',
     20),
    ('repaired', 'Repair complete',
     'Update: the repair on your rifle is complete. Work performed: {work_done}. It is now being prepared to ship back to you.',
     30),
    ('shipped', 'Shipped',
     'Your rifle is on its way back to you. Tracking number: {tracking}. Thank you for your patience.',
     40);

-- Starter lists so the forms work on day one; the Repair Center replaces these.
INSERT INTO repair.choice (kind, label, position) VALUES
    ('problem', 'Accuracy / will not group', 10),
    ('problem', 'Feeding or extraction', 20),
    ('problem', 'Trigger', 30),
    ('problem', 'Bolt or action', 40),
    ('problem', 'Stock or bedding', 50),
    ('problem', 'Finish / cosmetic', 60),
    ('problem', 'No fault found', 70),
    ('problem', 'Other', 80),
    ('solution', 'Re-bedded action', 10),
    ('solution', 'Replaced part', 20),
    ('solution', 'Adjusted trigger', 30),
    ('solution', 'Replaced barrel', 40),
    ('solution', 'Refinished', 50),
    ('solution', 'Cleaned and test fired, no fault found', 60),
    ('solution', 'Other', 70);

-- The stage buttons replace these two original quick actions; keep them
-- (history refers to them) but hide them so techs see one button per stage.
UPDATE portal.quick_action SET active = false WHERE tag IN ('rp-arrived', 'rp-awaiting');
