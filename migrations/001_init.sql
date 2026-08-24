-- RTM tracker initial schema.
CREATE SCHEMA IF NOT EXISTS rtm;

CREATE TABLE IF NOT EXISTS rtm.root_cause (
    id smallint PRIMARY KEY,
    code text UNIQUE NOT NULL,
    label text NOT NULL,
    sort_order smallint NOT NULL DEFAULT 0,
    active boolean NOT NULL DEFAULT true
);

CREATE TABLE IF NOT EXISTS rtm.responsibility (
    id smallint PRIMARY KEY,
    code text UNIQUE NOT NULL,
    label text NOT NULL,
    sort_order smallint NOT NULL DEFAULT 0,
    active boolean NOT NULL DEFAULT true
);

CREATE TABLE IF NOT EXISTS rtm.resolution (
    id smallint PRIMARY KEY,
    code text UNIQUE NOT NULL,
    label text NOT NULL,
    sort_order smallint NOT NULL DEFAULT 0,
    active boolean NOT NULL DEFAULT true
);

CREATE TABLE IF NOT EXISTS rtm.tech (
    id serial PRIMARY KEY,
    name text UNIQUE NOT NULL,
    active boolean NOT NULL DEFAULT true
);

CREATE TABLE IF NOT EXISTS rtm.config (
    key text PRIMARY KEY,
    value text NOT NULL
);

CREATE TABLE IF NOT EXISTS rtm.rtm (
    id serial PRIMARY KEY,
    rtm_number text UNIQUE,
    serial_no text NOT NULL,
    part_id text,
    model text,
    caliber text,
    barrel_length text,
    visual_snapshot jsonb,
    build_date date,
    ship_date date,
    original_order_id text,
    original_customer_id text,
    zendesk_ticket_id bigint,
    reason_for_return text,
    inspection_notes text,
    findings text,
    root_cause_id smallint REFERENCES rtm.root_cause(id),
    responsibility_id smallint REFERENCES rtm.responsibility(id),
    resolution_id smallint REFERENCES rtm.resolution(id),
    round_count int CHECK (round_count >= 0),
    ammo_used text,
    repeat_return boolean NOT NULL DEFAULT false,
    status text NOT NULL DEFAULT 'received'
        CHECK (status IN ('received','in_inspection','in_repair','qc_test','shipped','closed')),
    received_at timestamptz NOT NULL DEFAULT now(),
    shipped_at timestamptz,
    closed_at timestamptz,
    created_by int REFERENCES rtm.tech(id),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS rtm_serial_no_idx ON rtm.rtm (serial_no);
CREATE INDEX IF NOT EXISTS rtm_status_idx ON rtm.rtm (status);
CREATE INDEX IF NOT EXISTS rtm_model_caliber_idx ON rtm.rtm (model, caliber);
CREATE INDEX IF NOT EXISTS rtm_received_at_idx ON rtm.rtm (received_at);

CREATE TABLE IF NOT EXISTS rtm.status_event (
    id serial PRIMARY KEY,
    rtm_id int NOT NULL REFERENCES rtm.rtm(id) ON DELETE CASCADE,
    status text NOT NULL,
    changed_at timestamptz NOT NULL DEFAULT now(),
    tech_id int REFERENCES rtm.tech(id)
);

CREATE INDEX IF NOT EXISTS status_event_rtm_id_idx ON rtm.status_event (rtm_id);

CREATE TABLE IF NOT EXISTS rtm.work_session (
    id serial PRIMARY KEY,
    rtm_id int NOT NULL REFERENCES rtm.rtm(id) ON DELETE CASCADE,
    tech_id int NOT NULL REFERENCES rtm.tech(id),
    started_at timestamptz NOT NULL DEFAULT now(),
    ended_at timestamptz,
    note text,
    CHECK (ended_at IS NULL OR ended_at > started_at)
);

CREATE INDEX IF NOT EXISTS work_session_rtm_id_idx ON rtm.work_session (rtm_id);
CREATE UNIQUE INDEX IF NOT EXISTS work_session_one_open_per_tech
    ON rtm.work_session (tech_id) WHERE ended_at IS NULL;

CREATE TABLE IF NOT EXISTS rtm.part_line (
    id serial PRIMARY KEY,
    rtm_id int NOT NULL REFERENCES rtm.rtm(id) ON DELETE CASCADE,
    part_id text NOT NULL,
    description text,
    qty numeric(10,2) NOT NULL CHECK (qty > 0),
    unit_cost numeric(12,4) NOT NULL DEFAULT 0,
    added_by int REFERENCES rtm.tech(id),
    added_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS part_line_rtm_id_idx ON rtm.part_line (rtm_id);
