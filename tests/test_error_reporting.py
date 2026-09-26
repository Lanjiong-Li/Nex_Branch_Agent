"""Failure reporting/reconciliation uses isolated PostgreSQL and synthetic errors."""
import asyncio
import hashlib
from copy import deepcopy
from uuid import uuid4

import pytest

from branch_agent.model_service import ModelRunError
from branch_agent.records import new_record, canonical_bytes, usage
from branch_agent.workflow import WorkflowBlocked, all_records, update
from test_runtime import FakeModel, runtime, coordinator, source_response


def failed_operation(engine, pid, cid, *, known=True):
    message = engine.submit_message(pid, cid, "测试失败分类")
    with engine.store.transaction():
        for queued in engine.status(pid)["queue"]:
            update(engine.store, queued, state="cancelled")
        task = engine._new_task(pid, cid, message, "generate", 2)
        task, run, session, config = engine._start_run(task, 2, [])
        contents = {"instructions": {"storage": "inline_text", "text": "test"},
                    "input_items": {"storage": "inline_json", "value": []},
                    "tool_definitions": {"storage": "inline_json", "value": []}}
        snapshot = engine.store.put(new_record("context_snapshot", pid,
            task_id=task["id"], run_id=run["id"], session_id=session["id"],
            config_version_id=run["config_version_id"], model=config["model"]["name"],
            reasoning_effort=config["model"]["reasoning_effort"],
            output_schema=None,
            **contents, input_token_estimate=10, input_token_budget=10000,
            content_sha256=hashlib.sha256(canonical_bytes(contents)).hexdigest()))
        call = engine.store.put(new_record("model_call", pid, task_id=task["id"],
            run_id=run["id"], operation_id=str(uuid4()), context_snapshot_id=snapshot["id"],
            state="failed" if known else "unknown", started_at=engine.store.now(),
            finished_at=engine.store.now(), error={"code": "output_limit_exceeded" if known else "ModelBehaviorError",
                "message": "模型输出达到上限", "retryable": False,
                "details": {"known_outcome": True, "terminal_status": "incomplete"} if known else {}}))
    return task, run, call


def test_known_incomplete_without_usage_blocks_on_usage_not_operation(runtime):
    engine, _, pid, cid = runtime
    task, _, _ = failed_operation(engine, pid, cid)
    with pytest.raises(WorkflowBlocked, match="usage_uncertain"):
        engine._budget_check(task)


def test_known_incomplete_cost_still_counts_and_rejection_is_not_unknown_usage(runtime):
    engine, _, pid, cid = runtime
    task, _, call = failed_operation(engine, pid, cid)
    measured = deepcopy(call["usage"])
    measured["estimated_cost"] = {"amount": "21", "currency": "USD"}
    update(engine.store, call, usage=measured)
    with pytest.raises(WorkflowBlocked, match="cost_limit"):
        engine._budget_check(task)
    call = engine.store.get(call["id"], pid)
    update(engine.store, call, usage=usage(),
        error={"code": "AuthenticationError", "message": "凭据无效", "retryable": False,
               "details": {"http_status": 401}})
    engine._budget_check(task)


def test_step1_branch_output_limit_pauses_once_preserving_safe_error_details(runtime):
    engine, _, pid, cid = runtime
    engine.import_source(pid, cid, "甲见乙。", "故事")
    details = {"known_outcome": True, "terminal_status": "incomplete",
               "incomplete_reason": "max_output_tokens", "max_output_tokens": 8000}

    class LimitGlobal(FakeModel):
        async def run(self, stage, task, run, session, config, materials, message, control=None,
                      step1_view=None, step1_window=None, instructions_override=None):
            if step1_view == "global":
                self.calls.append(stage)
                error = ModelRunError("output_limit_exceeded", "模型输出达到 8000 tokens 上限（含推理），响应未完成。")
                error.details = details
                raise error
            return await super().run(stage, task, run, session, config, materials, message,
                                     control=control, step1_view=step1_view,
                                     step1_window=step1_window,
                                     instructions_override=instructions_override)

    model = LimitGlobal()
    model.store = engine.store
    engine.model_service = model
    with engine.store.transaction():
        message = engine._message(pid, cid, "分析原作", role="user")
        task = engine._new_task(pid, cid, message, "generate", stage=1)
    asyncio.run(engine.tick(pid, cid))
    branches = {engine._task_data(child)["step1_view"]: child for child in all_records(
        engine.store, pid, "task") if child["parent_task_id"] == task["id"]}
    failed = engine.store.get(branches["global"]["id"], pid)
    assert failed["state"] == "paused" and failed["pause_reason"] == "output_limit_exceeded"
    assert failed["repair_rounds_used"] == 0
    assert sorted(model.calls) == ["step1", "step1"]
    run = next(r for r in engine.status(pid)["runs"] if r["task_id"] == failed["id"])
    assert run["error"]["details"] == details
    assert engine.store.get(task["id"], pid)["state"] == "paused"
    assert engine.workflow.resolve(pid, "source_character_events")
    with pytest.raises(WorkflowBlocked, match="missing_material"):
        engine.workflow.resolve(pid, "source_global_events")


LEGACY_LIMIT_MESSAGE = ("模型输出不符合结构约定：Responses stream ended with terminal event "
                        "`response.incomplete`. status=incomplete; "
                        "incomplete_details=IncompleteDetails(reason='max_output_tokens').")


def legacy_failure(engine, pid, cid, message=LEGACY_LIMIT_MESSAGE):
    task, run, call = failed_operation(engine, pid, cid, known=False)
    with engine.store.transaction():
        engine._close_run(task, run, "failed", {"code": "output_validation_failed",
            "message": message, "retryable": False, "details": {}})
        engine._transition(engine.store.get(task["id"], pid), "paused", "operation_uncertain")
    return task, run, call


def test_legacy_limit_correction_is_evidenced_idempotent_and_does_not_retry(runtime):
    from branch_agent.reconciliation import reconcile_legacy_output_limit
    engine, model, pid, cid = runtime
    task, run, call = legacy_failure(engine, pid, cid)
    before = {kind: engine.store.get(row["id"], pid) for kind, row in
              (("task", task), ("run", run), ("model_call", call))}
    result = reconcile_legacy_output_limit(engine, pid, task["id"])
    assert result["changed"]
    corrected_task = engine.store.get(task["id"], pid)
    corrected_call = engine.store.get(call["id"], pid)
    assert corrected_task["state"] == "paused"
    assert corrected_task["pause_reason"] == "output_limit_exceeded"
    assert corrected_task["budget"] == before["task"]["budget"]
    assert corrected_task["repair_rounds_used"] == before["task"]["repair_rounds_used"]
    assert corrected_call["state"] == "failed"
    assert corrected_call["usage"] == before["model_call"]["usage"]
    assert corrected_call["provider_response_id"] is None
    assert corrected_call["response_history_ids"] == []
    for row in (run, call):
        corrected = engine.store.get(row["id"], pid)
        assert corrected["started_at"] == before[row["record_type"]]["started_at"]
        assert corrected["finished_at"] == before[row["record_type"]]["finished_at"]
    correction = engine.store.get(result["event_id"], pid)
    assert correction["payload"]["before"] == before
    assert engine.store.get(run["id"], pid)["error"]["code"] == "output_limit_exceeded"
    with pytest.raises(WorkflowBlocked, match="usage_uncertain"):
        engine._budget_check(corrected_task)
    counts = {k: len(all_records(engine.store, pid, k)) for k in
              ("runtime_event", "history_record", "run", "model_call")}
    assert not reconcile_legacy_output_limit(engine, pid, task["id"])["changed"]
    assert counts == {k: len(all_records(engine.store, pid, k)) for k in counts}
    assert not model.calls


@pytest.mark.parametrize("message", ["Connection reset; max_output_tokens=8000", "模型输出错误：max_output_tokens", LEGACY_LIMIT_MESSAGE + " untrusted suffix"])
def test_reconciliation_does_not_guess_unknown_outcome_from_keywords(runtime, message):
    from branch_agent.reconciliation import reconcile_legacy_output_limit
    engine, _, pid, cid = runtime
    task, run, call = legacy_failure(engine, pid, cid, message)
    before = [engine.store.get(row["id"], pid) for row in (task, run, call)]
    assert not reconcile_legacy_output_limit(engine, pid, task["id"])["changed"]
    assert [engine.store.get(row["id"], pid) for row in (task, run, call)] == before
    with pytest.raises(WorkflowBlocked, match="operation_uncertain"):
        engine._budget_check(task)
