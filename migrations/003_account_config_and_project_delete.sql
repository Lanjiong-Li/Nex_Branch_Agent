-- Account-level configuration migration/idempotency and an explicit, transaction-scoped
-- escape hatch for permanent project deletion. Ordinary record deletion remains blocked.
CREATE TABLE account_config_migrations (
 account_id TEXT PRIMARY KEY,
 source_project_id UUID,
 migrated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);

CREATE TABLE account_api_idempotency (
 account_id TEXT NOT NULL,
 operation TEXT NOT NULL,
 key TEXT NOT NULL,
 sha256 TEXT NOT NULL,
 result JSONB NOT NULL,
 created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
 PRIMARY KEY(account_id,operation,key)
);

CREATE INDEX config_versions_account_scope ON config_versions(
 (data->>'owner_account_id'),config_key,(data->>'scope_key'),state,version
) WHERE project_id IS NULL;

CREATE OR REPLACE FUNCTION harness_record_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF TG_OP='DELETE' THEN
  IF current_setting('branch_agent.hard_delete_project',true)=OLD.project_id::text THEN RETURN OLD; END IF;
  IF OLD.data->>'record_type' <> 'session_item' THEN RAISE EXCEPTION 'immutable record' USING ERRCODE='23514'; END IF;
  RETURN OLD;
 END IF;
 NEW.id := (NEW.data->>'id')::uuid;
 NEW.project_id := (NEW.data->>'project_id')::uuid;
 NEW.created_at := (NEW.data->>'created_at')::timestamptz;
 NEW.updated_at := (NEW.data->>'updated_at')::timestamptz;
 NEW.row_version := (NEW.data->>'row_version')::bigint;
 IF TG_OP='UPDATE' THEN
  IF OLD.row_version IS NULL THEN RAISE EXCEPTION 'immutable record' USING ERRCODE='23514'; END IF;
  IF NEW.id<>OLD.id OR NEW.project_id IS DISTINCT FROM OLD.project_id OR NEW.created_at<>OLD.created_at OR
     NEW.row_version<>OLD.row_version+1 THEN RAISE EXCEPTION 'invalid identity or row version' USING ERRCODE='23514'; END IF;
  IF OLD.data->>'record_type'='config_version' AND
     (OLD.data->>'scope_kind'='run_snapshot' OR
      (NEW.data - ARRAY['updated_at','row_version','state','published_at']) IS DISTINCT FROM
      (OLD.data - ARRAY['updated_at','row_version','state','published_at'])) THEN
   RAISE EXCEPTION 'immutable configuration content' USING ERRCODE='23514';
  END IF;
  IF OLD.data->>'record_type'='blob' AND OLD.data->>'state'='ready' AND
     (NEW.data-ARRAY['updated_at','row_version']) IS DISTINCT FROM (OLD.data-ARRAY['updated_at','row_version']) THEN
   RAISE EXCEPTION 'immutable ready blob' USING ERRCODE='23514';
  END IF;
 END IF;
 RETURN NEW;
END $$;
