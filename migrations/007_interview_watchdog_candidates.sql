-- Candidate discovery only; services re-read under their existing transaction
-- and decide lease expiry/deadlines using their trusted clock and legacy rules.
-- Falsy JSON values retain Python truthiness for older documents. No data or
-- RLS policy is changed, and runtime roles do not create these indexes.
CREATE INDEX IF NOT EXISTS idx_interviews_watchdog_takeover
    ON documents (organization_id)
    WHERE collection = 'interviews'
      AND jsonb_typeof(data#>'{agent_runtime,takeover}') = 'object'
      AND data#>'{agent_runtime,takeover}' <> '{}'::jsonb
      AND COALESCE(data->'list_removed_at', 'null'::jsonb) IN
          ('null'::jsonb, 'false'::jsonb, '0'::jsonb, '""'::jsonb, '[]'::jsonb, '{}'::jsonb);

CREATE INDEX IF NOT EXISTS idx_interviews_watchdog_deadline
    ON documents (organization_id)
    WHERE collection = 'interviews'
      AND data->>'status' IN ('scheduled', 'waiting', 'in_progress', 'paused')
      AND COALESCE(data->'candidate_input_completed_at', 'null'::jsonb) IN
          ('null'::jsonb, 'false'::jsonb, '0'::jsonb, '""'::jsonb, '[]'::jsonb, '{}'::jsonb);
