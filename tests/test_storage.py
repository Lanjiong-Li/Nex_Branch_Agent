"""Integration tests use a real local PostgreSQL database, never fixture records."""
import copy
import importlib.util
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from uuid import uuid4

import pytest

DSN = os.getenv("BRANCH_AGENT_TEST_DSN", "postgresql:///branch_agent_local")


def test_storage_module_exists():
    assert importlib.util.find_spec("branch_agent.storage") is not None


@pytest.fixture
def store(tmp_path):
    import psycopg
    from psycopg import sql
    from psycopg.conninfo import make_conninfo
    from branch_agent.storage import Store
    namespace = "storage_test_" + uuid4().hex
    with psycopg.connect(DSN, autocommit=True) as connection:
        connection.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(namespace)))
    isolated_dsn = make_conninfo(DSN, options="-c search_path=" + namespace)
    result = Store(isolated_dsn, tmp_path / "blobs")
    result.migrate()
    try:
        yield result
    finally:
        result.close()
        with psycopg.connect(DSN, autocommit=True) as connection:
            connection.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(namespace)))


def project(store, account="storage-test"):
    from branch_agent.records import new_record
    return store.put(new_record("project", None, owner_account_id=account, title="Storage test"))


def conversation(store, pid):
    from branch_agent.records import new_record
    return store.put(new_record("conversation", pid, title="Test conversation"))


def test_isolation_and_foreign_keys(store):
    from branch_agent.records import new_record
    from branch_agent.storage import StorageError
    first, second = project(store), project(store)
    chat = conversation(store, first["id"])
    assert store.get(chat["id"], second["id"]) is None
    assert store.get(chat["id"]) is None
    assert store.list(second["id"], "conversation") == []
    with pytest.raises(StorageError):
        store.put(new_record("history_record", second["id"], conversation_id=chat["id"],
                             sequence=1, role="user", content={"storage": "inline_text", "text": "hello"}))
    assert store.list(second["id"], "history_record") == []


def test_schema_and_required_business_identity(store):
    from branch_agent.records import new_record
    from branch_agent.storage import InvalidRecord
    first = project(store)
    with pytest.raises(ValueError):
        new_record("history_record", first["id"], role="user")
    invalid = new_record("conversation", first["id"], title="Bad")
    invalid["secret_extension"] = "not permitted"
    with pytest.raises(InvalidRecord):
        store.put(invalid)


def test_nested_transaction_and_rollback(store):
    first = project(store)
    with store.transaction():
        kept = conversation(store, first["id"])
        with pytest.raises(RuntimeError):
            with store.transaction():
                lost = conversation(store, first["id"])
                raise RuntimeError("rollback inner savepoint")
        assert store.get(lost["id"], first["id"]) is None
    assert store.get(kept["id"], first["id"])
    with pytest.raises(RuntimeError):
        with store.transaction():
            lost_outer = conversation(store, first["id"])
            raise RuntimeError("rollback outer transaction")
    assert store.get(lost_outer["id"], first["id"]) is None


def test_cas_has_one_winner_under_concurrency(store, tmp_path):
    from branch_agent.storage import Store, VersionConflict
    initial = project(store)
    barrier = Barrier(2)

    def worker(label):
        other = Store(store.dsn, tmp_path / "blobs")
        candidate = copy.deepcopy(initial)
        candidate["title"] = label
        barrier.wait(timeout=10)
        try:
            return other.update(candidate, expected_version=1)["row_version"]
        except VersionConflict:
            return "conflict"
        finally:
            other.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        values = list(pool.map(worker, ["first", "second"]))
    assert sorted(values, key=str) == [2, "conflict"]
    assert store.get(initial["id"], initial["id"])["row_version"] == 2


def test_history_immutable_and_delete_is_restricted(store):
    from branch_agent.records import new_record
    from branch_agent.storage import ImmutableRecord
    first = project(store)
    chat = conversation(store, first["id"])
    history = store.put(new_record("history_record", first["id"], conversation_id=chat["id"],
                                   sequence=1, role="user", content={"storage": "inline_text", "text": "original"}))
    history["content"]["text"] = "rewritten"
    with pytest.raises(ImmutableRecord):
        store.update(history, expected_version=1)
    with pytest.raises(ImmutableRecord):
        store.delete(history["id"], first["id"])
    assert store.get(history["id"], first["id"])["content"]["text"] == "original"


def test_ready_blob_cannot_be_rewritten_or_read_cross_project(store):
    from branch_agent.storage import ImmutableRecord, StorageError
    first, second = project(store), project(store)
    blob = store.blob_put(first["id"], b"original bytes", "text/plain", "story.txt")
    assert store.blob_read(blob["id"], first["id"]) == b"original bytes"
    with pytest.raises(StorageError):
        store.blob_read(blob["id"], second["id"])
    candidate = copy.deepcopy(blob)
    candidate["byte_size"] += 1
    with pytest.raises(ImmutableRecord):
        store.update(candidate, expected_version=1)
    second_blob = store.blob_put(first["id"], b"new bytes", "text/plain", "story.txt")
    assert second_blob["id"] != blob["id"]
    assert store.blob_read(blob["id"], first["id"]) == b"original bytes"


def test_config_values_hash_and_immutability(store):
    from branch_agent.records import new_record
    from branch_agent.storage import ImmutableRecord
    first = project(store)
    config = store.put(new_record("config_version", first["id"], config_key="test",
                                  scope_kind="project", values={"model": "test-only"}))
    candidate = copy.deepcopy(config)
    candidate["values"]["model"] = "edited"
    with pytest.raises(ImmutableRecord):
        store.update(candidate, expected_version=1)


def test_helpers_do_not_claim_unknown_usage_is_zero():
    from branch_agent.records import budget, scope, usage
    assert usage()["input_tokens"] is None
    assert usage()["reported_cost"] is None
    assert budget()["max_active_seconds"] == 3600
    assert budget()["max_cost"]["amount"] == "20"
    assert scope(stage=9, chapter_id="chapter-a")["chapter_ids"] == ["chapter-a"]


def test_record_constructor_accepts_history_kind_field():
    from branch_agent.records import new_record
    record = new_record("history_record", str(uuid4()), conversation_id=str(uuid4()), sequence=1,
                        role="assistant", kind="model_output", content={"storage": "inline_text", "text": "done"})
    assert record["kind"] == "model_output"


def test_projection_and_db_clock_are_transactional(store):
    first = project(store)
    key = first["id"] + ":queue:root"
    with store.transaction():
        store.advisory_lock(key)
        store.projection_put(key, {"project_id": first["id"], "held": True})
    assert store.projection_get(key)["held"] is True
    with pytest.raises(RuntimeError):
        with store.transaction():
            store.projection_put(key, {"project_id": first["id"], "held": False})
            raise RuntimeError("rollback")
    assert store.projection_get(key)["held"] is True
    assert store.now().endswith("Z")


def execution_records(store):
    from branch_agent.records import new_record
    first = project(store)
    pid = first["id"]
    chat = conversation(store, pid)
    message = store.put(new_record("history_record", pid, conversation_id=chat["id"], sequence=1,
                                    role="user", content={"storage": "inline_text", "text": "Analyze this story"}))
    config = store.put(new_record("config_version", pid, config_key="execution", scope_kind="project", values={}))
    task = store.put(new_record("task", pid, conversation_id=chat["id"], requested_by_message_id=message["id"], intent="generate"))
    session = store.put(new_record("work_session", pid, conversation_id=chat["id"], session_key="stage1"))
    run = store.put(new_record("run", pid, task_id=task["id"], session_id=session["id"], agent_key="source_parser", config_version_id=config["id"]))
    return first, chat, message, config, task, session, run


def test_context_hash_matches_exact_saved_input(store):
    import hashlib
    from branch_agent.records import canonical_bytes, new_record
    from branch_agent.storage import InvalidRecord
    first, _, _, config, task, session, run = execution_records(store)
    contents = {"instructions": {"storage": "inline_text", "text": "Read source"},
                "input_items": {"storage": "inline_json", "value": []},
                "tool_definitions": {"storage": "inline_json", "value": []}}
    snapshot = new_record("context_snapshot", first["id"], task_id=task["id"], run_id=run["id"],
                          session_id=session["id"], config_version_id=config["id"], model="test-only",
                          input_token_budget=32000, content_sha256=hashlib.sha256(canonical_bytes(contents)).hexdigest(), **contents)
    invalid = copy.deepcopy(snapshot)
    invalid["instructions"]["text"] = "Changed after hashing"
    with pytest.raises(InvalidRecord):
        store.put(invalid)
    assert store.put(snapshot)["content_sha256"] == snapshot["content_sha256"]


def test_session_item_deletion_retains_original_history(store):
    from branch_agent.records import new_record
    first, _, history, _, _, session, _ = execution_records(store)
    item = store.put(new_record("session_item", first["id"], session_id=session["id"], sequence=1,
                                sdk_item={"storage": "inline_json", "value": {"role": "user", "content": "story"}},
                                history_ids=[history["id"]]))
    assert store.delete(item["id"], first["id"]) is True
    assert store.get(item["id"], first["id"]) is None
    assert store.get(history["id"], first["id"])["content"]["text"] == "Analyze this story"


def test_revision_can_start_from_an_older_immutable_baseline(store):
    from branch_agent.records import new_record
    first = project(store)
    artifact = store.put(new_record("artifact", first["id"], artifact_kind="source_text"))
    for number, parent in [(1, None), (2, 1), (3, 1)]:
        version = store.put(new_record("artifact_version", first["id"], artifact_id=artifact["id"],
                                       version=number, parent_version=parent, origin="user",
                                       content={"storage": "inline_text", "text": f"Revision {number}"}))
    assert version["parent_version"] == 1


def test_direct_sql_cannot_overwrite_immutable_history(store):
    import psycopg
    from psycopg.types.json import Jsonb
    from branch_agent.records import new_record
    first = project(store)
    chat = conversation(store, first["id"])
    record = store.put(new_record("history_record", first["id"], conversation_id=chat["id"], sequence=1,
                                  role="user", content={"storage": "inline_text", "text": "unaltered"}))
    record["content"]["text"] = "altered"
    with psycopg.connect(store.dsn, autocommit=True) as connection:
        with pytest.raises(psycopg.errors.CheckViolation):
            connection.execute("UPDATE history_records SET data=%s WHERE id=%s", (Jsonb(record), record["id"]))
    assert store.get(record["id"], first["id"])["content"]["text"] == "unaltered"


def test_blob_integrity_detects_external_byte_tampering(store):
    from branch_agent.storage import StorageError
    first = project(store)
    blob = store.blob_put(first["id"], b"saved", "text/plain", "a.txt")
    path = store.blob_dir / blob["storage_key"]
    path.chmod(0o600)
    path.write_bytes(b"tampered")
    with pytest.raises(StorageError, match="integrity"):
        store.blob_read(blob["id"], first["id"])


def test_global_configuration_requires_explicit_global_read(store):
    from branch_agent.records import new_record
    first = project(store)
    global_config = store.put(new_record("config_version", None, config_key="test-" + str(uuid4()),
                                         owner_account_id="storage-test",scope_kind="global",scope_key="project:",
                                         values={"temperature": 0}))
    assert store.get(global_config["id"], first["id"]) is None
    assert store.get(global_config["id"], None)["project_id"] is None
    assert first["id"] in [record["id"] for record in store.list_projects("storage-test", limit=10000)]


def test_delete_project_removes_records_and_blob_bytes_but_keeps_account_config(store):
    from branch_agent.records import new_record
    first=project(store,'delete-account')
    second=project(store,'delete-account')
    chat=conversation(store,first['id'])
    history=store.put(new_record('history_record',first['id'],conversation_id=chat['id'],sequence=1,
        role='user',content={'storage':'inline_text','text':'永久删除我'}))
    blob=store.blob_put(first['id'],b'private source','text/plain','source.txt')
    blob_path=store.blob_dir/blob['storage_key']
    global_config=store.put(new_record('config_version',None,config_key='harness:delete-account',
        scope_kind='global',scope_key='project:',state='published',owner_account_id='delete-account',values={}))

    assert store.delete_project(first['id'],'delete-account') is True

    assert store.get(first['id'],first['id']) is None
    assert store.get(chat['id'],first['id']) is None
    assert store.get(history['id'],first['id']) is None
    assert not blob_path.exists()
    assert store.get(second['id'],second['id']) is not None
    assert store.get(global_config['id'],None) is not None


def test_direct_sql_cannot_bypass_project_relationship(store):
    import psycopg
    from psycopg.types.json import Jsonb
    from branch_agent.records import new_record
    first, second = project(store), project(store)
    chat = conversation(store, first["id"])
    record = new_record("history_record", second["id"], conversation_id=chat["id"], sequence=1,
                        role="user", content={"storage": "inline_text", "text": "wrong project"})
    with psycopg.connect(store.dsn) as connection, connection.transaction(force_rollback=True):
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            with connection.transaction():
                connection.execute("INSERT INTO history_records(data) VALUES(%s)", (Jsonb(record),))
                connection.execute("SET CONSTRAINTS ALL IMMEDIATE")


def test_content_cannot_reference_unready_blob(store):
    from branch_agent.records import new_record
    from branch_agent.storage import InvalidRecord
    first = project(store)
    chat = conversation(store, first["id"])
    pending = store.put(new_record("blob", first["id"], filename="pending.txt", media_type="text/plain",
                                   byte_size=0, sha256="0" * 64, storage_key=first["id"] + "/pending", state="pending"))
    with pytest.raises(InvalidRecord):
        store.put(new_record("history_record", first["id"], conversation_id=chat["id"], sequence=1,
                             role="user", content={"storage": "blob", "blob_id": pending["id"]}))
