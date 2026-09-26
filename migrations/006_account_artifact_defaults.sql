-- Account templates are copied into ordinary project ArtifactVersions at creation.
-- A JSON null is an explicit cleared default; a missing row uses the built-in initial value.
CREATE TABLE account_artifact_defaults (
    account_id TEXT NOT NULL,
    artifact_kind TEXT NOT NULL CHECK (artifact_kind IN ('adaptation_strategy', 'adaptation_plan')),
    content JSONB NOT NULL,
    revision BIGINT NOT NULL DEFAULT 1,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (account_id, artifact_kind)
);
