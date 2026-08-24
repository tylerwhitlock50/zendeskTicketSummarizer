-- Seed lookup values, config, and placeholder techs. Idempotent.

INSERT INTO rtm.root_cause (id, code, label, sort_order) VALUES
    (1, 'manufacturing',       'Manufacturing',         1),
    (2, 'component_supplier',  'Component / Supplier',  2),
    (3, 'assembly',            'Assembly',              3),
    (4, 'accuracy',            'Accuracy',              4),
    (5, 'ammunition',          'Ammunition',            5),
    (6, 'customer_use',        'Customer Use / Setup',  6),
    (7, 'no_fault_found',      'No Fault Found',        7),
    (8, 'other',               'Other',                 8)
ON CONFLICT DO NOTHING;

INSERT INTO rtm.responsibility (id, code, label, sort_order) VALUES
    (1, 'christensen',  'Christensen',   1),
    (2, 'customer',     'Customer',      2),
    (3, 'supplier',     'Supplier',      3),
    (4, 'undetermined', 'Undetermined',  4)
ON CONFLICT DO NOTHING;

INSERT INTO rtm.resolution (id, code, label, sort_order) VALUES
    (1, 'repair',             'Repair',              1),
    (2, 'parts_replaced',     'Parts Replaced',      2),
    (3, 'rifle_replaced',     'Rifle Replaced',      3),
    (4, 'returned_as_is',     'Returned As-Is',      4),
    (5, 'customer_education', 'Customer Education',  5),
    (6, 'other',              'Other',               6)
ON CONFLICT DO NOTHING;

INSERT INTO rtm.config (key, value) VALUES
    ('labor_rate_per_hour', '85.00')
ON CONFLICT DO NOTHING;

INSERT INTO rtm.tech (name, active) VALUES
    ('Colton', true),
    ('Trevor', true)
ON CONFLICT DO NOTHING;
