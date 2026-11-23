-- Fuller stage forms: an accuracy-test stage, a configurable inspection
-- checklist, accessories received, arrival condition, complaint match, and a
-- status flag for "Other updates" (awaiting parts, on hold, ...).

-- New lists: accessories received with the rifle, and inspection checklist points.
ALTER TABLE repair.choice DROP CONSTRAINT choice_kind_check;
ALTER TABLE repair.choice ADD CONSTRAINT choice_kind_check
    CHECK (kind IN ('problem', 'solution', 'accessory', 'inspection_point'));

-- New stage between Repair complete and Shipped.
ALTER TABLE repair.stage DROP CONSTRAINT stage_key_check;
ALTER TABLE repair.stage ADD CONSTRAINT stage_key_check
    CHECK (key IN ('received', 'inspected', 'repaired', 'tested', 'shipped'));

ALTER TABLE repair.rma
    -- Received
    ADD COLUMN accessory_other  text,
    ADD COLUMN arrival_damage   boolean,
    ADD COLUMN arrival_notes    text,
    -- Inspected: [{id, label, result: ok|issue|na, note}] and does it match the complaint
    ADD COLUMN inspection       jsonb,
    ADD COLUMN complaint_match  text CHECK (complaint_match IN ('yes', 'partly', 'no')),
    -- Accuracy test
    ADD COLUMN tested_at        timestamptz,
    ADD COLUMN test_required    boolean,
    ADD COLUMN test_result      text CHECK (test_result IN ('pass', 'fail')),
    ADD COLUMN group_size       numeric(6, 3),
    ADD COLUMN group_unit       text CHECK (group_unit IN ('moa', 'in')),
    ADD COLUMN test_distance    integer,
    ADD COLUMN test_ammo        text,
    -- Latest "Other update" (e.g. Awaiting parts); cleared when a stage is posted
    ADD COLUMN flag             text,
    ADD COLUMN flag_at          timestamptz;

INSERT INTO repair.stage (key, label, comment_body, position) VALUES
    ('tested', 'Accuracy test',
     'Update: your rifle passed its accuracy test, shooting a {group} group at {distance} yards with {ammo}. It is now being prepared to ship back to you.',
     35);

-- Messages that use the new form answers (only if still the original wording).
UPDATE repair.stage SET comment_body =
    'Good news: your rifle has arrived at our Repair Center and is checked in. Items received with it: {accessories}.{arrival_note} We will email you again once our technicians have inspected it.'
WHERE key = 'received' AND comment_body =
    'Good news: your rifle has arrived at our Repair Center and is checked in. We will email you again once our technicians have inspected it.';

UPDATE repair.stage SET comment_body = E'Update: our technicians have inspected your rifle ({points}-point inspection):\n\n{inspection}\n\nFindings: {problem}. We are starting the repair now and will email you when it is complete.'
WHERE key = 'inspected' AND comment_body =
    'Update: our technicians have inspected your rifle and found the following: {problem}. We are starting the repair now and will email you when it is complete.';

UPDATE repair.stage SET comment_body =
    'Update: the repair on your rifle is complete. Work performed: {work_done}. It is now in final checks before it ships back to you.'
WHERE key = 'repaired' AND comment_body =
    'Update: the repair on your rifle is complete. Work performed: {work_done}. It is now being prepared to ship back to you.';

-- Generic 11-point inspection; edit in Settings.
INSERT INTO repair.choice (kind, label, position) VALUES
    ('inspection_point', 'Barrel: bore, crown and muzzle device', 10),
    ('inspection_point', 'Chamber', 20),
    ('inspection_point', 'Action and receiver', 30),
    ('inspection_point', 'Bolt: lugs, extractor, ejector, firing pin', 40),
    ('inspection_point', 'Trigger: pull weight and function', 50),
    ('inspection_point', 'Safety function', 60),
    ('inspection_point', 'Magazine and feeding', 70),
    ('inspection_point', 'Stock and bedding', 80),
    ('inspection_point', 'Optic mounts and bases', 90),
    ('inspection_point', 'Finish and exterior', 100),
    ('inspection_point', 'Function check: cycle and dry fire', 110),
    ('accessory', 'Scope', 10),
    ('accessory', 'Scope rings / bases', 20),
    ('accessory', 'Magazine', 30),
    ('accessory', 'Sling', 40),
    ('accessory', 'Bipod', 50),
    ('accessory', 'Hard case', 60),
    ('accessory', 'Soft case', 70),
    ('accessory', 'Muzzle device / thread cap', 80);

-- Other updates: one-tap status notes. Public toggle per button.
ALTER TABLE portal.quick_action ADD COLUMN public boolean NOT NULL DEFAULT true;
UPDATE portal.quick_action SET active = false WHERE tag = 'rp-testing';  -- now the Accuracy test stage
INSERT INTO portal.quick_action (label, comment_body, tag, position) VALUES
    ('Awaiting parts',
     'Update: your repair is waiting on parts from our manufacturing team. We will email you as soon as they arrive and work continues.',
     'rp-parts', 50),
    ('Under review',
     'Update: your rifle is under review by our technical team. We will email you once we have a plan for the repair.',
     'rp-review', 60),
    ('On hold',
     'Update: your repair is on hold for the moment. Our customer service team will be in touch with the details.',
     'rp-hold', 70);
