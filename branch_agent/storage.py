"""PostgreSQL persistence for the versioned Harness records.

Store is a trusted service boundary, not an authorization endpoint. Public callers
must first authenticate project membership; every read is additionally project-scoped.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import shutil
import threading
from contextlib import contextmanager
from datetime import timezone
from pathlib import Path
from uuid import UUID, uuid4

import psycopg
from psycopg import sql
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from .records import DEFINITIONS, KINDS, ROOT, canonical_bytes, content_hash, new_record, now_utc, validate_record


class StorageError(ValueError):
    pass


class InvalidRecord(StorageError):
    pass


class VersionConflict(StorageError):
    pass


class ImmutableRecord(StorageError):
    pass


class RecordConflict(StorageError):
    pass


# Keep table names explicit even if the source Schema is reordered.
TABLES = {kind: {"history_record": "history_records", "work_session": "work_sessions",
                "session_item": "session_items", "artifact_version": "artifact_versions",
                "artifact_state": "artifact_states", "queued_request": "queued_requests",
                "config_version": "config_versions", "context_snapshot": "context_snapshots",
                "model_call": "model_calls", "tool_call": "tool_calls", "dependency": "dependencies",
                "runtime_event": "runtime_events", "idempotency_record": "idempotency_records"}.get(kind, kind + "s")
          for kind in KINDS}

MUTABLE = {
    "project": {"title", "state", "nexo_project_id"},
    "conversation": {"title", "state", "last_message_seq"},
    "work_session": {"state", "generation", "latest_summary_ref", "last_item_seq", "lease_owner", "lease_expires_at", "fencing_token"},
    "artifact": {"latest_version", "current_effective_version"},
    "artifact_state": {"confirmation_status", "dependency_status", "quality_status", "effective_selections", "confirmation_ids", "check_record_ids"},
    "task": {"state", "pause_reason", "budget", "usage", "repair_rounds_used", "current_run_id", "latest_checkpoint_id"},
    "run": {"state", "started_at", "finished_at", "model_turns_used", "latest_checkpoint_id", "error", "lease_owner", "lease_expires_at", "fencing_token"},
    "queued_request": {"state", "adopted_run_id", "adopted_context_snapshot_id", "created_task_id", "resolved_at", "blocked_reason"},
    "config_version": {"state", "published_at"},
    "model_call": {"state", "started_at", "finished_at", "provider_response_id", "response_history_ids", "usage", "error"},
    "tool_call": {"state", "started_at", "finished_at", "result", "history_ids", "error"},
    "dependency": {"state", "assessment_ref"},
    "blob": {"state", "error"},
    "idempotency_record": {"state", "result", "error", "resource_refs"},
}

SCALAR_LINKS = {
    "conversation_id": "conversation", "session_id": "work_session", "task_id": "task", "run_id": "run",
    "source_message_id": "history_record", "requested_by_message_id": "history_record",
    "parent_task_id": "task", "budget_root_task_id": "task", "current_run_id": "run",
    "latest_checkpoint_id": "checkpoint", "target_run_id": "run", "adopted_run_id": "run",
    "adopted_context_snapshot_id": "context_snapshot", "created_task_id": "task", "artifact_id": "artifact",
    "artifact_version_id": "artifact_version", "producer_run_id": "run", "applied_run_id": "run",
    "context_snapshot_id": "context_snapshot", "model_call_id": "model_call", "causation_id": None,
}
ARRAY_LINKS = {
    "attachment_blob_ids": "blob", "history_ids": "history_record", "source_message_ids": "history_record",
    "carried_from_confirmation_ids": "confirmation", "revokes_confirmation_ids": "confirmation",
    "confirmation_ids": "confirmation", "check_record_ids": "runtime_event", "dependency_ids": "dependency",
    "response_history_ids": "history_record", "adopted_message_ids": "history_record",
}


class Store:
    def __init__(self, dsn: str, blob_dir: Path):
        self.dsn = dsn
        self.blob_dir = Path(blob_dir).resolve()
        self.blob_dir.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self._connections = []
        self._connections_lock = threading.Lock()

    def _connection(self):
        connection = getattr(self._local, "connection", None)
        if connection is None or connection.closed:
            connection = psycopg.connect(self.dsn, autocommit=True, row_factory=dict_row)
            self._local.connection = connection
            self._local.depth = 0
            self._local.pending = []
            with self._connections_lock:
                self._connections.append(connection)
        return connection

    def close(self):
        with self._connections_lock:
            for connection in self._connections:
                connection.close()
            self._connections.clear()

    @contextmanager
    def transaction(self):
        connection = self._connection()
        prior_depth = self._local.depth
        mark = len(self._local.pending)
        try:
            with connection.transaction():
                self._local.depth += 1
                yield self
                if prior_depth == 0:
                    # Deferred relation validation permits cyclic records in one transaction.
                    pending = list(dict.fromkeys(self._local.pending))
                    for record_id in pending:
                        row = connection.execute("SELECT project_id FROM record_index WHERE id=%s", (record_id,)).fetchone()
                        if row:
                            record = self.get(record_id, str(row["project_id"]) if row["project_id"] else None)
                            self._validate_relations(record)
                    self._local.pending.clear()
        except psycopg.errors.UniqueViolation as exc:
            del self._local.pending[mark:]
            raise RecordConflict("Record identity or unique business key already exists") from exc
        except psycopg.Error as exc:
            del self._local.pending[mark:]
            raise StorageError(str(exc).split("\n")[0]) from exc
        except BaseException:
            del self._local.pending[mark:]
            raise
        finally:
            self._local.depth = prior_depth

    def migrate(self):
        connection = self._connection()
        with connection.transaction():
            connection.execute("SELECT pg_advisory_xact_lock(819244634)")
            connection.execute("CREATE TABLE IF NOT EXISTS branch_agent_schema_migrations(name TEXT PRIMARY KEY,sha256 TEXT NOT NULL,applied_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp())")
            for path in sorted((ROOT / "migrations").glob("*.sql")):
                # macOS archives can contain AppleDouble sidecar files such as
                # `._001_records.sql`. They are binary metadata, not migrations.
                if path.name.startswith("._"):
                    continue
                raw = path.read_bytes()
                digest = hashlib.sha256(raw).hexdigest()
                existing = connection.execute("SELECT sha256 FROM branch_agent_schema_migrations WHERE name=%s", (path.name,)).fetchone()
                if existing:
                    if existing["sha256"] != digest:
                        raise StorageError("Applied migration changed: " + path.name)
                    continue
                connection.execute(raw.decode("utf-8"))
                connection.execute("INSERT INTO branch_agent_schema_migrations(name,sha256) VALUES(%s,%s)", (path.name, digest))

    def now(self) -> str:
        value = self._connection().execute("SELECT clock_timestamp() AS value").fetchone()["value"]
        return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")

    def advisory_lock(self, key: str):
        connection = self._connection()
        if not self._local.depth:
            raise StorageError("advisory_lock requires an active Store.transaction()")
        number = int.from_bytes(hashlib.sha256(key.encode()).digest()[:8], "big", signed=True)
        connection.execute("SELECT pg_advisory_xact_lock(%s)", (number,))

    def projection_get(self, key: str) -> dict | None:
        row = self._connection().execute("SELECT value FROM runtime_projections WHERE key=%s", (key,)).fetchone()
        return row["value"] if row else None

    def projection_put(self, key: str, value: dict) -> dict:
        value = copy.deepcopy(value)
        try:
            project_id = str(UUID(value["project_id"]))
        except (KeyError, ValueError, TypeError) as exc:
            raise InvalidRecord("Projection requires a project_id") from exc
        if not key.startswith(project_id + ":"):
            raise InvalidRecord("Projection key must be prefixed with project_id:")
        canonical_bytes(value)
        with self.transaction():
            self._connection().execute("""INSERT INTO runtime_projections(key,project_id,value) VALUES(%s,%s,%s)
              ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=clock_timestamp(),row_version=runtime_projections.row_version+1""",
                                       (key, project_id, Jsonb(value)))
        return value

    def _validate(self, record):
        try:
            validate_record(record)
        except Exception as exc:
            raise InvalidRecord(str(exc)) from exc

    def put(self, record: dict) -> dict:
        record = copy.deepcopy(record)
        self._validate(record)
        if record.get("row_version", 1) != 1:
            raise InvalidRecord("New mutable records start at row_version=1")
        kind = record["record_type"]
        with self.transaction():
            if kind in ("artifact", "decision"):
                logical_id = record["id"] if kind == "artifact" else record["decision_id"]
                self._connection().execute("INSERT INTO logical_objects(id,project_id,object_type) VALUES(%s,%s,%s) ON CONFLICT(id) DO NOTHING",
                                           (logical_id, record["project_id"], kind))
                logical = self._connection().execute("SELECT project_id,object_type FROM logical_objects WHERE id=%s FOR UPDATE", (logical_id,)).fetchone()
                if str(logical["project_id"]) != record["project_id"] or logical["object_type"] != kind:
                    raise InvalidRecord("Logical identity belongs to another project or type")
            self._connection().execute(sql.SQL("INSERT INTO {}(data) VALUES(%s)").format(sql.Identifier(TABLES[kind])), (Jsonb(record),))
            self._local.pending.append(record["id"])
        return record

    insert = put

    def get(self, record_id: str, project_id: str | None = None) -> dict | None:
        try:
            UUID(record_id)
        except (ValueError, TypeError, AttributeError):
            return None
        row = self._connection().execute("SELECT record_type FROM record_index WHERE id=%s AND project_id IS NOT DISTINCT FROM %s::uuid", (record_id, project_id)).fetchone()
        if not row:
            return None
        kind = row["record_type"]
        if project_id is None and kind != "config_version":
            return None
        row = self._connection().execute(sql.SQL("SELECT data FROM {} WHERE id=%s").format(sql.Identifier(TABLES[kind])), (record_id,)).fetchone()
        return row["data"] if row else None

    def _list(self, project_id, record_type, limit, offset, filters, scan=False):
        if not isinstance(limit, int) or not 1 <= limit <= 10000 or not isinstance(offset, int) or offset < 0:
            raise InvalidRecord("Invalid pagination")
        if record_type is not None and record_type not in TABLES:
            raise InvalidRecord("Unknown record type")
        where, params = [], []
        if not scan:
            where.append(sql.SQL("r.project_id IS NOT DISTINCT FROM %s::uuid"))
            params.append(project_id)
        kinds = [record_type] if record_type else list(TABLES)
        if project_id is None and not scan:
            kinds = [kind for kind in kinds if kind == "config_version"]
        if not kinds:
            return []
        # JSON containment is parameterized, permits strict exact-value filters.
        if filters:
            for key in filters:
                if not any(key in DEFINITIONS[KINDS[kind]]["properties"] for kind in kinds):
                    raise InvalidRecord("Unknown filter field: " + key)
            where.append(sql.SQL("r.data @> %s::jsonb"))
            params.append(Jsonb(filters))
        union = sql.SQL(" UNION ALL ").join(sql.SQL("SELECT data,project_id,created_at,id FROM {} ").format(sql.Identifier(TABLES[kind])) for kind in kinds)
        statement = sql.SQL("SELECT r.data FROM ({}) r").format(union)
        if where:
            statement += sql.SQL(" WHERE ") + sql.SQL(" AND ").join(where)
        statement += sql.SQL(" ORDER BY r.created_at,r.id LIMIT %s OFFSET %s")
        rows = self._connection().execute(statement, params + [limit, offset]).fetchall()
        return [row["data"] for row in rows]

    def list(self, project_id: str | None, record_type: str | None = None, *, limit=100, offset=0, filters: dict | None = None) -> list[dict]:
        return self._list(project_id, record_type, limit, offset, filters)

    def scan(self, record_type: str, *, limit=100, filters: dict | None = None) -> list[dict]:
        """Trusted scheduler-only cross-project scan. Never route directly to HTTP."""
        return self._list(None, record_type, limit, 0, filters, scan=True)

    def list_projects(self, account_id: str, *, limit=100, offset=0) -> list[dict]:
        return self._list(None, "project", limit, offset, {"owner_account_id": account_id}, scan=True)

    def update(self, record: dict, expected_version: int) -> dict:
        kind = record.get("record_type")
        if kind not in MUTABLE:
            raise ImmutableRecord("This record is append-only")
        if type(expected_version) is not int or expected_version < 1:
            raise InvalidRecord("expected_version must be a positive integer")
        with self.transaction():
            old = self.get(record["id"], record["project_id"])
            if old is None:
                raise InvalidRecord("Record not found in project")
            if old["row_version"] != expected_version:
                raise VersionConflict("Record changed; read its current row_version")
            allowed = MUTABLE[kind] | {"updated_at", "row_version"}
            changed = {key for key in set(old) | set(record) if old.get(key) != record.get(key)}
            if changed - allowed or (kind == "config_version" and old["scope_kind"] == "run_snapshot"):
                raise ImmutableRecord("Immutable content fields cannot be overwritten")
            if kind == "blob" and old["state"] == "ready" and changed - {"updated_at", "row_version"}:
                raise ImmutableRecord("Ready Blob metadata and bytes are immutable")
            candidate = copy.deepcopy(record)
            candidate.update(updated_at=self.now(), row_version=expected_version + 1)
            self._validate(candidate)
            row = self._connection().execute(sql.SQL("UPDATE {} SET data=%s WHERE id=%s AND project_id IS NOT DISTINCT FROM %s::uuid AND row_version=%s RETURNING data").format(sql.Identifier(TABLES[kind])),
                                              (Jsonb(candidate), candidate["id"], candidate["project_id"], expected_version)).fetchone()
            if not row:
                raise VersionConflict("Concurrent update lost optimistic lock")
            self._local.pending.append(candidate["id"])
        return candidate

    def delete(self, record_id: str, project_id: str) -> bool:
        with self.transaction():
            record = self.get(record_id, project_id)
            if record is None:
                return False
            if record["record_type"] != "session_item":
                raise ImmutableRecord("Only rebuildable SessionItem records may be deleted")
            self._connection().execute("DELETE FROM session_items WHERE id=%s AND project_id=%s", (record_id, project_id))
        return True

    def account_idempotency_get(self, account_id: str, operation: str, key: str) -> dict | None:
        row=self._connection().execute("""SELECT sha256,result FROM account_api_idempotency
          WHERE account_id=%s AND operation=%s AND key=%s""",(account_id,operation,key)).fetchone()
        return {'sha256':row['sha256'],'result':row['result']} if row else None

    def account_idempotency_put(self, account_id: str, operation: str, key: str, sha256: str, result: dict):
        self._connection().execute("""INSERT INTO account_api_idempotency(account_id,operation,key,sha256,result)
          VALUES(%s,%s,%s,%s,%s)""",(account_id,operation,key,sha256,Jsonb(result)))

    def delete_project(self, project_id: str, account_id: str) -> bool:
        """Permanently delete one owned project and its bytes as one recoverable operation."""
        try:
            project_id=str(UUID(project_id))
        except (ValueError,TypeError,AttributeError):
            return False
        project=self.get(project_id,project_id)
        if not project or project['record_type']!='project' or project['owner_account_id']!=account_id:
            return False
        source=(self.blob_dir/project_id).resolve()
        if source.parent!=self.blob_dir or source.is_symlink():
            raise InvalidRecord('Project Blob directory escapes the storage directory')
        deleting=self.blob_dir/'.deleting'
        deleting.mkdir(mode=0o700,exist_ok=True)
        staged=deleting/(project_id+'-'+uuid4().hex)
        moved=False
        if source.exists():
            os.replace(source,staged);moved=True
        try:
            with self.transaction():
                self.advisory_lock('project:delete:'+project_id)
                locked=self._connection().execute('SELECT owner_account_id FROM projects WHERE id=%s FOR UPDATE',(project_id,)).fetchone()
                if not locked or locked['owner_account_id']!=account_id:
                    raise InvalidRecord('Project does not belong to this account')
                connection=self._connection()
                connection.execute("SELECT set_config('branch_agent.hard_delete_project',%s,true)",(project_id,))
                connection.execute('SET CONSTRAINTS ALL DEFERRED')
                connection.execute('DELETE FROM config_links WHERE consumer_project_id=%s OR config_version_id IN (SELECT id FROM config_versions WHERE project_id=%s)',(project_id,project_id))
                connection.execute('DELETE FROM record_links WHERE project_id=%s',(project_id,))
                connection.execute('DELETE FROM runtime_projections WHERE project_id=%s',(project_id,))
                connection.execute('DELETE FROM logical_objects WHERE project_id=%s',(project_id,))
                for table in dict.fromkeys(TABLES.values()):
                    if table!='projects':
                        connection.execute(sql.SQL('DELETE FROM {} WHERE project_id=%s').format(sql.Identifier(table)),(project_id,))
                connection.execute('DELETE FROM projects WHERE id=%s AND owner_account_id=%s',(project_id,account_id))
            if moved: shutil.rmtree(staged)
            return True
        except BaseException:
            if moved and staged.exists() and not source.exists(): os.replace(staged,source)
            raise

    def _reference(self, reference: dict, project_id: str):
        if reference["version"] is None:
            record = self.get(reference["record_id"], project_id)
        else:
            version = reference["version"]
            if not version.isdigit() or str(int(version)) != version or int(version) < 1:
                raise InvalidRecord("EvidenceRef.version must be a positive decimal version")
            row = self._connection().execute("SELECT object_type FROM logical_objects WHERE id=%s AND project_id=%s", (reference["record_id"], project_id)).fetchone()
            if not row:
                raise InvalidRecord("Logical reference missing in project")
            kind = row["object_type"]
            table, field = ("artifact_versions", "artifact_id") if kind == "artifact" else ("decisions", "decision_id")
            row = self._connection().execute(sql.SQL("SELECT data FROM {} WHERE project_id=%s AND {}=%s AND version=%s").format(sql.Identifier(table), sql.Identifier(field)),
                                              (project_id, reference["record_id"], int(version))).fetchone()
            record = row["data"] if row else None
        if not record:
            raise InvalidRecord("Referenced record/version does not exist in this project")
        if reference["version"] is None and record["record_type"] in ("artifact", "artifact_version", "decision"):
            raise InvalidRecord("Business references require a logical ID and explicit version")
        return record

    def resolve_ref(self, reference: dict, project_id: str) -> dict:
        return self._reference(reference, project_id)

    def _link(self, source, relation, target_id, expected_kind=None, reference=None):
        project_id = source["project_id"]
        target = self.get(target_id, project_id)
        if not target or (expected_kind and target["record_type"] != expected_kind):
            raise InvalidRecord(f"Missing, cross-project or wrong-type reference: {relation}")
        self._connection().execute("INSERT INTO record_links(project_id,from_id,relation,ordinal,to_id,to_type,target_version,selector) VALUES(%s,%s,%s,0,%s,%s,%s,%s)",
                                   (project_id, source["id"], relation, target_id, target["record_type"],
                                    int(reference["version"]) if reference and reference["version"] else None,
                                    Jsonb(reference) if reference else None))
        return target

    def _config_link(self, source, relation, target_id):
        target = self.get(target_id, source["project_id"]) or self.get(target_id, None)
        if not target or target["record_type"] != "config_version":
            raise InvalidRecord("Invalid or cross-project configuration reference")
        self._connection().execute("INSERT INTO config_links(consumer_project_id,consumer_id,config_version_id,relation,ordinal) VALUES(%s,%s,%s,%s,0)",
                                   (source["project_id"], source["id"], target_id, relation))
        return target

    def _walk_refs(self, source, value, spec, path):
        if "$ref" in spec:
            name = spec["$ref"].split("/")[-1]
            if name == "EvidenceRef":
                target = self._reference(value, source["project_id"])
                self._link(source, path, target["id"], reference=value)
                return
            if name == "Content":
                if value["storage"] == "blob":
                    blob = self._link(source, path + "/blob_id", value["blob_id"], "blob")
                    if blob["state"] != "ready":
                        raise InvalidRecord("Content cannot reference a Blob before it is ready")
                    self._read_blob_record(blob)
                return  # Arbitrary original text/JSON is data, not trusted foreign keys.
            spec = DEFINITIONS[name]
        if value is None:
            return
        for variant in spec.get("anyOf", []):
            if variant.get("type") != "null":
                self._walk_refs(source, value, variant, path)
        if isinstance(value, list) and "items" in spec:
            for index, item in enumerate(value):
                self._walk_refs(source, item, spec["items"], path + "/" + str(index))
        if isinstance(value, dict):
            for key, child in spec.get("properties", {}).items():
                if key in value:
                    self._walk_refs(source, value[key], child, path + "/" + key)

    def _validate_relations(self, record):
        kind, pid = record["record_type"], record["project_id"]
        connection = self._connection()
        connection.execute("DELETE FROM record_links WHERE from_id=%s", (record["id"],))
        connection.execute("DELETE FROM config_links WHERE consumer_id=%s", (record["id"],))
        for key, target_kind in SCALAR_LINKS.items():
            if record.get(key) is not None:
                self._link(record, key, record[key], target_kind)
        for key, target_kind in ARRAY_LINKS.items():
            for index, target_id in enumerate(record.get(key, [])):
                self._link(record, f"{key}/{index}", target_id, target_kind)
        for key in ("config_version_id", "parent_config_id"):
            if record.get(key) is not None:
                self._config_link(record, key, record[key])
        for index, target_id in enumerate(record.get("resolved_from_ids", [])):
            self._config_link(record, f"resolved_from_ids/{index}", target_id)
        self._walk_refs(record, record, DEFINITIONS[KINDS[kind]], "")
        if kind == "config_version":
            if hashlib.sha256(canonical_bytes(record["values"])).hexdigest() != record["sha256"]:
                raise InvalidRecord("Config values hash mismatch")
        if kind == "context_snapshot":
            contents = {key: record[key] for key in ("instructions", "input_items", "tool_definitions")}
            if hashlib.sha256(canonical_bytes(contents)).hexdigest() != record["content_sha256"]:
                raise InvalidRecord("ContextSnapshot saved input hash mismatch")
            run = self.get(record["run_id"], pid)
            if any(record[key] != run[key] for key in ("task_id", "session_id", "config_version_id")):
                raise InvalidRecord("ContextSnapshot does not match its Run input ownership")
        if kind == "blob" and record["state"] == "ready":
            self._read_blob_record(record)
        if kind == "artifact_version":
            actual = self._content_digest(record["content"], pid)
            if actual != record["content_sha256"]:
                raise InvalidRecord("Artifact content hash mismatch")
            self._check_version(record, "artifact_versions", "artifact_id")
        if kind == "decision":
            self._check_version(record, "decisions", "decision_id")
        if kind == "artifact_state":
            version = self.get(record["artifact_version_id"], pid)
            if version["artifact_id"] != record["artifact_id"] or version["version"] != record["version"]:
                raise InvalidRecord("ArtifactState points to the wrong artifact version")
        if kind == "artifact":
            for value in (record["latest_version"], record["current_effective_version"]):
                if value and not connection.execute("SELECT 1 FROM artifact_versions WHERE project_id=%s AND artifact_id=%s AND version=%s", (pid, record["id"], value)).fetchone():
                    raise InvalidRecord("Artifact version pointer does not exist")
        if kind == "run":
            task, session = self.get(record["task_id"], pid), self.get(record["session_id"], pid)
            if task["conversation_id"] != session["conversation_id"]:
                raise InvalidRecord("Run Task and Session belong to different conversations")
        if kind == "model_call":
            run = self.get(record["run_id"], pid)
            snapshot = self.get(record["context_snapshot_id"], pid)
            if run["execution_kind"] != "runner" or run["task_id"] != record["task_id"] or snapshot["run_id"] != record["run_id"] or snapshot["task_id"] != record["task_id"]:
                raise InvalidRecord("ModelCall must belong to its actual runner and input snapshot")
        if kind == "tool_call":
            model = self.get(record["model_call_id"], pid)
            if model["task_id"] != record["task_id"]:
                raise InvalidRecord("ToolCall belongs to another ModelCall Task")
            if model["run_id"] != record["run_id"]:
                raise InvalidRecord("Cross-Run tool execution requires a verified recovery adapter")
        if kind == "confirmation":
            for message_id in record["source_message_ids"]:
                if self.get(message_id, pid)["role"] != "user":
                    raise InvalidRecord("Confirmation requires actual user messages")
            if record["basis"] == "unchanged_scope":
                event = self._reference(record["scope_mapping_ref"], pid)
                if event["record_type"] != "runtime_event" or event["event_name"] != "confirmation.scope_mapped":
                    raise InvalidRecord("Confirmation inheritance requires a scope mapping event")

    def _check_version(self, record, table, identity_field):
        versions = self._connection().execute(sql.SQL("SELECT version FROM {} WHERE project_id=%s AND {}=%s ORDER BY version").format(sql.Identifier(table), sql.Identifier(identity_field)),
                                              (record["project_id"], record[identity_field])).fetchall()
        if [row["version"] for row in versions] != list(range(1, len(versions) + 1)):
            raise InvalidRecord("Content versions must be contiguous starting at one")
        if record["record_type"] == "artifact_version":
            parent = record["parent_version"]
            if (record["version"] == 1 and parent is not None) or (record["version"] > 1 and (parent is None or not 1 <= parent < record["version"])):
                raise InvalidRecord("Artifact version must identify an existing earlier baseline version")

    def _content_digest(self, content, project_id):
        if content["storage"] == "blob":
            blob = self.get(content["blob_id"], project_id)
            if not blob or blob["record_type"] != "blob" or blob["state"] != "ready":
                raise InvalidRecord("Content Blob is not ready")
            self._read_blob_record(blob)
            return blob["sha256"]
        return content_hash(content)

    def _blob_path(self, record):
        relative = Path(record["storage_key"])
        if relative.is_absolute() or ".." in relative.parts or not relative.parts or relative.parts[0] != record["project_id"]:
            raise InvalidRecord("Invalid Blob storage key")
        path = self.blob_dir / relative
        if self.blob_dir not in path.resolve().parents or path.is_symlink():
            raise InvalidRecord("Blob storage key escapes the storage directory")
        return path

    def _read_blob_record(self, record):
        if record["state"] != "ready":
            raise InvalidRecord("Blob is not ready")
        try:
            raw = self._blob_path(record).read_bytes()
        except OSError as exc:
            raise StorageError("Persisted Blob bytes are unavailable") from exc
        if len(raw) != record["byte_size"] or hashlib.sha256(raw).hexdigest() != record["sha256"]:
            raise StorageError("Persisted Blob failed integrity verification")
        return raw

    def blob_put(self, project_id: str, data: bytes, media_type: str, name: str) -> dict:
        if not isinstance(data, bytes):
            raise InvalidRecord("Blob data must be bytes")
        if not self.get(project_id, project_id):
            raise InvalidRecord("Blob project does not exist")
        blob_id = str(uuid4())
        key = project_id + "/" + blob_id
        path = self.blob_dir / key
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as file:
            file.write(data)
            file.flush()
            os.fsync(file.fileno())
        path.chmod(0o440)
        record = new_record("blob", project_id, id=blob_id, filename=Path(name).name or "attachment",
                            media_type=media_type, byte_size=len(data), sha256=hashlib.sha256(data).hexdigest(),
                            storage_key=key, state="ready")
        return self.put(record)

    def blob_read(self, blob_id: str, project_id: str) -> bytes:
        record = self.get(blob_id, project_id)
        if not record or record["record_type"] != "blob":
            raise InvalidRecord("Blob not found in project")
        return self._read_blob_record(record)
