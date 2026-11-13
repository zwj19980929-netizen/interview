-- Optional organization defaults inherit documents ENABLE/FORCE ROW LEVEL
-- SECURITY and tenant_documents policy. No separate bypass-RLS table exists.
CREATE UNIQUE INDEX IF NOT EXISTS uq_interview_customization_organization
    ON documents (organization_id)
    WHERE collection = 'interview_customizations';

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                   WHERE conrelid = 'documents'::regclass AND conname = 'ck_interview_customization') THEN
        ALTER TABLE documents ADD CONSTRAINT ck_interview_customization
        CHECK (
            collection <> 'interview_customizations'
            OR (
                data ?& ARRAY['id', 'organization_id', 'version', 'default_skill_id',
                    'sealed_company_profile', 'content_hash', 'has_company_profile', 'updated_at', 'updated_by']
                AND jsonb_typeof(data->'id') = 'string'
                AND jsonb_typeof(data->'organization_id') = 'string'
                AND id = organization_id
                AND data->>'id' = id
                AND data->>'organization_id' = organization_id
                AND jsonb_typeof(data->'version') = 'number'
                AND data->>'version' ~ '^[1-9][0-9]*$'
                AND jsonb_typeof(data->'default_skill_id') IN ('null', 'string')
                AND (data->'default_skill_id' = 'null'::jsonb OR length(data->>'default_skill_id') > 0)
                AND jsonb_typeof(data->'sealed_company_profile') = 'string'
                AND length(data->>'sealed_company_profile') > 0
                AND jsonb_typeof(data->'content_hash') = 'string'
                AND data->>'content_hash' ~ '^[a-f0-9]{64}$'
                AND jsonb_typeof(data->'has_company_profile') = 'boolean'
                AND jsonb_typeof(data->'updated_at') = 'string'
                AND jsonb_typeof(data->'updated_by') = 'string'
            )
        );
    END IF;
END
$$;
