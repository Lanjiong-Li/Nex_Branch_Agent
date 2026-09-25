-- Mutable editor working copies are separate from immutable configuration versions.
-- They may contain incomplete schema text and are never used by running tasks.
CREATE TABLE account_config_editor_drafts (
 account_id TEXT NOT NULL,
 scope_kind TEXT NOT NULL,
 scope_key TEXT NOT NULL DEFAULT '',
 revision BIGINT NOT NULL DEFAULT 1,
 payload JSONB NOT NULL,
 updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
 PRIMARY KEY (account_id, scope_kind, scope_key)
);
