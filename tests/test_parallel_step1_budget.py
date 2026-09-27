"""A live Step 1 sibling's context summary shares the workflow budget."""
import hashlib
from uuid import uuid4

import pytest

from branch_agent.records import canonical_bytes, new_record
from branch_agent.workflow import WorkflowBlocked, update
from test_runtime import runtime


@pytest.fixture
def parallel_summary(runtime):
    engine, _, project, conversation = runtime
    with engine.store.transaction():
        message = engine._message(project, conversation, "分析原作", role="user")
        root = engine._new_task(project, conversation, message, "generate", is_workflow=True)
        global_view = engine._new_task(project, conversation, message, "generate", stage=1,
                                       parent=root, step1_view="global")
        character_view = engine._new_task(project, conversation, message, "generate", stage=1,
                                          parent=root, step1_view="character")
        global_view, _, _, _ = engine._start_run(global_view, 1, [], session_key_override="global-test")
        character_view, character_run, _, _ = engine._start_run(
            character_view, 1, [], session_key_override="character-test")

    def add_call(task, run, state="running"):
        with engine.store.transaction():
            contents = {"instructions": {"storage": "inline_text", "text": "test"},
                        "input_items": {"storage": "inline_json", "value": []},
                        "tool_definitions": {"storage": "inline_json", "value": []}}
            session = engine.store.get(run["session_id"], project)
            snapshot = engine.store.put(new_record(
                "context_snapshot", project, task_id=task["id"], run_id=run["id"],
                session_id=session["id"], config_version_id=run["config_version_id"],
                model="test", reasoning_effort="low", output_schema=None,
                **contents, input_token_estimate=10, input_token_budget=10000,
                content_sha256=hashlib.sha256(canonical_bytes(contents)).hexdigest()))
            return engine.store.put(new_record(
                "model_call", project, task_id=task["id"], run_id=run["id"],
                operation_id=str(uuid4()), context_snapshot_id=snapshot["id"],
                state=state, started_at=engine.store.now()))

    def add_summary(parent):
        # Match compact_session's parent-owned task and lease-free run.
        with engine.store.transaction():
            summary = engine.store.put(new_record(
                "task", project, conversation_id=conversation, parent_task_id=parent["id"],
                budget_root_task_id=root["id"], requested_by_message_id=message["id"],
                intent="summarize", scope=parent["scope"], state="running",
                budget=parent["budget"]))
            engine._save_task_data(summary, {"stage": "aux.summary", "parent_owned": True})
            session = engine.store.put(new_record(
                "work_session", project, conversation_id=conversation,
                session_key=f"summary:{summary['id']}", scope=summary["scope"]))
            run = engine.store.put(new_record(
                "run", project, task_id=summary["id"], agent_key="context_summarizer",
                session_id=session["id"], config_version_id=character_run["config_version_id"],
                state="running", started_at=engine.store.now()))
            summary = update(engine.store, summary, current_run_id=run["id"])
            call = add_call(summary, run)
        return summary, run, call

    summary, run, call = add_summary(character_view)
    return engine, project, root, global_view, character_view, character_run, summary, run, call, add_summary, add_call


@pytest.mark.parametrize("state", ["pending", "running"])
def test_live_other_step1_summary_does_not_block_budget(parallel_summary, state):
    engine, project, _, global_view, _, _, _, _, call, _, _ = parallel_summary
    update(engine.store, call, state=state)
    engine._budget_check(global_view)


def test_live_other_step1_direct_call_still_does_not_block(parallel_summary):
    engine, _, _, global_view, character_view, character_run, _, _, _, _, add_call = parallel_summary
    add_call(character_view, character_run)
    engine._budget_check(global_view)


@pytest.mark.parametrize("invalid", [
    "unknown_call", "finished_summary_run", "old_summary_run", "expired_view_lease",
    "wrong_view_lease_owner",
    "old_view_run", "wrong_summary_stage", "unrelated_parent", "same_view",
])
def test_unrelated_or_uncertain_summary_still_blocks_budget(parallel_summary, invalid):
    engine, project, root, global_view, character_view, character_run, summary, run, call, add_summary, _ = parallel_summary
    with engine.store.transaction():
        if invalid == "unknown_call":
            update(engine.store, call, state="unknown")
        elif invalid == "finished_summary_run":
            update(engine.store, run, state="failed", finished_at=engine.store.now())
        elif invalid == "old_summary_run":
            update(engine.store, summary, current_run_id=None)
        elif invalid == "expired_view_lease":
            update(engine.store, engine.store.get(character_run["id"], project),
                   lease_expires_at=engine.store.now())
        elif invalid == "wrong_view_lease_owner":
            update(engine.store, engine.store.get(character_run["id"], project),
                   lease_owner=str(uuid4()))
        elif invalid == "old_view_run":
            update(engine.store, engine.store.get(character_view["id"], project), current_run_id=None)
        elif invalid == "wrong_summary_stage":
            engine._save_task_data(summary, {"stage": "step1", "parent_owned": True})
        elif invalid == "unrelated_parent":
            add_summary(root)
        elif invalid == "same_view":
            add_summary(global_view)
    with pytest.raises(WorkflowBlocked, match="operation_uncertain"):
        engine._budget_check(global_view)
