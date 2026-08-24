-- Status board support: hold flag ("Waiting" column) and a 'ready' status
-- between QC and Shipped ("Ready" column).

ALTER TABLE rtm.rtm ADD COLUMN IF NOT EXISTS on_hold boolean NOT NULL DEFAULT false;
ALTER TABLE rtm.rtm ADD COLUMN IF NOT EXISTS hold_reason text;

ALTER TABLE rtm.rtm DROP CONSTRAINT IF EXISTS rtm_status_check;
ALTER TABLE rtm.rtm ADD CONSTRAINT rtm_status_check CHECK (
    status IN ('received', 'in_inspection', 'in_repair', 'qc_test', 'ready', 'shipped', 'closed')
);
