CREATE TABLE IF NOT EXISTS documents (
    collection text NOT NULL,
    id text NOT NULL,
    organization_id text NOT NULL,
    data jsonb NOT NULL,
    updated_at timestamptz,
    PRIMARY KEY (collection, id)
);

CREATE INDEX IF NOT EXISTS idx_documents_tenant_collection
    ON documents (organization_id, collection);
CREATE INDEX IF NOT EXISTS idx_documents_json
    ON documents USING gin (data jsonb_path_ops);
CREATE INDEX IF NOT EXISTS idx_question_catalog_scope
    ON documents (
        organization_id,
        (data->>'job_position_id'),
        (data->>'knowledge_base_id'),
        (data->>'status'),
        (data->>'validation_status'),
        (data->>'speech_status')
    ) WHERE collection = 'questions';

CREATE UNIQUE INDEX IF NOT EXISTS uq_interview_appointment_session
    ON documents (organization_id, (data->>'appointment_id'))
    WHERE collection = 'interviews' AND data->>'appointment_id' IS NOT NULL;

CREATE OR REPLACE FUNCTION jsonb_array_has_unique_key(items jsonb, key_name text)
RETURNS boolean
LANGUAGE sql
IMMUTABLE
AS $$
    SELECT CASE
        WHEN items IS NULL OR jsonb_typeof(items) <> 'array' THEN true
        ELSE (
            SELECT count(*) = count(DISTINCT value->>key_name)
            FROM jsonb_array_elements(items)
        )
    END
$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'ck_interview_unique_selection_slots'
    ) THEN
        ALTER TABLE documents ADD CONSTRAINT ck_interview_unique_selection_slots
        CHECK (
            collection <> 'interviews'
            OR jsonb_array_has_unique_key(data->'question_selections', 'slot_id')
        );
    END IF;
END
$$;

CREATE TABLE IF NOT EXISTS outbox_work_items (
    id text PRIMARY KEY,
    organization_id text NOT NULL,
    idempotency_key text NOT NULL,
    status text NOT NULL,
    data jsonb NOT NULL,
    created_at timestamptz NOT NULL,
    updated_at timestamptz NOT NULL,
    UNIQUE (organization_id, idempotency_key)
);
CREATE INDEX IF NOT EXISTS idx_outbox_claim
    ON outbox_work_items (organization_id, status, updated_at);

CREATE TABLE IF NOT EXISTS provider_secrets (
    provider_config_id text NOT NULL,
    organization_id text NOT NULL,
    data jsonb NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (organization_id, provider_config_id)
);

CREATE TABLE IF NOT EXISTS model_invocations (
    id text PRIMARY KEY,
    organization_id text NOT NULL,
    data jsonb NOT NULL,
    created_at timestamptz NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_model_invocations_tenant_time
    ON model_invocations (organization_id, created_at DESC);

ALTER TABLE documents ENABLE ROW LEVEL SECURITY;
ALTER TABLE documents FORCE ROW LEVEL SECURITY;
ALTER TABLE outbox_work_items ENABLE ROW LEVEL SECURITY;
ALTER TABLE outbox_work_items FORCE ROW LEVEL SECURITY;
ALTER TABLE provider_secrets ENABLE ROW LEVEL SECURITY;
ALTER TABLE provider_secrets FORCE ROW LEVEL SECURITY;
ALTER TABLE model_invocations ENABLE ROW LEVEL SECURITY;
ALTER TABLE model_invocations FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS tenant_documents ON documents;
CREATE POLICY tenant_documents ON documents
    USING (organization_id = current_setting('app.organization_id', true))
    WITH CHECK (organization_id = current_setting('app.organization_id', true));
DROP POLICY IF EXISTS tenant_outbox ON outbox_work_items;
CREATE POLICY tenant_outbox ON outbox_work_items
    USING (organization_id = current_setting('app.organization_id', true))
    WITH CHECK (organization_id = current_setting('app.organization_id', true));
DROP POLICY IF EXISTS tenant_provider_secrets ON provider_secrets;
CREATE POLICY tenant_provider_secrets ON provider_secrets
    USING (organization_id = current_setting('app.organization_id', true))
    WITH CHECK (organization_id = current_setting('app.organization_id', true));
DROP POLICY IF EXISTS tenant_model_invocations ON model_invocations;
CREATE POLICY tenant_model_invocations ON model_invocations
    USING (organization_id = current_setting('app.organization_id', true))
    WITH CHECK (organization_id = current_setting('app.organization_id', true));
