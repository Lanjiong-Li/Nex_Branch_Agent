-- Record JSON is authoritative; projections are generated or assigned by a trigger.
CREATE TABLE record_index (
 id UUID PRIMARY KEY, project_id UUID, record_type TEXT NOT NULL, created_at TIMESTAMPTZ NOT NULL,
 UNIQUE(project_id,id), UNIQUE(project_id,id,record_type)
);
CREATE TABLE logical_objects (
 id UUID PRIMARY KEY, project_id UUID NOT NULL, object_type TEXT NOT NULL CHECK(object_type IN ('artifact','decision')),
 UNIQUE(project_id,id,object_type)
);
CREATE FUNCTION harness_record_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF TG_OP='DELETE' THEN
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
CREATE FUNCTION harness_record_index() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF TG_OP='INSERT' THEN
  INSERT INTO record_index(id,project_id,record_type,created_at)
  VALUES(NEW.id,NEW.project_id,NEW.data->>'record_type',NEW.created_at);
 ELSIF TG_OP='DELETE' THEN
  DELETE FROM record_index WHERE id=OLD.id;
 END IF;
 RETURN NULL;
END $$;

CREATE TABLE projects (
 id UUID PRIMARY KEY,
 project_id UUID,
 data JSONB NOT NULL,
 created_at TIMESTAMPTZ NOT NULL,
 updated_at TIMESTAMPTZ,
 row_version BIGINT,
 CHECK(data->>'record_type'='project'),
 CHECK(data->>'schema_version'='1.0.0'),
 UNIQUE(project_id,id),
 CHECK(project_id IS NOT NULL),
 CHECK(project_id=id),
 owner_account_id TEXT GENERATED ALWAYS AS ((data->>'owner_account_id')::TEXT) STORED,
 title TEXT GENERATED ALWAYS AS ((data->>'title')::TEXT) STORED,
 state TEXT GENERATED ALWAYS AS ((data->>'state')::TEXT) STORED
);

CREATE TRIGGER guard_projects BEFORE INSERT OR UPDATE OR DELETE ON projects FOR EACH ROW EXECUTE FUNCTION harness_record_guard();

CREATE TRIGGER index_projects AFTER INSERT OR DELETE ON projects FOR EACH ROW EXECUTE FUNCTION harness_record_index();

ALTER TABLE projects ADD FOREIGN KEY(project_id) REFERENCES projects(id) DEFERRABLE INITIALLY DEFERRED;

CREATE INDEX projects_project_created ON projects(project_id,created_at,id);

CREATE TABLE conversations (
 id UUID PRIMARY KEY,
 project_id UUID,
 data JSONB NOT NULL,
 created_at TIMESTAMPTZ NOT NULL,
 updated_at TIMESTAMPTZ,
 row_version BIGINT,
 CHECK(data->>'record_type'='conversation'),
 CHECK(data->>'schema_version'='1.0.0'),
 UNIQUE(project_id,id),
 CHECK(project_id IS NOT NULL),
 title TEXT GENERATED ALWAYS AS ((data->>'title')::TEXT) STORED,
 state TEXT GENERATED ALWAYS AS ((data->>'state')::TEXT) STORED,
 last_message_seq BIGINT GENERATED ALWAYS AS ((data->>'last_message_seq')::BIGINT) STORED
);

CREATE TRIGGER guard_conversations BEFORE INSERT OR UPDATE OR DELETE ON conversations FOR EACH ROW EXECUTE FUNCTION harness_record_guard();

CREATE TRIGGER index_conversations AFTER INSERT OR DELETE ON conversations FOR EACH ROW EXECUTE FUNCTION harness_record_index();

ALTER TABLE conversations ADD FOREIGN KEY(project_id) REFERENCES projects(id) DEFERRABLE INITIALLY DEFERRED;

CREATE INDEX conversations_project_created ON conversations(project_id,created_at,id);

CREATE TABLE history_records (
 id UUID PRIMARY KEY,
 project_id UUID,
 data JSONB NOT NULL,
 created_at TIMESTAMPTZ NOT NULL,
 updated_at TIMESTAMPTZ,
 row_version BIGINT,
 CHECK(data->>'record_type'='history_record'),
 CHECK(data->>'schema_version'='1.0.0'),
 UNIQUE(project_id,id),
 CHECK(project_id IS NOT NULL),
 conversation_id UUID GENERATED ALWAYS AS ((data->>'conversation_id')::UUID) STORED,
 sequence BIGINT GENERATED ALWAYS AS ((data->>'sequence')::BIGINT) STORED,
 session_id UUID GENERATED ALWAYS AS ((data->>'session_id')::UUID) STORED,
 task_id UUID GENERATED ALWAYS AS ((data->>'task_id')::UUID) STORED,
 run_id UUID GENERATED ALWAYS AS ((data->>'run_id')::UUID) STORED,
 role TEXT GENERATED ALWAYS AS ((data->>'role')::TEXT) STORED,
 visibility TEXT GENERATED ALWAYS AS ((data->>'visibility')::TEXT) STORED,
 kind TEXT GENERATED ALWAYS AS ((data->>'kind')::TEXT) STORED,
 source_message_id UUID GENERATED ALWAYS AS ((data->>'source_message_id')::UUID) STORED,
 operation_id UUID GENERATED ALWAYS AS ((data->>'operation_id')::UUID) STORED
);

CREATE TRIGGER guard_history_records BEFORE INSERT OR UPDATE OR DELETE ON history_records FOR EACH ROW EXECUTE FUNCTION harness_record_guard();

CREATE TRIGGER index_history_records AFTER INSERT OR DELETE ON history_records FOR EACH ROW EXECUTE FUNCTION harness_record_index();

ALTER TABLE history_records ADD FOREIGN KEY(project_id) REFERENCES projects(id) DEFERRABLE INITIALLY DEFERRED;

CREATE INDEX history_records_project_created ON history_records(project_id,created_at,id);

CREATE TABLE work_sessions (
 id UUID PRIMARY KEY,
 project_id UUID,
 data JSONB NOT NULL,
 created_at TIMESTAMPTZ NOT NULL,
 updated_at TIMESTAMPTZ,
 row_version BIGINT,
 CHECK(data->>'record_type'='work_session'),
 CHECK(data->>'schema_version'='1.0.0'),
 UNIQUE(project_id,id),
 CHECK(project_id IS NOT NULL),
 conversation_id UUID GENERATED ALWAYS AS ((data->>'conversation_id')::UUID) STORED,
 session_key TEXT GENERATED ALWAYS AS ((data->>'session_key')::TEXT) STORED,
 state TEXT GENERATED ALWAYS AS ((data->>'state')::TEXT) STORED,
 generation BIGINT GENERATED ALWAYS AS ((data->>'generation')::BIGINT) STORED,
 last_item_seq BIGINT GENERATED ALWAYS AS ((data->>'last_item_seq')::BIGINT) STORED,
 fencing_token BIGINT GENERATED ALWAYS AS ((data->>'fencing_token')::BIGINT) STORED
);

CREATE TRIGGER guard_work_sessions BEFORE INSERT OR UPDATE OR DELETE ON work_sessions FOR EACH ROW EXECUTE FUNCTION harness_record_guard();

CREATE TRIGGER index_work_sessions AFTER INSERT OR DELETE ON work_sessions FOR EACH ROW EXECUTE FUNCTION harness_record_index();

ALTER TABLE work_sessions ADD FOREIGN KEY(project_id) REFERENCES projects(id) DEFERRABLE INITIALLY DEFERRED;

CREATE INDEX work_sessions_project_created ON work_sessions(project_id,created_at,id);

CREATE TABLE session_items (
 id UUID PRIMARY KEY,
 project_id UUID,
 data JSONB NOT NULL,
 created_at TIMESTAMPTZ NOT NULL,
 updated_at TIMESTAMPTZ,
 row_version BIGINT,
 CHECK(data->>'record_type'='session_item'),
 CHECK(data->>'schema_version'='1.0.0'),
 UNIQUE(project_id,id),
 CHECK(project_id IS NOT NULL),
 session_id UUID GENERATED ALWAYS AS ((data->>'session_id')::UUID) STORED,
 generation BIGINT GENERATED ALWAYS AS ((data->>'generation')::BIGINT) STORED,
 sequence BIGINT GENERATED ALWAYS AS ((data->>'sequence')::BIGINT) STORED
);

CREATE TRIGGER guard_session_items BEFORE INSERT OR UPDATE OR DELETE ON session_items FOR EACH ROW EXECUTE FUNCTION harness_record_guard();

CREATE TRIGGER index_session_items AFTER INSERT OR DELETE ON session_items FOR EACH ROW EXECUTE FUNCTION harness_record_index();

ALTER TABLE session_items ADD FOREIGN KEY(project_id) REFERENCES projects(id) DEFERRABLE INITIALLY DEFERRED;

CREATE INDEX session_items_project_created ON session_items(project_id,created_at,id);

CREATE TABLE artifacts (
 id UUID PRIMARY KEY,
 project_id UUID,
 data JSONB NOT NULL,
 created_at TIMESTAMPTZ NOT NULL,
 updated_at TIMESTAMPTZ,
 row_version BIGINT,
 CHECK(data->>'record_type'='artifact'),
 CHECK(data->>'schema_version'='1.0.0'),
 UNIQUE(project_id,id),
 CHECK(project_id IS NOT NULL),
 artifact_kind TEXT GENERATED ALWAYS AS ((data->>'artifact_kind')::TEXT) STORED,
 latest_version BIGINT GENERATED ALWAYS AS ((data->>'latest_version')::BIGINT) STORED
);

CREATE TRIGGER guard_artifacts BEFORE INSERT OR UPDATE OR DELETE ON artifacts FOR EACH ROW EXECUTE FUNCTION harness_record_guard();

CREATE TRIGGER index_artifacts AFTER INSERT OR DELETE ON artifacts FOR EACH ROW EXECUTE FUNCTION harness_record_index();

ALTER TABLE artifacts ADD FOREIGN KEY(project_id) REFERENCES projects(id) DEFERRABLE INITIALLY DEFERRED;

CREATE INDEX artifacts_project_created ON artifacts(project_id,created_at,id);

CREATE TABLE artifact_versions (
 id UUID PRIMARY KEY,
 project_id UUID,
 data JSONB NOT NULL,
 created_at TIMESTAMPTZ NOT NULL,
 updated_at TIMESTAMPTZ,
 row_version BIGINT,
 CHECK(data->>'record_type'='artifact_version'),
 CHECK(data->>'schema_version'='1.0.0'),
 UNIQUE(project_id,id),
 CHECK(project_id IS NOT NULL),
 artifact_id UUID GENERATED ALWAYS AS ((data->>'artifact_id')::UUID) STORED,
 version BIGINT GENERATED ALWAYS AS ((data->>'version')::BIGINT) STORED,
 content_sha256 TEXT GENERATED ALWAYS AS ((data->>'content_sha256')::TEXT) STORED,
 producer_run_id UUID GENERATED ALWAYS AS ((data->>'producer_run_id')::UUID) STORED,
 config_version_id UUID GENERATED ALWAYS AS ((data->>'config_version_id')::UUID) STORED,
 origin TEXT GENERATED ALWAYS AS ((data->>'origin')::TEXT) STORED
);

CREATE TRIGGER guard_artifact_versions BEFORE INSERT OR UPDATE OR DELETE ON artifact_versions FOR EACH ROW EXECUTE FUNCTION harness_record_guard();

CREATE TRIGGER index_artifact_versions AFTER INSERT OR DELETE ON artifact_versions FOR EACH ROW EXECUTE FUNCTION harness_record_index();

ALTER TABLE artifact_versions ADD FOREIGN KEY(project_id) REFERENCES projects(id) DEFERRABLE INITIALLY DEFERRED;

CREATE INDEX artifact_versions_project_created ON artifact_versions(project_id,created_at,id);

CREATE TABLE artifact_states (
 id UUID PRIMARY KEY,
 project_id UUID,
 data JSONB NOT NULL,
 created_at TIMESTAMPTZ NOT NULL,
 updated_at TIMESTAMPTZ,
 row_version BIGINT,
 CHECK(data->>'record_type'='artifact_state'),
 CHECK(data->>'schema_version'='1.0.0'),
 UNIQUE(project_id,id),
 CHECK(project_id IS NOT NULL),
 artifact_id UUID GENERATED ALWAYS AS ((data->>'artifact_id')::UUID) STORED,
 artifact_version_id UUID GENERATED ALWAYS AS ((data->>'artifact_version_id')::UUID) STORED,
 version BIGINT GENERATED ALWAYS AS ((data->>'version')::BIGINT) STORED,
 confirmation_status TEXT GENERATED ALWAYS AS ((data->>'confirmation_status')::TEXT) STORED,
 dependency_status TEXT GENERATED ALWAYS AS ((data->>'dependency_status')::TEXT) STORED,
 quality_status TEXT GENERATED ALWAYS AS ((data->>'quality_status')::TEXT) STORED
);

CREATE TRIGGER guard_artifact_states BEFORE INSERT OR UPDATE OR DELETE ON artifact_states FOR EACH ROW EXECUTE FUNCTION harness_record_guard();

CREATE TRIGGER index_artifact_states AFTER INSERT OR DELETE ON artifact_states FOR EACH ROW EXECUTE FUNCTION harness_record_index();

ALTER TABLE artifact_states ADD FOREIGN KEY(project_id) REFERENCES projects(id) DEFERRABLE INITIALLY DEFERRED;

CREATE INDEX artifact_states_project_created ON artifact_states(project_id,created_at,id);

CREATE TABLE decisions (
 id UUID PRIMARY KEY,
 project_id UUID,
 data JSONB NOT NULL,
 created_at TIMESTAMPTZ NOT NULL,
 updated_at TIMESTAMPTZ,
 row_version BIGINT,
 CHECK(data->>'record_type'='decision'),
 CHECK(data->>'schema_version'='1.0.0'),
 UNIQUE(project_id,id),
 CHECK(project_id IS NOT NULL),
 decision_id UUID GENERATED ALWAYS AS ((data->>'decision_id')::UUID) STORED,
 version BIGINT GENERATED ALWAYS AS ((data->>'version')::BIGINT) STORED,
 topic TEXT GENERATED ALWAYS AS ((data->>'topic')::TEXT) STORED,
 origin TEXT GENERATED ALWAYS AS ((data->>'origin')::TEXT) STORED,
 status TEXT GENERATED ALWAYS AS ((data->>'status')::TEXT) STORED
);

CREATE TRIGGER guard_decisions BEFORE INSERT OR UPDATE OR DELETE ON decisions FOR EACH ROW EXECUTE FUNCTION harness_record_guard();

CREATE TRIGGER index_decisions AFTER INSERT OR DELETE ON decisions FOR EACH ROW EXECUTE FUNCTION harness_record_index();

ALTER TABLE decisions ADD FOREIGN KEY(project_id) REFERENCES projects(id) DEFERRABLE INITIALLY DEFERRED;

CREATE INDEX decisions_project_created ON decisions(project_id,created_at,id);

CREATE TABLE confirmations (
 id UUID PRIMARY KEY,
 project_id UUID,
 data JSONB NOT NULL,
 created_at TIMESTAMPTZ NOT NULL,
 updated_at TIMESTAMPTZ,
 row_version BIGINT,
 CHECK(data->>'record_type'='confirmation'),
 CHECK(data->>'schema_version'='1.0.0'),
 UNIQUE(project_id,id),
 CHECK(project_id IS NOT NULL),
 action TEXT GENERATED ALWAYS AS ((data->>'action')::TEXT) STORED,
 basis TEXT GENERATED ALWAYS AS ((data->>'basis')::TEXT) STORED,
 applied_run_id UUID GENERATED ALWAYS AS ((data->>'applied_run_id')::UUID) STORED
);

CREATE TRIGGER guard_confirmations BEFORE INSERT OR UPDATE OR DELETE ON confirmations FOR EACH ROW EXECUTE FUNCTION harness_record_guard();

CREATE TRIGGER index_confirmations AFTER INSERT OR DELETE ON confirmations FOR EACH ROW EXECUTE FUNCTION harness_record_index();

ALTER TABLE confirmations ADD FOREIGN KEY(project_id) REFERENCES projects(id) DEFERRABLE INITIALLY DEFERRED;

CREATE INDEX confirmations_project_created ON confirmations(project_id,created_at,id);

CREATE TABLE tasks (
 id UUID PRIMARY KEY,
 project_id UUID,
 data JSONB NOT NULL,
 created_at TIMESTAMPTZ NOT NULL,
 updated_at TIMESTAMPTZ,
 row_version BIGINT,
 CHECK(data->>'record_type'='task'),
 CHECK(data->>'schema_version'='1.0.0'),
 UNIQUE(project_id,id),
 CHECK(project_id IS NOT NULL),
 conversation_id UUID GENERATED ALWAYS AS ((data->>'conversation_id')::UUID) STORED,
 parent_task_id UUID GENERATED ALWAYS AS ((data->>'parent_task_id')::UUID) STORED,
 budget_root_task_id UUID GENERATED ALWAYS AS ((data->>'budget_root_task_id')::UUID) STORED,
 requested_by_message_id UUID GENERATED ALWAYS AS ((data->>'requested_by_message_id')::UUID) STORED,
 intent TEXT GENERATED ALWAYS AS ((data->>'intent')::TEXT) STORED,
 state TEXT GENERATED ALWAYS AS ((data->>'state')::TEXT) STORED,
 repair_rounds_used BIGINT GENERATED ALWAYS AS ((data->>'repair_rounds_used')::BIGINT) STORED,
 current_run_id UUID GENERATED ALWAYS AS ((data->>'current_run_id')::UUID) STORED,
 latest_checkpoint_id UUID GENERATED ALWAYS AS ((data->>'latest_checkpoint_id')::UUID) STORED
);

CREATE TRIGGER guard_tasks BEFORE INSERT OR UPDATE OR DELETE ON tasks FOR EACH ROW EXECUTE FUNCTION harness_record_guard();

CREATE TRIGGER index_tasks AFTER INSERT OR DELETE ON tasks FOR EACH ROW EXECUTE FUNCTION harness_record_index();

ALTER TABLE tasks ADD FOREIGN KEY(project_id) REFERENCES projects(id) DEFERRABLE INITIALLY DEFERRED;

CREATE INDEX tasks_project_created ON tasks(project_id,created_at,id);

CREATE TABLE runs (
 id UUID PRIMARY KEY,
 project_id UUID,
 data JSONB NOT NULL,
 created_at TIMESTAMPTZ NOT NULL,
 updated_at TIMESTAMPTZ,
 row_version BIGINT,
 CHECK(data->>'record_type'='run'),
 CHECK(data->>'schema_version'='1.0.0'),
 UNIQUE(project_id,id),
 CHECK(project_id IS NOT NULL),
 task_id UUID GENERATED ALWAYS AS ((data->>'task_id')::UUID) STORED,
 agent_key TEXT GENERATED ALWAYS AS ((data->>'agent_key')::TEXT) STORED,
 session_id UUID GENERATED ALWAYS AS ((data->>'session_id')::UUID) STORED,
 state TEXT GENERATED ALWAYS AS ((data->>'state')::TEXT) STORED,
 config_version_id UUID GENERATED ALWAYS AS ((data->>'config_version_id')::UUID) STORED,
 max_turns BIGINT GENERATED ALWAYS AS ((data->>'max_turns')::BIGINT) STORED,
 model_turns_used BIGINT GENERATED ALWAYS AS ((data->>'model_turns_used')::BIGINT) STORED,
 latest_checkpoint_id UUID GENERATED ALWAYS AS ((data->>'latest_checkpoint_id')::UUID) STORED,
 fencing_token BIGINT GENERATED ALWAYS AS ((data->>'fencing_token')::BIGINT) STORED,
 execution_kind TEXT GENERATED ALWAYS AS ((data->>'execution_kind')::TEXT) STORED
);

CREATE TRIGGER guard_runs BEFORE INSERT OR UPDATE OR DELETE ON runs FOR EACH ROW EXECUTE FUNCTION harness_record_guard();

CREATE TRIGGER index_runs AFTER INSERT OR DELETE ON runs FOR EACH ROW EXECUTE FUNCTION harness_record_index();

ALTER TABLE runs ADD FOREIGN KEY(project_id) REFERENCES projects(id) DEFERRABLE INITIALLY DEFERRED;

CREATE INDEX runs_project_created ON runs(project_id,created_at,id);

CREATE TABLE queued_requests (
 id UUID PRIMARY KEY,
 project_id UUID,
 data JSONB NOT NULL,
 created_at TIMESTAMPTZ NOT NULL,
 updated_at TIMESTAMPTZ,
 row_version BIGINT,
 CHECK(data->>'record_type'='queued_request'),
 CHECK(data->>'schema_version'='1.0.0'),
 UNIQUE(project_id,id),
 CHECK(project_id IS NOT NULL),
 conversation_id UUID GENERATED ALWAYS AS ((data->>'conversation_id')::UUID) STORED,
 source_message_id UUID GENERATED ALWAYS AS ((data->>'source_message_id')::UUID) STORED,
 mode TEXT GENERATED ALWAYS AS ((data->>'mode')::TEXT) STORED,
 target_run_id UUID GENERATED ALWAYS AS ((data->>'target_run_id')::UUID) STORED,
 sequence BIGINT GENERATED ALWAYS AS ((data->>'sequence')::BIGINT) STORED,
 state TEXT GENERATED ALWAYS AS ((data->>'state')::TEXT) STORED,
 adopted_run_id UUID GENERATED ALWAYS AS ((data->>'adopted_run_id')::UUID) STORED,
 adopted_context_snapshot_id UUID GENERATED ALWAYS AS ((data->>'adopted_context_snapshot_id')::UUID) STORED,
 created_task_id UUID GENERATED ALWAYS AS ((data->>'created_task_id')::UUID) STORED
);

CREATE TRIGGER guard_queued_requests BEFORE INSERT OR UPDATE OR DELETE ON queued_requests FOR EACH ROW EXECUTE FUNCTION harness_record_guard();

CREATE TRIGGER index_queued_requests AFTER INSERT OR DELETE ON queued_requests FOR EACH ROW EXECUTE FUNCTION harness_record_index();

ALTER TABLE queued_requests ADD FOREIGN KEY(project_id) REFERENCES projects(id) DEFERRABLE INITIALLY DEFERRED;

CREATE INDEX queued_requests_project_created ON queued_requests(project_id,created_at,id);

CREATE TABLE checkpoints (
 id UUID PRIMARY KEY,
 project_id UUID,
 data JSONB NOT NULL,
 created_at TIMESTAMPTZ NOT NULL,
 updated_at TIMESTAMPTZ,
 row_version BIGINT,
 CHECK(data->>'record_type'='checkpoint'),
 CHECK(data->>'schema_version'='1.0.0'),
 UNIQUE(project_id,id),
 CHECK(project_id IS NOT NULL),
 task_id UUID GENERATED ALWAYS AS ((data->>'task_id')::UUID) STORED,
 run_id UUID GENERATED ALWAYS AS ((data->>'run_id')::UUID) STORED,
 session_id UUID GENERATED ALWAYS AS ((data->>'session_id')::UUID) STORED,
 session_generation BIGINT GENERATED ALWAYS AS ((data->>'session_generation')::BIGINT) STORED,
 config_version_id UUID GENERATED ALWAYS AS ((data->>'config_version_id')::UUID) STORED,
 repair_rounds_used BIGINT GENERATED ALWAYS AS ((data->>'repair_rounds_used')::BIGINT) STORED
);

CREATE TRIGGER guard_checkpoints BEFORE INSERT OR UPDATE OR DELETE ON checkpoints FOR EACH ROW EXECUTE FUNCTION harness_record_guard();

CREATE TRIGGER index_checkpoints AFTER INSERT OR DELETE ON checkpoints FOR EACH ROW EXECUTE FUNCTION harness_record_index();

ALTER TABLE checkpoints ADD FOREIGN KEY(project_id) REFERENCES projects(id) DEFERRABLE INITIALLY DEFERRED;

CREATE INDEX checkpoints_project_created ON checkpoints(project_id,created_at,id);

CREATE TABLE config_versions (
 id UUID PRIMARY KEY,
 project_id UUID,
 data JSONB NOT NULL,
 created_at TIMESTAMPTZ NOT NULL,
 updated_at TIMESTAMPTZ,
 row_version BIGINT,
 CHECK(data->>'record_type'='config_version'),
 CHECK(data->>'schema_version'='1.0.0'),
 UNIQUE(project_id,id),
 CHECK((project_id IS NULL) = (data->>'scope_kind'='global')),
 config_key TEXT GENERATED ALWAYS AS ((data->>'config_key')::TEXT) STORED,
 version BIGINT GENERATED ALWAYS AS ((data->>'version')::BIGINT) STORED,
 scope_kind TEXT GENERATED ALWAYS AS ((data->>'scope_kind')::TEXT) STORED,
 state TEXT GENERATED ALWAYS AS ((data->>'state')::TEXT) STORED,
 parent_config_id UUID GENERATED ALWAYS AS ((data->>'parent_config_id')::UUID) STORED,
 sha256 TEXT GENERATED ALWAYS AS ((data->>'sha256')::TEXT) STORED
);

CREATE TRIGGER guard_config_versions BEFORE INSERT OR UPDATE OR DELETE ON config_versions FOR EACH ROW EXECUTE FUNCTION harness_record_guard();

CREATE TRIGGER index_config_versions AFTER INSERT OR DELETE ON config_versions FOR EACH ROW EXECUTE FUNCTION harness_record_index();

ALTER TABLE config_versions ADD FOREIGN KEY(project_id) REFERENCES projects(id) DEFERRABLE INITIALLY DEFERRED;

CREATE INDEX config_versions_project_created ON config_versions(project_id,created_at,id);

CREATE TABLE context_snapshots (
 id UUID PRIMARY KEY,
 project_id UUID,
 data JSONB NOT NULL,
 created_at TIMESTAMPTZ NOT NULL,
 updated_at TIMESTAMPTZ,
 row_version BIGINT,
 CHECK(data->>'record_type'='context_snapshot'),
 CHECK(data->>'schema_version'='1.0.0'),
 UNIQUE(project_id,id),
 CHECK(project_id IS NOT NULL),
 task_id UUID GENERATED ALWAYS AS ((data->>'task_id')::UUID) STORED,
 run_id UUID GENERATED ALWAYS AS ((data->>'run_id')::UUID) STORED,
 session_id UUID GENERATED ALWAYS AS ((data->>'session_id')::UUID) STORED,
 config_version_id UUID GENERATED ALWAYS AS ((data->>'config_version_id')::UUID) STORED,
 model TEXT GENERATED ALWAYS AS ((data->>'model')::TEXT) STORED,
 input_token_budget BIGINT GENERATED ALWAYS AS ((data->>'input_token_budget')::BIGINT) STORED,
 content_sha256 TEXT GENERATED ALWAYS AS ((data->>'content_sha256')::TEXT) STORED
);

CREATE TRIGGER guard_context_snapshots BEFORE INSERT OR UPDATE OR DELETE ON context_snapshots FOR EACH ROW EXECUTE FUNCTION harness_record_guard();

CREATE TRIGGER index_context_snapshots AFTER INSERT OR DELETE ON context_snapshots FOR EACH ROW EXECUTE FUNCTION harness_record_index();

ALTER TABLE context_snapshots ADD FOREIGN KEY(project_id) REFERENCES projects(id) DEFERRABLE INITIALLY DEFERRED;

CREATE INDEX context_snapshots_project_created ON context_snapshots(project_id,created_at,id);

CREATE TABLE model_calls (
 id UUID PRIMARY KEY,
 project_id UUID,
 data JSONB NOT NULL,
 created_at TIMESTAMPTZ NOT NULL,
 updated_at TIMESTAMPTZ,
 row_version BIGINT,
 CHECK(data->>'record_type'='model_call'),
 CHECK(data->>'schema_version'='1.0.0'),
 UNIQUE(project_id,id),
 CHECK(project_id IS NOT NULL),
 task_id UUID GENERATED ALWAYS AS ((data->>'task_id')::UUID) STORED,
 run_id UUID GENERATED ALWAYS AS ((data->>'run_id')::UUID) STORED,
 operation_id UUID GENERATED ALWAYS AS ((data->>'operation_id')::UUID) STORED,
 attempt BIGINT GENERATED ALWAYS AS ((data->>'attempt')::BIGINT) STORED,
 turn_index BIGINT GENERATED ALWAYS AS ((data->>'turn_index')::BIGINT) STORED,
 context_snapshot_id UUID GENERATED ALWAYS AS ((data->>'context_snapshot_id')::UUID) STORED,
 state TEXT GENERATED ALWAYS AS ((data->>'state')::TEXT) STORED
);

CREATE TRIGGER guard_model_calls BEFORE INSERT OR UPDATE OR DELETE ON model_calls FOR EACH ROW EXECUTE FUNCTION harness_record_guard();

CREATE TRIGGER index_model_calls AFTER INSERT OR DELETE ON model_calls FOR EACH ROW EXECUTE FUNCTION harness_record_index();

ALTER TABLE model_calls ADD FOREIGN KEY(project_id) REFERENCES projects(id) DEFERRABLE INITIALLY DEFERRED;

CREATE INDEX model_calls_project_created ON model_calls(project_id,created_at,id);

CREATE TABLE tool_calls (
 id UUID PRIMARY KEY,
 project_id UUID,
 data JSONB NOT NULL,
 created_at TIMESTAMPTZ NOT NULL,
 updated_at TIMESTAMPTZ,
 row_version BIGINT,
 CHECK(data->>'record_type'='tool_call'),
 CHECK(data->>'schema_version'='1.0.0'),
 UNIQUE(project_id,id),
 CHECK(project_id IS NOT NULL),
 task_id UUID GENERATED ALWAYS AS ((data->>'task_id')::UUID) STORED,
 run_id UUID GENERATED ALWAYS AS ((data->>'run_id')::UUID) STORED,
 operation_id UUID GENERATED ALWAYS AS ((data->>'operation_id')::UUID) STORED,
 provider_tool_call_id TEXT GENERATED ALWAYS AS ((data->>'provider_tool_call_id')::TEXT) STORED,
 tool_name TEXT GENERATED ALWAYS AS ((data->>'tool_name')::TEXT) STORED,
 state TEXT GENERATED ALWAYS AS ((data->>'state')::TEXT) STORED,
 attempt BIGINT GENERATED ALWAYS AS ((data->>'attempt')::BIGINT) STORED,
 model_call_id UUID GENERATED ALWAYS AS ((data->>'model_call_id')::UUID) STORED
);

CREATE TRIGGER guard_tool_calls BEFORE INSERT OR UPDATE OR DELETE ON tool_calls FOR EACH ROW EXECUTE FUNCTION harness_record_guard();

CREATE TRIGGER index_tool_calls AFTER INSERT OR DELETE ON tool_calls FOR EACH ROW EXECUTE FUNCTION harness_record_index();

ALTER TABLE tool_calls ADD FOREIGN KEY(project_id) REFERENCES projects(id) DEFERRABLE INITIALLY DEFERRED;

CREATE INDEX tool_calls_project_created ON tool_calls(project_id,created_at,id);

CREATE TABLE dependencies (
 id UUID PRIMARY KEY,
 project_id UUID,
 data JSONB NOT NULL,
 created_at TIMESTAMPTZ NOT NULL,
 updated_at TIMESTAMPTZ,
 row_version BIGINT,
 CHECK(data->>'record_type'='dependency'),
 CHECK(data->>'schema_version'='1.0.0'),
 UNIQUE(project_id,id),
 CHECK(project_id IS NOT NULL),
 relation TEXT GENERATED ALWAYS AS ((data->>'relation')::TEXT) STORED,
 state TEXT GENERATED ALWAYS AS ((data->>'state')::TEXT) STORED
);

CREATE TRIGGER guard_dependencies BEFORE INSERT OR UPDATE OR DELETE ON dependencies FOR EACH ROW EXECUTE FUNCTION harness_record_guard();

CREATE TRIGGER index_dependencies AFTER INSERT OR DELETE ON dependencies FOR EACH ROW EXECUTE FUNCTION harness_record_index();

ALTER TABLE dependencies ADD FOREIGN KEY(project_id) REFERENCES projects(id) DEFERRABLE INITIALLY DEFERRED;

CREATE INDEX dependencies_project_created ON dependencies(project_id,created_at,id);

CREATE TABLE blobs (
 id UUID PRIMARY KEY,
 project_id UUID,
 data JSONB NOT NULL,
 created_at TIMESTAMPTZ NOT NULL,
 updated_at TIMESTAMPTZ,
 row_version BIGINT,
 CHECK(data->>'record_type'='blob'),
 CHECK(data->>'schema_version'='1.0.0'),
 UNIQUE(project_id,id),
 CHECK(project_id IS NOT NULL),
 filename TEXT GENERATED ALWAYS AS ((data->>'filename')::TEXT) STORED,
 media_type TEXT GENERATED ALWAYS AS ((data->>'media_type')::TEXT) STORED,
 byte_size BIGINT GENERATED ALWAYS AS ((data->>'byte_size')::BIGINT) STORED,
 sha256 TEXT GENERATED ALWAYS AS ((data->>'sha256')::TEXT) STORED,
 storage_key TEXT GENERATED ALWAYS AS ((data->>'storage_key')::TEXT) STORED,
 state TEXT GENERATED ALWAYS AS ((data->>'state')::TEXT) STORED
);

CREATE TRIGGER guard_blobs BEFORE INSERT OR UPDATE OR DELETE ON blobs FOR EACH ROW EXECUTE FUNCTION harness_record_guard();

CREATE TRIGGER index_blobs AFTER INSERT OR DELETE ON blobs FOR EACH ROW EXECUTE FUNCTION harness_record_index();

ALTER TABLE blobs ADD FOREIGN KEY(project_id) REFERENCES projects(id) DEFERRABLE INITIALLY DEFERRED;

CREATE INDEX blobs_project_created ON blobs(project_id,created_at,id);

CREATE TABLE runtime_events (
 id UUID PRIMARY KEY,
 project_id UUID,
 data JSONB NOT NULL,
 created_at TIMESTAMPTZ NOT NULL,
 updated_at TIMESTAMPTZ,
 row_version BIGINT,
 CHECK(data->>'record_type'='runtime_event'),
 CHECK(data->>'schema_version'='1.0.0'),
 UNIQUE(project_id,id),
 CHECK(project_id IS NOT NULL),
 sequence BIGINT GENERATED ALWAYS AS ((data->>'sequence')::BIGINT) STORED,
 conversation_id UUID GENERATED ALWAYS AS ((data->>'conversation_id')::UUID) STORED,
 task_id UUID GENERATED ALWAYS AS ((data->>'task_id')::UUID) STORED,
 run_id UUID GENERATED ALWAYS AS ((data->>'run_id')::UUID) STORED,
 event_name TEXT GENERATED ALWAYS AS ((data->>'event_name')::TEXT) STORED,
 actor_kind TEXT GENERATED ALWAYS AS ((data->>'actor_kind')::TEXT) STORED,
 causation_id UUID GENERATED ALWAYS AS ((data->>'causation_id')::UUID) STORED
);

CREATE TRIGGER guard_runtime_events BEFORE INSERT OR UPDATE OR DELETE ON runtime_events FOR EACH ROW EXECUTE FUNCTION harness_record_guard();

CREATE TRIGGER index_runtime_events AFTER INSERT OR DELETE ON runtime_events FOR EACH ROW EXECUTE FUNCTION harness_record_index();

ALTER TABLE runtime_events ADD FOREIGN KEY(project_id) REFERENCES projects(id) DEFERRABLE INITIALLY DEFERRED;

CREATE INDEX runtime_events_project_created ON runtime_events(project_id,created_at,id);

CREATE TABLE idempotency_records (
 id UUID PRIMARY KEY,
 project_id UUID,
 data JSONB NOT NULL,
 created_at TIMESTAMPTZ NOT NULL,
 updated_at TIMESTAMPTZ,
 row_version BIGINT,
 CHECK(data->>'record_type'='idempotency_record'),
 CHECK(data->>'schema_version'='1.0.0'),
 UNIQUE(project_id,id),
 CHECK(project_id IS NOT NULL),
 account_id TEXT GENERATED ALWAYS AS ((data->>'account_id')::TEXT) STORED,
 operation TEXT GENERATED ALWAYS AS ((data->>'operation')::TEXT) STORED,
 key TEXT GENERATED ALWAYS AS ((data->>'key')::TEXT) STORED,
 request_sha256 TEXT GENERATED ALWAYS AS ((data->>'request_sha256')::TEXT) STORED,
 state TEXT GENERATED ALWAYS AS ((data->>'state')::TEXT) STORED
);

CREATE TRIGGER guard_idempotency_records BEFORE INSERT OR UPDATE OR DELETE ON idempotency_records FOR EACH ROW EXECUTE FUNCTION harness_record_guard();

CREATE TRIGGER index_idempotency_records AFTER INSERT OR DELETE ON idempotency_records FOR EACH ROW EXECUTE FUNCTION harness_record_index();

ALTER TABLE idempotency_records ADD FOREIGN KEY(project_id) REFERENCES projects(id) DEFERRABLE INITIALLY DEFERRED;

CREATE INDEX idempotency_records_project_created ON idempotency_records(project_id,created_at,id);


ALTER TABLE record_index ADD FOREIGN KEY(project_id) REFERENCES projects(id) DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE logical_objects ADD FOREIGN KEY(project_id) REFERENCES projects(id) DEFERRABLE INITIALLY DEFERRED;
CREATE TABLE record_links (
 project_id UUID NOT NULL, from_id UUID NOT NULL, relation TEXT NOT NULL, ordinal INTEGER NOT NULL,
 to_id UUID NOT NULL, to_type TEXT NOT NULL, target_version BIGINT, selector JSONB,
 PRIMARY KEY(from_id,relation,ordinal),
 FOREIGN KEY(project_id,from_id) REFERENCES record_index(project_id,id) ON DELETE CASCADE DEFERRABLE INITIALLY DEFERRED,
 FOREIGN KEY(project_id,to_id,to_type) REFERENCES record_index(project_id,id,record_type) DEFERRABLE INITIALLY DEFERRED
);
CREATE INDEX record_links_reverse ON record_links(project_id,to_id,relation);
CREATE TABLE config_links (
 consumer_project_id UUID, consumer_id UUID NOT NULL, config_version_id UUID NOT NULL,
 relation TEXT NOT NULL, ordinal INTEGER NOT NULL, PRIMARY KEY(consumer_id,relation,ordinal),
 FOREIGN KEY(consumer_id) REFERENCES record_index(id) ON DELETE CASCADE DEFERRABLE INITIALLY DEFERRED,
 FOREIGN KEY(config_version_id) REFERENCES config_versions(id) DEFERRABLE INITIALLY DEFERRED
);
CREATE TABLE runtime_projections (
 key TEXT PRIMARY KEY, project_id UUID NOT NULL REFERENCES projects(id), value JSONB NOT NULL,
 updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(), row_version BIGINT NOT NULL DEFAULT 1,
 CHECK(value->>'project_id'=project_id::text), CHECK(key LIKE project_id::text || ':%')
);
CREATE UNIQUE INDEX work_sessions_key ON work_sessions(project_id,session_key);
CREATE UNIQUE INDEX session_items_order ON session_items(project_id,session_id,generation,sequence);
CREATE UNIQUE INDEX history_order ON history_records(project_id,conversation_id,sequence);
CREATE UNIQUE INDEX artifact_content_version ON artifact_versions(project_id,artifact_id,version);
CREATE UNIQUE INDEX artifact_status_version ON artifact_states(project_id,artifact_version_id);
CREATE UNIQUE INDEX decision_content_version ON decisions(project_id,decision_id,version);
CREATE UNIQUE INDEX queued_order ON queued_requests(project_id,conversation_id,sequence);
CREATE UNIQUE INDEX events_order ON runtime_events(project_id,sequence);
CREATE UNIQUE INDEX tool_provider_attempt ON tool_calls(project_id,run_id,provider_tool_call_id,attempt);
CREATE UNIQUE INDEX model_operation_attempt ON model_calls(project_id,operation_id,attempt);
CREATE UNIQUE INDEX tool_operation_attempt ON tool_calls(project_id,operation_id,attempt);
CREATE UNIQUE INDEX idem_key ON idempotency_records(project_id,account_id,operation,key);
CREATE UNIQUE INDEX blob_storage_key ON blobs(storage_key);
CREATE INDEX tasks_dispatch ON tasks(state,created_at);
ALTER TABLE runs ADD COLUMN lease_expires_at TEXT GENERATED ALWAYS AS (data->>'lease_expires_at') STORED;
CREATE INDEX runs_recovery ON runs(state,lease_expires_at);
CREATE INDEX queue_dispatch ON queued_requests(state,sequence);
CREATE UNIQUE INDEX configuration_version ON config_versions(
 COALESCE(project_id,'00000000-0000-0000-0000-000000000000'::uuid),config_key,scope_kind,COALESCE(data->>'scope_key',''),version);
