-- Run the Python document migrator before starting the v2 application.
-- This DDL keeps the PostgreSQL secret table aligned for upgraded databases.
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'provider_secrets' AND column_name = 'provider_config_id'
    ) AND NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'provider_secrets' AND column_name = 'provider_connection_id'
    ) THEN
        ALTER TABLE provider_secrets RENAME COLUMN provider_config_id TO provider_connection_id;
    END IF;
END
$$;
