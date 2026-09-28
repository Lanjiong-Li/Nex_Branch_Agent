"""Independent action-boundary regressions; isolated PostgreSQL, synthetic models only."""
import asyncio
from copy import deepcopy

import pytest

from branch_agent.actions import ActionService
from branch_agent.workflow import WorkflowBlocked, ref, update
from test_actions import card, failed_output, initial_task, pending_confirmation, pending_question, submit
from test_runtime import runtime, source_response


@pytest.mark.parametrize("field,value", [
    ("max_cost_usd", "NaN"), ("max_cost_usd", "Infinity"),
    ("max_cost_usd", "-1"), ("max_cost_usd", True),
    ("max_active_seconds", "Infinity"), ("max_active_seconds", 0),
    ("max_active_seconds", True), ("max_active_seconds", 1.5),
])
def test_review_restart_rejects_nonfinite_or_invalid_budget_atomically(runtime, field, value):
    engine, model, pid, cid = runtime
    _, task, _, _ = failed_output(runtime, unknown_cost=False)
    service = ActionService(engine)
    shown = card(service, pid, cid, "task:" + task["id"])
    values = {"max_cost_usd": "3", "max_active_seconds": 600, field: value}
    before = deepcopy(engine.status(pid))
    with pytest.raises(WorkflowBlocked):
        submit(service, pid, cid, shown, "restart", values)
    assert engine.status(pid) == before
    assert model.calls == []


@pytest.mark.parametrize("accepted", [False, "true", 1])
def test_review_unknown_cost_acceptance_cannot_be_coerced(runtime, accepted):
    engine, model, pid, cid = runtime
    _, task, call, _ = failed_output(runtime)
    service = ActionService(engine)
    shown = card(service, pid, cid, "task:" + task["id"])
    before = len(engine.status(pid)["tasks"])
    with pytest.raises(WorkflowBlocked):
        submit(service, pid, cid, shown, "restart", {
            "max_cost_usd": "3", "max_active_seconds": 600,
            "accept_unknown_cost": accepted,
        })
    assert len(engine.status(pid)["tasks"]) == before
    assert engine.store.get(call["id"], pid)["usage"]["estimated_cost"] is None
    assert model.calls == []


def test_review_restart_card_expires_when_source_changes(runtime):
    engine, model, pid, cid = runtime
    _, task, _, _ = failed_output(runtime, unknown_cost=False)
    service = ActionService(engine)
    shown = card(service, pid, cid, "task:" + task["id"])
    engine.import_source(pid, cid, "新版原作：甲离开乙。", "原作 v2")
    before = len(engine.status(pid)["tasks"])
    with pytest.raises(WorkflowBlocked, match="action_stale"):
        submit(service, pid, cid, shown, "restart", {"max_cost_usd": "3", "max_active_seconds": 600})
    assert len(engine.status(pid)["tasks"]) == before
    assert model.calls == []


def test_review_restart_execution_cannot_silently_switch_authorized_source(runtime):
    engine, model, pid, cid = runtime
    _, task, _, _ = failed_output(runtime, unknown_cost=False)
    service = ActionService(engine)
    current = ref(engine.workflow.resolve(pid, "source_text"))
    receipt = submit(service, pid, cid, card(service, pid, cid, "task:" + task["id"]),
                     "restart", {"max_cost_usd": "3", "max_active_seconds": 600})
    engine.import_source(pid, cid, "未授权的新版本：甲离开乙。", "后续原作")
    asyncio.run(engine.tick(pid, cid))
    # Pausing is valid; executing the original fixed source is also valid.
    children = engine.store.list(pid, "task", filters={"parent_task_id": receipt["task_id"]})
    calls = [c for t in children for c in engine.store.list(pid, "model_call", filters={"task_id": t["id"]})]
    assert not calls
    if model.calls:
        runs = [r for t in children for r in engine.store.list(pid, "run", filters={"task_id": t["id"]})]
        assert runs and all(current in r["input_refs"] for r in runs)


@pytest.mark.parametrize("values", [{}, {"answer": "__custom__"}, {"answer": "__custom__", "text": "  "}, {"answer": "未提供的选项"}])
def test_review_question_required_and_select_options_enforced_in_backend(runtime, values):
    engine, model, pid, cid = runtime
    task = pending_question(runtime, ("唯一问题",))
    service = ActionService(engine)
    item = engine._task_data(task)["pending_user_items"][0]
    shown = card(service, pid, cid, "pending:" + item["id"])
    with pytest.raises(WorkflowBlocked):
        submit(service, pid, cid, shown, "answer", values)
    assert engine._task_data(task)["pending_user_items"][0]["state"] == "open"
    assert model.calls == []


def test_review_real_stage_needs_input_retains_options(runtime):
    engine, model, pid, cid = runtime
    _, task = initial_task(runtime, stage=3)
    with engine.store.transaction():
        from test_runtime import seed_knowledge_asset
        seed_knowledge_asset(engine, pid)
    question = {"question_id": "choice-1", "prompt": "保留哪条路线？", "reason": "决定后续范围",
                "target_field": "/payload/route", "suggested_answers": ["保留甲", "保留乙"], "blocking_scope": "step3"}
    model.responses = [{"result_kind": "needs_input", "payload": None, "questions": [question], "evidence_refs": [], "notes": []}]
    asyncio.run(engine.tick(pid, cid))
    item = engine._task_data(task)["pending_user_items"][0]
    for key in ("suggested_answers", "reason", "target_field", "blocking_scope"):
        assert item[key] == question[key]
    shown = card(ActionService(engine), pid, cid, "pending:" + item["id"])
    assert {o["value"] for o in shown["actions"][0]["fields"][0]["options"]} >= {"保留甲", "保留乙"}


def test_review_coordinator_needs_input_has_answerable_pending_card(runtime):
    engine, model, pid, cid = runtime
    engine.submit_message(pid, cid, "帮我处理")
    model.responses = [{"result_kind": "needs_input", "payload": None, "questions": [{
        "question_id": "intent-1", "prompt": "想执行什么？", "reason": "需要确定任务", "target_field": None,
        "suggested_answers": ["仅查看", "改编"], "blocking_scope": "coordinator"}], "evidence_refs": [], "notes": []}]
    asyncio.run(engine.tick(pid, cid))
    pending = engine.status(pid)["pending_user_items"]
    assert len(pending) == 1
    task = engine.store.get(pending[0]["task_id"], pid)
    assert task["state"] == "waiting_user"
    service = ActionService(engine)
    submit(service, pid, cid, card(service, pid, cid, "pending:" + pending[0]["id"]), "answer", {"answer": "仅查看"})
    def result(current_task, materials):
        return {"result_kind": "ready", "payload": {"reply": "当前没有产物",
                "source_message_kind": "request", "task_requests": []},
                "questions": [], "evidence_refs": [], "notes": []}
    model.responses = [result]
    prompts = []
    original_run = model.run
    async def capture(*args, **kwargs):
        prompts.append(args[6])
        return await original_run(*args, **kwargs)
    model.run = capture
    asyncio.run(engine.tick(pid, cid))
    assert model.calls == ["coordinator", "coordinator"]
    assert not engine.status(pid)["pending_user_items"]
    assert not [t for t in engine.status(pid)["tasks"] if t["state"] in ("paused", "failed")]
    assert prompts and all(text in prompts[-1] for text in ("帮我处理", "想执行什么", "仅查看"))


def test_review_invalidated_waiting_confirmation_has_explicit_recovery(runtime):
    engine, model, pid, cid = runtime
    task, version = pending_confirmation(runtime)
    service = ActionService(engine)
    item = engine._task_data(task)["pending_user_items"][0]
    old = card(service, pid, cid, "pending:" + item["id"])
    with engine.store.transaction():
        update(engine.store, engine.workflow.state(version), dependency_status="review_required")
    with pytest.raises(WorkflowBlocked, match="action_stale"):
        submit(service, pid, cid, old, "confirm")
    cards = service.list_cards(pid, cid)["cards"]
    pending = next(c for c in cards if c["id"] == old["id"])
    assert next(a for a in pending["actions"] if a["id"] == "confirm")["disabled_reason"]
    assert any(a["id"] == "restart" and not a["disabled_reason"] for c in cards for a in c["actions"])
    assert model.calls == [] and not engine.store.list(pid, "confirmation")


def test_review_chapter_change_reaches_explicit_planning_prompt(runtime, monkeypatch):
    engine, model, pid, cid = runtime
    engine.import_source(pid, cid, "甲见乙。", "原作")
    with engine.store.transaction():
        message = engine._message(pid, cid, "请改编全剧", role="user")
        root = engine._new_task(pid, cid, message, "generate", is_workflow=True, stages=[9], request="请改编全剧")
        engine._propose_chapters(root, [{"chapter_id": "chapter-a", "source_message_ids": [message["id"]]}],
                                 message, "建议一章", {"id": None})
        engine._transition(root, "waiting_user")
    service = ActionService(engine)
    item = engine._task_data(root)["pending_user_items"][0]
    change = "请按时间线分成三章，并把失踪事件放到第二章"
    submit(service, pid, cid, card(service, pid, cid, "pending:" + item["id"]), "request_changes", {"text": change})
    with engine.store.transaction():
        engine._advance_root(engine.store.get(root["id"], pid))
    planning = next(t for t in engine.status(pid)["tasks"] if engine._task_data(t).get("chapter_planning"))
    # Isolate the planning prompt path from unrelated upstream-material fixtures.
    monkeypatch.setattr(engine.workflow, "materials", lambda *args, **kwargs: [])
    prompts = []
    async def capture(stage, task, run, session, config, materials, text, token):
        prompts.append(text)
        raise WorkflowBlocked("review_synthetic_stop")
    monkeypatch.setattr(engine, "_invoke", capture)
    asyncio.run(engine._plan_chapters(planning, 1))
    assert prompts and change in prompts[0]
    assert model.calls == []


def test_manager_chapter_confirmation_queues_resume_without_stale_root(runtime):
    engine, model, pid, cid = runtime
    engine.import_source(pid, cid, "甲见乙。", "原作")
    with engine.store.transaction():
        message = engine._message(pid, cid, "请改编全剧", role="user")
        root = engine._new_task(pid, cid, message, "generate", is_workflow=True,
                                stages=list(range(1, 12)), request="请改编全剧")
        data = engine._task_data(root)
        data["manager_controlled"] = True
        engine._save_task_data(root, data)
        engine._propose_chapters(root, [{"chapter_id": "chapter-a", "source_message_ids": [message["id"]]}],
                                 message, "建议一章", {"id": None})
        engine._transition(root, "waiting_user")
    service = ActionService(engine)
    item = engine._task_data(root)["pending_user_items"][0]
    shown = card(service, pid, cid, "pending:" + item["id"])
    receipt = submit(service, pid, cid, shown, "confirm")
    assert receipt["status"] == "confirmed"
    current = engine.store.get(root["id"], pid)
    assert current["state"] == "queued"
    assert engine._task_data(current)["confirmed_chapter_plan_ref"]
    assert len([q for q in engine.store.list(pid, "queued_request")
                if engine._projection(pid, "queue_context", q["id"]).get("manager_resume")]) == 1
    assert model.calls == []


def test_review_changed_source_restart_regenerates_step1_and_step2_with_new_config(runtime):
    from test_error_reporting import failed_operation

    engine, model, pid, cid = runtime
    engine.import_source(pid, cid, "甲见乙。", "原作 v1")
    source_v1 = engine.workflow.resolve(pid, "source_text")
    from test_split_source_workflow import _output as split_source_output
    with engine.store.transaction():
        views = [engine.workflow.save(pid, kind, split_source_output(kind, ref(source_v1)),
                                      stage=1, inputs=[ref(source_v1)], effective=True)
                 for kind in ("source_global_events", "source_character_events")]
    task, run, call = failed_operation(engine, pid, cid)
    old_config = deepcopy(engine.store.get(run["config_version_id"], pid))
    with engine.store.transaction():
        engine._close_run(task, run, "paused", {"code": "output_limit_exceeded", "message": "截断", "retryable": False, "details": {}})
        engine._transition(engine.store.get(task["id"], pid), "paused", "output_limit_exceeded")
    draft = engine.config_service.draft(pid, {"model": {"max_output_tokens": 100000}})
    published = engine.config_service.publish(pid, draft["id"])
    engine.import_source(pid, cid, "甲与乙分别。", "原作 v2")
    source_v2 = engine.workflow.resolve(pid, "source_text")
    assert all(engine.workflow.state(view)["dependency_status"] == "review_required" for view in views)
    service = ActionService(engine)
    shown = card(service, pid, cid, "task:" + task["id"])
    assert next(a for a in shown["actions"] if a["id"] == "rerun_published")["disabled_reason"]
    receipt = submit(service, pid, cid, shown, "restart", {
        "accept_unknown_cost": True, "max_cost_usd": "3", "max_active_seconds": 600,
    })
    root = engine.store.get(receipt["task_id"], pid)
    assert engine._task_data(root)["source_ref"] == ref(source_v2)
    assert engine.store.get(call["id"], pid)["usage"]["estimated_cost"] is None
    assert engine.store.get(run["config_version_id"], pid) == old_config
    child = engine.store.get(receipt["stage_task_id"], pid)
    assert child["scope"]["stage"] == 1
    snapshot = engine.store.get(engine._task_data(child)["config_version_id"], pid)
    assert snapshot["values"]["model"]["max_output_tokens"] == 100000
    assert published["id"] in snapshot["resolved_from_ids"]
    model.responses = [source_response, source_response]
    async def drive():
        for _ in range(3):
            await engine.tick(pid, cid)
    asyncio.run(drive())
    assert model.calls == ["step1", "step1"]
    root = engine.store.get(root["id"], pid)
    assert root["state"] == "waiting_user"
    review = engine._task_data(root)["step1_review"]
    shown = card(service, pid, cid, "pending:" + next(item["id"] for item in
        engine._task_data(root)["pending_user_items"] if item["state"] == "open"))
    assert {target["record_id"] for target in shown["targets"]} == {
        fixed_ref["record_id"] for fixed_ref in review["artifact_refs"].values()}
    submit(service, pid, cid, shown, "confirm")
    async def continue_after_review():
        for _ in range(2):
            await engine.tick(pid, cid)
    asyncio.run(continue_after_review())
    assert model.calls == ["step1", "step1", "step2"]
    children = engine.store.list(pid, "task", filters={"parent_task_id": root["id"]})
    step2 = next(t for t in children if t["scope"]["stage"] == 2)
    assert step2["state"] == "waiting_user"
    assert engine.store.list(pid, "run", filters={"task_id": step2["id"]})
    assert engine._task_data(step2)["result_ref"] == ref(engine.workflow.resolve(
        pid, "source_knowledge_asset", effective=False))
    view_children = engine.store.list(pid, "task", filters={"parent_task_id": child["id"]})
    assert {engine._task_data(view)["step1_view"] for view in view_children} == {"global", "character"}
    view_runs = [run for view in view_children
                 for run in engine.store.list(pid, "run", filters={"task_id": view["id"]})]
    assert len(view_runs) == 2
    assert all(engine.store.get(run["config_version_id"], pid)["values"]["model"]["max_output_tokens"] == 100000
               for run in view_runs)
    assert all(ref(source_v2) in run["input_refs"] and ref(source_v1) not in run["input_refs"]
               for run in view_runs)


@pytest.mark.parametrize("remote_unknown", [False, True])
def test_review_resume_cannot_bypass_unknown_remote_or_usage(runtime, remote_unknown):
    engine, model, pid, cid = runtime
    _, task, _, _ = failed_output(runtime, remote_unknown=remote_unknown)
    service = ActionService(engine)
    shown = card(service, pid, cid, "task:" + task["id"])
    assert next(a for a in shown["actions"] if a["id"] == "resume")["disabled_reason"]
    before = deepcopy(engine.status(pid))
    with pytest.raises(WorkflowBlocked, match="action_unavailable"):
        submit(service, pid, cid, shown, "resume", {"additional_cost": "1", "additional_seconds": 60})
    assert engine.status(pid) == before and model.calls == []


def test_review_resume_preserves_config_and_budget_without_explicit_increment(runtime):
    engine, model, pid, cid = runtime
    root, task, _, run = failed_output(runtime, unknown_cost=False)
    with engine.store.transaction():
        engine._transition(engine.store.get(task["id"], pid), "paused", "turn_limit")
    before_budget = deepcopy(engine.store.get(root["id"], pid)["budget"])
    service = ActionService(engine)
    submit(service, pid, cid, card(service, pid, cid, "task:" + task["id"]), "resume")
    assert engine.store.get(root["id"], pid)["budget"] == before_budget
    assert engine._task_data(task)["config_version_id"] == run["config_version_id"]
    assert engine.store.get(task["id"], pid)["state"] == "queued"
    assert model.calls == []


def test_review_superseded_workflow_cannot_resume_via_legacy_control(runtime):
    engine, model, pid, cid = runtime
    root, task, _, _ = failed_output(runtime, unknown_cost=False)
    service = ActionService(engine)
    submit(service, pid, cid, card(service, pid, cid, "task:" + task["id"]), "restart",
           {"max_cost_usd": "3", "max_active_seconds": 600})
    for old in (root, task):
        with pytest.raises(WorkflowBlocked, match="task_superseded"):
            engine.control_task(pid, old["id"], "continue", additional_cost="0", additional_seconds=0)
        assert not engine._ancestors_allow(engine.store.get(old["id"], pid))
    assert model.calls == []
