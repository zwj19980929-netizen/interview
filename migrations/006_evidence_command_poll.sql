-- Bound realtime owner polling by the current interview's unsettled commands.
-- Existing document RLS remains authoritative; no history is removed and no
-- lease or command status is changed by this additive migration.
CREATE INDEX IF NOT EXISTS idx_evidence_commands_unsettled
    ON documents (organization_id, (data->>'interview_id'))
    WHERE collection = 'evidence_commands'
      AND data->>'status' IN ('pending', 'running');
