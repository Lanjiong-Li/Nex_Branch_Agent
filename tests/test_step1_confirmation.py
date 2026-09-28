"""Step1 review is a fixed-version workflow action, not an ordinary chat answer."""

import asyncio
from copy import deepcopy
from uuid import uuid4

import pytest

from branch_agent.actions import ActionService
from branch_agent.app import BASE
from branch_agent.records import new_record
from branch_agent.workflow import WorkflowBlocked, all_records, body, ref
from test_action_api import action_api
from test_runtime import runtime


KINDS = {"source_global_events", "source_character_events"}


def completed_step1(runtime, *, manager_controlled=False):
    engine, model, project, conversation = runtime
    imported = engine.import_source(project, conversation, "甲见乙。", "原作", start_step1=True)
    root_id = imported["step1_task"]["id"]
    if manager_controlled:
        asyncio.run(engine.tick(project, conversation))  # Dispatch the Step1 child first.
        with engine.store.transaction():
            root = engine.store.get(root_id, project_id=project)
            data = engine._task_data(root)
            data["manager_controlled"] = True
            engine._save_task_data(root, data)
    for _ in range(8):
        asyncio.run(engine.tick(project, conversation))
        root = engine.store.get(root_id, project_id=project)
        if engine._task_data(root).get("step1_review"):
            break
    else:
        pytest.fail(f"Step1 did not create fixed-version review: root={root['state']}, "
                    f"model_calls={model.calls}, refs={engine._task_data(root).get('result_refs')}")
    root = engine.store.get(root_id, project_id=project)
    service = ActionService(engine)
    cards = service.list_cards(project, conversation)["cards"]
    matches = [card for card in cards if card["kind"] == "confirmation"
               and {engine.store.get(target["record_id"], project_id=project)["artifact_kind"]
                    for target in card["targets"]} == KINDS]
    assert len(matches) == 1
    return engine, model, project, conversation, imported, root, matches[0]


def step2_roots(engine, project, conversation):
    return [task for task in all_records(engine.store, project, "task", conversation_id=conversation)
            if engine._task_data(task).get("is_workflow")
            and engine._task_data(task).get("stages") == [2]]


@pytest.mark.parametrize("manager_controlled", [False, True])
def test_step1_confirmation_closes_root_and_starts_one_step2_from_both_fixed_views(runtime, manager_controlled):
    engine, model, project, conversation, imported, root, card = completed_step1(
        runtime, manager_controlled=manager_controlled)
    data = engine._task_data(root)
    assert root["state"] == "waiting_user"
    assert bool(data.get("manager_controlled")) is manager_controlled
    fixed = data["step1_review"]["artifact_refs"]
    assert set(fixed) == KINDS
    assert {(target["record_id"], target["version"]) for target in card["targets"]} == {
        (value["record_id"], value["version"]) for value in fixed.values()}
    assert {target["version"] for target in card["targets"]} == {"1"}
    assert not step2_roots(engine, project, conversation)

    receipt = ActionService(engine).submit(project, conversation, card["id"], "confirm", card["revision"], {})
    assert receipt["status"] == "confirmed"
    assert engine.store.get(root["id"], project_id=project)["state"] == "succeeded"
    confirmations = all_records(engine.store, project, "confirmation")
    assert {item["subject"]["record_id"] for item in confirmations} == {
        value["record_id"] for value in fixed.values()}
    roots = step2_roots(engine, project, conversation)
    assert len(roots) == 1
    assert engine._task_data(roots[0])["manager_controlled"] is True
    assert engine._task_data(roots[0])["source_ref"] == ref(imported["source"])
    assert any(queue["state"] == "pending" for queue in all_records(engine.store, project, "queued_request"))
    assert model.calls.count("step2") == 0

    with pytest.raises(WorkflowBlocked, match="action_not_found|action_stale"):
        ActionService(engine).submit(project, conversation, card["id"], "confirm", card["revision"], {})
    assert len(step2_roots(engine, project, conversation)) == 1
    assert len(all_records(engine.store, project, "confirmation")) == 2


@pytest.mark.parametrize("changed", ["view", "source"])
def test_step1_confirmation_rejects_changed_fixed_inputs(runtime, changed):
    engine, model, project, conversation, imported, root, card = completed_step1(runtime)
    with engine.store.transaction():
        if changed == "source":
            engine.workflow.save(project, "source_text", "甲与乙分别。", effective=True)
        else:
            version = engine.workflow.resolve(project, "source_global_events")
            revised = deepcopy(body(engine.store, version))
            revised["payload"]["global_events"][0]["summary"] = "甲见乙，事情有了变化。"
            engine.workflow.save(project, "source_global_events", revised, stage=1,
                                 inputs=[ref(imported["source"])], effective=True)
    with pytest.raises(WorkflowBlocked, match="action_stale|action_unavailable"):
        ActionService(engine).submit(project, conversation, card["id"], "confirm", card["revision"], {})
    assert engine.store.get(root["id"], project_id=project)["state"] != "succeeded"
    assert not all_records(engine.store, project, "confirmation")
    assert not step2_roots(engine, project, conversation)
    assert model.calls.count("step2") == 0


def test_step1_change_request_keeps_work_in_step1(runtime):
    engine, model, project, conversation, _, root, card = completed_step1(runtime)
    receipt = ActionService(engine).submit(project, conversation, card["id"], "request_changes",
                                           card["revision"], {"text": "请修正第一件事的边界"})
    assert receipt["status"] == "queued"
    assert not all_records(engine.store, project, "confirmation")
    assert not step2_roots(engine, project, conversation)
    assert engine.store.get(root["id"], project_id=project)["state"] != "succeeded"
    assert model.calls.count("step2") == 0


def test_manager_controlled_step1_change_queues_manager_resume_without_step2(runtime):
    engine, model, project, conversation, _, root, card = completed_step1(
        runtime, manager_controlled=True)
    assert engine._task_data(root)["manager_controlled"] is True
    receipt = ActionService(engine).submit(project, conversation, card["id"], "request_changes",
                                           card["revision"], {"text": "重新核对事件边界"})
    replacement = engine.store.get(receipt["task_id"], project_id=project)
    assert replacement["id"] != root["id"]
    assert engine._task_data(replacement)["manager_controlled"] is True
    assert replacement["state"] == "queued"
    assert engine.store.get(root["id"], project_id=project)["state"] == "stopped"
    assert not step2_roots(engine, project, conversation)
    assert any(queue["state"] == "pending" and
               engine._projection(project, "queue_context", queue["id"]).get("manager_resume")
               for queue in all_records(engine.store, project, "queued_request"))
    assert model.calls.count("step2") == 0


def test_step1_confirmation_api_replay_does_not_start_a_second_step2(action_api):
    client, engine, model, project, conversation = action_api
    _, _, _, _, _, _, card = completed_step1((engine, model, project, conversation))
    endpoint = BASE + f"/projects/{project}/conversations/{conversation}/actions"
    payload = {"card_id": card["id"], "action_id": "confirm",
               "expected_revision": card["revision"], "values": {}}
    client.headers["Idempotency-Key"] = str(uuid4())
    first = client.post(endpoint, json=payload)
    replay = client.post(endpoint, json=payload)
    assert first.status_code == replay.status_code == 200
    assert first.json() == replay.json()
    assert len(step2_roots(engine, project, conversation)) == 1
    assert len(all_records(engine.store, project, "confirmation")) == 2
    assert model.calls.count("step2") == 0


def test_step2_materials_require_review_even_from_another_conversation(runtime):
    engine, _, project, conversation, _, root, card = completed_step1(runtime)
    fixed = engine._task_data(root)["step1_review"]["artifact_refs"]
    with pytest.raises(WorkflowBlocked) as blocked:
        engine.workflow.materials(project, 2)
    assert blocked.value.reason == "confirmation_required"

    other = engine.store.put(new_record("conversation", project, title="另一段对话"))
    with engine.store.transaction():
        message = engine._message(project, other["id"], "直接执行 Step2", role="user")
    request = {"intent": "generate", "stage": 2, "chapter_id": None,
               "target_ref": None, "request": "直接执行 Step2",
               "source_message_ids": [message["id"]],
               "requested_confirmation_paths": []}
    with pytest.raises(WorkflowBlocked) as blocked:
        with engine.store.transaction():
            engine._apply_request(request, message, [], None, None)
    assert blocked.value.reason == "confirmation_required"
    assert not step2_roots(engine, project, other["id"])

    ActionService(engine).submit(project, conversation, card["id"], "confirm", card["revision"], {})
    materials = engine.workflow.materials(project, 2)
    assert {item["kind"]: item["ref"] for item in materials} == fixed


@pytest.mark.parametrize("kind", ["source_global_events", "source_character_events"])
def test_plain_chat_cannot_confirm_a_superseded_step1_view(runtime, kind):
    engine, _, project, conversation, imported, root, _ = completed_step1(runtime)
    old_ref = engine._task_data(root)["step1_review"]["artifact_refs"][kind]
    shown = engine._projection(project, "presentations", conversation, targets=[])["targets"]
    with engine.store.transaction():
        previous = engine.workflow.fixed_version(project, old_ref)
        revised = deepcopy(body(engine.store, previous))
        if kind == "source_global_events":
            revised["payload"]["global_events"][0]["summary"] = "甲与乙重逢。"
        else:
            revised["payload"]["character_views"][0]["events"][0]["summary"] = "甲与乙重逢。"
        engine.workflow.save(project, kind, revised, stage=1,
                             inputs=[ref(imported["source"])], effective=True)
        user = engine._message(project, conversation, "确认旧版事件视图", role="user")
    request = {"source_message_ids": [user["id"]], "target_ref": old_ref,
               "requested_confirmation_paths": [""]}
    with pytest.raises(WorkflowBlocked) as blocked:
        with engine.store.transaction():
            engine.workflow.confirm(project, request, user, shown)
    assert blocked.value.reason == "confirmation_candidate_changed"
    assert not all_records(engine.store, project, "confirmation")


def test_step1_event_selection_cannot_partially_confirm_a_view(runtime):
    engine, _, project, conversation, _, root, _ = completed_step1(runtime)
    fixed = engine._task_data(root)["step1_review"]["artifact_refs"]["source_global_events"]
    shown = engine._projection(project, "presentations", conversation, targets=[])["targets"]
    with engine.store.transaction():
        user = engine._message(project, conversation, "只确认第一个事件", role="user")
    request = {"source_message_ids": [user["id"]], "target_ref": fixed,
               "requested_confirmation_paths": ["/payload/global_events/0"]}
    with pytest.raises(WorkflowBlocked) as blocked:
        with engine.store.transaction():
            engine.workflow.confirm(project, request, user, shown)
    assert blocked.value.reason == "confirmation_scope_incomplete"
    assert not all_records(engine.store, project, "confirmation")
    assert engine.workflow.state(engine.workflow.fixed_version(project, fixed))["confirmation_status"] == "not_required"


def test_fixed_step2_run_inputs_cannot_bypass_step1_review(runtime):
    engine, _, project, conversation, _, root, card = completed_step1(runtime)
    fixed = engine._task_data(root)["step1_review"]["artifact_refs"]
    materials = []
    for kind in ("source_global_events", "source_character_events"):
        version = engine.workflow.fixed_version(project, fixed[kind])
        materials.append({"kind": kind, "schema_id": kind, "ref": fixed[kind],
                          "record": version, "content": body(engine.store, version)})
    values = engine.config_service.resolve(project, "step2")["values"]
    with pytest.raises(WorkflowBlocked) as blocked:
        engine._assert_source_analysis_contract(2, values, materials, project_id=project)
    assert blocked.value.reason == "confirmation_required"

    ActionService(engine).submit(project, conversation, card["id"], "confirm", card["revision"], {})
    engine._assert_source_analysis_contract(2, values, materials, project_id=project)
