-- Optional user-authored v2 Skills become active when saved. Preserve the
-- recorded 003 migration and upgrade only its lifecycle check in this step.
ALTER TABLE documents DROP CONSTRAINT IF EXISTS ck_interview_skill_revision;

ALTER TABLE documents ADD CONSTRAINT ck_interview_skill_revision
CHECK (
    collection <> 'interview_skill_revisions'
    OR (
        data ?& ARRAY['skill_id', 'revision', 'status', 'authorization_epoch', 'content_hash', 'sealed_package']
        AND jsonb_typeof(data->'skill_id') = 'string'
        AND jsonb_typeof(data->'revision') = 'number'
        AND jsonb_typeof(data->'status') = 'string'
        AND jsonb_typeof(data->'authorization_epoch') = 'number'
        AND jsonb_typeof(data->'content_hash') = 'string'
        AND jsonb_typeof(data->'sealed_package') = 'string'
        AND (data->>'revision')::integer >= 1
        AND (data->>'authorization_epoch')::integer >= 1
        AND data->>'status' IN ('active', 'draft', 'validated', 'approved', 'retired', 'revoked')
        AND data->>'content_hash' ~ '^[a-f0-9]{64}$'
        AND length(data->>'sealed_package') > 0
    )
);

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                   WHERE conrelid = 'documents'::regclass AND conname = 'ck_interview_skill_active_version') THEN
        ALTER TABLE documents ADD CONSTRAINT ck_interview_skill_active_version
        CHECK (
            collection <> 'interview_skill_revisions'
            OR data->>'status' <> 'active'
            OR (data ? 'schema_version'
                AND jsonb_typeof(data->'schema_version') = 'string'
                AND data->>'schema_version' = 'interview_skill.v2')
        );
    END IF;
END
$$;
