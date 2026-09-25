-- Metadata-only Agents SDK spans. Full prompts and responses remain in audited records.
CREATE TABLE sdk_trace_spans (
 project_id UUID NOT NULL REFERENCES projects(id),
 run_id UUID NOT NULL REFERENCES runs(id),
 trace_id TEXT NOT NULL,
 span_id TEXT NOT NULL,
 parent_id TEXT,
 kind TEXT NOT NULL,
 name TEXT NOT NULL,
 started_at TIMESTAMPTZ,
 ended_at TIMESTAMPTZ,
 error_code TEXT,
 metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
 PRIMARY KEY (project_id, span_id)
);
CREATE INDEX sdk_trace_spans_run_time ON sdk_trace_spans(project_id,run_id,started_at,span_id);
