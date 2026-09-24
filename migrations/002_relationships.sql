-- Scalar relationship projections also enforce project ownership in PostgreSQL.
ALTER TABLE history_records ADD CONSTRAINT fk_history_records_conversation_id FOREIGN KEY(project_id,conversation_id) REFERENCES conversations(project_id,id) DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE history_records ADD CONSTRAINT fk_history_records_session_id FOREIGN KEY(project_id,session_id) REFERENCES work_sessions(project_id,id) DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE history_records ADD CONSTRAINT fk_history_records_task_id FOREIGN KEY(project_id,task_id) REFERENCES tasks(project_id,id) DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE history_records ADD CONSTRAINT fk_history_records_run_id FOREIGN KEY(project_id,run_id) REFERENCES runs(project_id,id) DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE history_records ADD CONSTRAINT fk_history_records_source_message_id FOREIGN KEY(project_id,source_message_id) REFERENCES history_records(project_id,id) DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE work_sessions ADD CONSTRAINT fk_work_sessions_conversation_id FOREIGN KEY(project_id,conversation_id) REFERENCES conversations(project_id,id) DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE session_items ADD CONSTRAINT fk_session_items_session_id FOREIGN KEY(project_id,session_id) REFERENCES work_sessions(project_id,id) DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE artifact_versions ADD CONSTRAINT fk_artifact_versions_artifact_id FOREIGN KEY(project_id,artifact_id) REFERENCES artifacts(project_id,id) DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE artifact_versions ADD CONSTRAINT fk_artifact_versions_producer_run_id FOREIGN KEY(project_id,producer_run_id) REFERENCES runs(project_id,id) DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE artifact_versions ADD FOREIGN KEY(config_version_id) REFERENCES config_versions(id) DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE artifact_states ADD CONSTRAINT fk_artifact_states_artifact_id FOREIGN KEY(project_id,artifact_id) REFERENCES artifacts(project_id,id) DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE artifact_states ADD CONSTRAINT fk_artifact_states_artifact_version_id FOREIGN KEY(project_id,artifact_version_id) REFERENCES artifact_versions(project_id,id) DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE confirmations ADD CONSTRAINT fk_confirmations_applied_run_id FOREIGN KEY(project_id,applied_run_id) REFERENCES runs(project_id,id) DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE tasks ADD CONSTRAINT fk_tasks_conversation_id FOREIGN KEY(project_id,conversation_id) REFERENCES conversations(project_id,id) DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE tasks ADD CONSTRAINT fk_tasks_requested_by_message_id FOREIGN KEY(project_id,requested_by_message_id) REFERENCES history_records(project_id,id) DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE tasks ADD CONSTRAINT fk_tasks_parent_task_id FOREIGN KEY(project_id,parent_task_id) REFERENCES tasks(project_id,id) DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE tasks ADD CONSTRAINT fk_tasks_budget_root_task_id FOREIGN KEY(project_id,budget_root_task_id) REFERENCES tasks(project_id,id) DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE tasks ADD CONSTRAINT fk_tasks_current_run_id FOREIGN KEY(project_id,current_run_id) REFERENCES runs(project_id,id) DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE tasks ADD CONSTRAINT fk_tasks_latest_checkpoint_id FOREIGN KEY(project_id,latest_checkpoint_id) REFERENCES checkpoints(project_id,id) DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE runs ADD CONSTRAINT fk_runs_session_id FOREIGN KEY(project_id,session_id) REFERENCES work_sessions(project_id,id) DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE runs ADD CONSTRAINT fk_runs_task_id FOREIGN KEY(project_id,task_id) REFERENCES tasks(project_id,id) DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE runs ADD CONSTRAINT fk_runs_latest_checkpoint_id FOREIGN KEY(project_id,latest_checkpoint_id) REFERENCES checkpoints(project_id,id) DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE runs ADD FOREIGN KEY(config_version_id) REFERENCES config_versions(id) DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE queued_requests ADD CONSTRAINT fk_queued_requests_conversation_id FOREIGN KEY(project_id,conversation_id) REFERENCES conversations(project_id,id) DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE queued_requests ADD CONSTRAINT fk_queued_requests_source_message_id FOREIGN KEY(project_id,source_message_id) REFERENCES history_records(project_id,id) DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE queued_requests ADD CONSTRAINT fk_queued_requests_target_run_id FOREIGN KEY(project_id,target_run_id) REFERENCES runs(project_id,id) DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE queued_requests ADD CONSTRAINT fk_queued_requests_adopted_run_id FOREIGN KEY(project_id,adopted_run_id) REFERENCES runs(project_id,id) DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE queued_requests ADD CONSTRAINT fk_queued_requests_adopted_context_snapshot_id FOREIGN KEY(project_id,adopted_context_snapshot_id) REFERENCES context_snapshots(project_id,id) DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE queued_requests ADD CONSTRAINT fk_queued_requests_created_task_id FOREIGN KEY(project_id,created_task_id) REFERENCES tasks(project_id,id) DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE checkpoints ADD CONSTRAINT fk_checkpoints_session_id FOREIGN KEY(project_id,session_id) REFERENCES work_sessions(project_id,id) DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE checkpoints ADD CONSTRAINT fk_checkpoints_task_id FOREIGN KEY(project_id,task_id) REFERENCES tasks(project_id,id) DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE checkpoints ADD CONSTRAINT fk_checkpoints_run_id FOREIGN KEY(project_id,run_id) REFERENCES runs(project_id,id) DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE checkpoints ADD FOREIGN KEY(config_version_id) REFERENCES config_versions(id) DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE config_versions ADD FOREIGN KEY(parent_config_id) REFERENCES config_versions(id) DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE context_snapshots ADD CONSTRAINT fk_context_snapshots_session_id FOREIGN KEY(project_id,session_id) REFERENCES work_sessions(project_id,id) DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE context_snapshots ADD CONSTRAINT fk_context_snapshots_task_id FOREIGN KEY(project_id,task_id) REFERENCES tasks(project_id,id) DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE context_snapshots ADD CONSTRAINT fk_context_snapshots_run_id FOREIGN KEY(project_id,run_id) REFERENCES runs(project_id,id) DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE context_snapshots ADD FOREIGN KEY(config_version_id) REFERENCES config_versions(id) DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE model_calls ADD CONSTRAINT fk_model_calls_task_id FOREIGN KEY(project_id,task_id) REFERENCES tasks(project_id,id) DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE model_calls ADD CONSTRAINT fk_model_calls_run_id FOREIGN KEY(project_id,run_id) REFERENCES runs(project_id,id) DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE model_calls ADD CONSTRAINT fk_model_calls_context_snapshot_id FOREIGN KEY(project_id,context_snapshot_id) REFERENCES context_snapshots(project_id,id) DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE tool_calls ADD CONSTRAINT fk_tool_calls_task_id FOREIGN KEY(project_id,task_id) REFERENCES tasks(project_id,id) DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE tool_calls ADD CONSTRAINT fk_tool_calls_run_id FOREIGN KEY(project_id,run_id) REFERENCES runs(project_id,id) DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE tool_calls ADD CONSTRAINT fk_tool_calls_model_call_id FOREIGN KEY(project_id,model_call_id) REFERENCES model_calls(project_id,id) DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE runtime_events ADD CONSTRAINT fk_runtime_events_conversation_id FOREIGN KEY(project_id,conversation_id) REFERENCES conversations(project_id,id) DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE runtime_events ADD CONSTRAINT fk_runtime_events_task_id FOREIGN KEY(project_id,task_id) REFERENCES tasks(project_id,id) DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE runtime_events ADD CONSTRAINT fk_runtime_events_run_id FOREIGN KEY(project_id,run_id) REFERENCES runs(project_id,id) DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE runtime_events ADD CONSTRAINT fk_runtime_events_causation_id FOREIGN KEY(project_id,causation_id) REFERENCES record_index(project_id,id) DEFERRABLE INITIALLY DEFERRED;

CREATE FUNCTION harness_config_link_guard() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE source_project uuid; target_project uuid;
BEGIN
 SELECT project_id INTO source_project FROM record_index WHERE id=NEW.consumer_id;
 SELECT project_id INTO target_project FROM config_versions WHERE id=NEW.config_version_id;
 IF source_project IS DISTINCT FROM NEW.consumer_project_id OR
    (target_project IS NOT NULL AND target_project IS DISTINCT FROM source_project) THEN
  RAISE EXCEPTION 'cross-project configuration reference' USING ERRCODE='23514';
 END IF;
 RETURN NULL;
END $$;
CREATE CONSTRAINT TRIGGER config_link_project_guard AFTER INSERT OR UPDATE ON config_links
 DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION harness_config_link_guard();
CREATE INDEX projects_owner_created ON projects(owner_account_id,created_at);
