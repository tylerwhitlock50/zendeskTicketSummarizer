-- Inspection and repair forms: pick SEVERAL problems / solutions, plus a
-- free-text "Other" for each. Picks move from rma.problem_id / solution_id
-- into a link table; existing picks are carried over.

CREATE TABLE repair.rma_choice (
    ticket_id  bigint  NOT NULL REFERENCES repair.rma (ticket_id) ON DELETE CASCADE,
    choice_id  integer NOT NULL REFERENCES repair.choice (id),
    PRIMARY KEY (ticket_id, choice_id)
);

INSERT INTO repair.rma_choice (ticket_id, choice_id)
SELECT ticket_id, problem_id FROM repair.rma WHERE problem_id IS NOT NULL
UNION
SELECT ticket_id, solution_id FROM repair.rma WHERE solution_id IS NOT NULL;

ALTER TABLE repair.rma
    ADD COLUMN problem_other  text,
    ADD COLUMN solution_other text,
    DROP COLUMN problem_id,
    DROP COLUMN solution_id;

-- The free-text box replaces the seeded "Other" list items. Hidden, not
-- deleted, so RMAs that already picked it keep their history.
UPDATE repair.choice SET active = false WHERE lower(label) = 'other';
