-- Both collections inherit the documents tenant RLS, including FORCE RLS.
-- The content body and review reasons are encrypted by the application.
CREATE UNIQUE INDEX IF NOT EXISTS uq_interview_skill_revision
    ON documents (organization_id, (data->>'skill_id'), ((data->>'revision')::integer))
    WHERE collection = 'interview_skill_revisions';

CREATE INDEX IF NOT EXISTS idx_interview_skill_authorization
    ON documents (organization_id, (data->>'skill_id'), (data->>'status'))
    WHERE collection = 'interview_skill_revisions';

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_interview_skill_revision') THEN
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
                AND data->>'status' IN ('draft', 'validated', 'approved', 'retired', 'revoked')
                AND data->>'content_hash' ~ '^[a-f0-9]{64}$'
                AND length(data->>'sealed_package') > 0
            )
        );
    END IF;
END
$$;
