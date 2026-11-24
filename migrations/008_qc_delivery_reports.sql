-- QA/QC pass on the stage forms, product data from Zendesk, delivery
-- tracking with an automatic "delivered" email, and a review-and-send queue
-- for final repair reports.

-- Small key/value settings edited in Settings (Zendesk field mapping,
-- delivered message, final report template). Missing keys use code defaults.
CREATE TABLE repair.setting (
    key         text PRIMARY KEY,
    value       text,
    updated_at  timestamptz NOT NULL DEFAULT now()
);

-- Root cause: one pick per repair, for warranty and manufacturing QC.
ALTER TABLE repair.choice DROP CONSTRAINT choice_kind_check;
ALTER TABLE repair.choice ADD CONSTRAINT choice_kind_check
    CHECK (kind IN ('problem', 'solution', 'accessory', 'inspection_point', 'root_cause'));

INSERT INTO repair.choice (kind, label, position) VALUES
    ('root_cause', 'Manufacturing defect: machining / assembly', 10),
    ('root_cause', 'Component or supplier part failure', 20),
    ('root_cause', 'Design issue', 30),
    ('root_cause', 'Normal wear', 40),
    ('root_cause', 'Customer damage or modification', 50),
    ('root_cause', 'Shipping damage', 60),
    ('root_cause', 'Ammunition related', 70),
    ('root_cause', 'No fault found', 80);

-- New event kinds: the delivered email and the final report.
ALTER TABLE repair.event DROP CONSTRAINT event_kind_check;
ALTER TABLE repair.event ADD CONSTRAINT event_kind_check
    CHECK (kind IN ('added', 'stage', 'action', 'note', 'delivery', 'report'));

ALTER TABLE repair.rma
    -- Snapshot of the Zendesk ticket's fields (refreshed whenever the ticket is opened)
    ADD COLUMN zd_serial            text,
    ADD COLUMN zd_model             text,
    ADD COLUMN zd_caliber           text,
    ADD COLUMN zd_part_number       text,
    ADD COLUMN zd_inbound_tracking  text,
    ADD COLUMN ticket_fields        jsonb,      -- [{title, value}] every filled-in custom field
    -- Received: what the tech verified on the rifle and the box
    ADD COLUMN serial_number        text,
    ADD COLUMN serial_match         boolean,    -- NULL: no serial on the ticket to compare
    ADD COLUMN inbound_tracking     text,
    ADD COLUMN inbound_match        boolean,
    ADD COLUMN model                text,
    ADD COLUMN caliber              text,
    ADD COLUMN part_number          text,
    ADD COLUMN arrived_loaded       boolean,
    -- Inspected: measured values
    ADD COLUMN trigger_pull_found   numeric(5, 2),          -- lb
    ADD COLUMN headspace            text CHECK (headspace IN ('pass', 'fail', 'not_checked')),
    -- Repair complete
    ADD COLUMN root_cause_id        integer REFERENCES repair.choice (id),
    ADD COLUMN coverage             text CHECK (coverage IN ('warranty', 'customer_pay', 'goodwill')),
    ADD COLUMN parts_used           text,
    ADD COLUMN trigger_pull_after   numeric(5, 2),
    -- Final check (on the Accuracy test stage)
    ADD COLUMN function_check       text CHECK (function_check IN ('pass', 'fail')),
    -- Shipped: serial scanned at pack-out and the return carrier
    ADD COLUMN ship_serial          text,
    ADD COLUMN return_carrier       text,
    ADD COLUMN ship_override        boolean,    -- shipped without a passed final check (manager OK)
    -- Delivery tracking of the return shipment
    ADD COLUMN delivery_status      text,       -- pre_transit, in_transit, out_for_delivery, delivered, exception, unknown
    ADD COLUMN delivery_detail      text,
    ADD COLUMN delivery_checked_at  timestamptz,
    ADD COLUMN delivered_at         timestamptz;

CREATE INDEX rma_awaiting_delivery_idx ON repair.rma (shipped_at)
    WHERE stage = 'shipped' AND delivered_at IS NULL;

-- Final repair report: drafted from the record, edited if needed, reviewed and
-- sent to the customer as one public comment.
CREATE TABLE repair.final_report (
    ticket_id   bigint PRIMARY KEY REFERENCES repair.rma (ticket_id) ON DELETE CASCADE,
    body        text,               -- saved draft; NULL = build from the record
    edited_by   text,
    edited_at   timestamptz,
    sent_at     timestamptz,
    sent_by     text,
    sent_body   text
);

-- Shipped message: add a tracking link (only if still the original wording).
UPDATE repair.stage SET comment_body =
    E'Your rifle is on its way back to you via {carrier}, tracking number {tracking}.
{tracking_link}
We will email you again when it is delivered. Thank you for your patience.'
WHERE key = 'shipped' AND comment_body =
    'Your rifle is on its way back to you. Tracking number: {tracking}. Thank you for your patience.';

-- A test that also covers the final safety / function check.
UPDATE repair.stage SET label = 'Final test'
WHERE key = 'tested' AND label = 'Accuracy test';
