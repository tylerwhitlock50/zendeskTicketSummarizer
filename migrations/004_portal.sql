-- Scan portal: configurable quick actions + local audit log.
-- Zendesk is the system of record for workflow; these tables only hold the
-- button configuration and a local trace of what the portal posted.

CREATE SCHEMA IF NOT EXISTS portal;

CREATE TABLE portal.quick_action (
    id            serial PRIMARY KEY,
    label         text        NOT NULL,
    comment_body  text        NOT NULL,
    tag           text        NOT NULL,
    position      integer     NOT NULL DEFAULT 0,
    active        boolean     NOT NULL DEFAULT true,
    created_at    timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE portal.action_log (
    id            serial PRIMARY KEY,
    ticket_id     bigint      NOT NULL,
    action_label  text        NOT NULL,
    note_excerpt  text,
    tech_initials text,
    created_at    timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX action_log_ticket_idx ON portal.action_log (ticket_id, created_at DESC);

INSERT INTO portal.quick_action (label, comment_body, tag, position) VALUES
    ('Arrived',
     'Good news — your firearm has arrived at our service center and has been checked in. We will update you as it moves through the process.',
     'rp-arrived', 10),
    ('Being worked on',
     'Update: your firearm is now being worked on by one of our gunsmiths.',
     'rp-in-work', 20),
    ('Being tested',
     'Update: work on your firearm is complete and it is now being test fired and inspected.',
     'rp-testing', 30),
    ('Awaiting processing',
     'Update: your firearm has finished service and is awaiting final processing before it ships back to you.',
     'rp-awaiting', 40);
