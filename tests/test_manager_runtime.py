"""The real SDK manager calls a child Agent through a run-bound tool adapter."""
import asyncio
import json
from types import SimpleNamespace
from uuid import uuid4

import httpx
import psycopg
import pytest
from openai import AsyncOpenAI
from psycopg import sql
from psycopg.conninfo import make_conninfo

from branch_agent.configuration import ConfigService
from branch_agent.actions import ActionService
from branch_agent.engine import Engine
from branch_agent.model_service import ModelService
from branch_agent.model_service import ask_user_request, manager_halt_request, manager_tool_behavior
from branch_agent.records import new_record
from branch_agent.storage import Store
from branch_agent.workflow import all_records, ref, update


@pytest.fixture
def runtime(tmp_path):
    base_dsn = "postgresql:///branch_agent_local"
    namespace = "manager_test_" + uuid4().hex
    with psycopg.connect(base_dsn, autocommit=True) as connection:
        connection.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(namespace)))
    store = Store(make_conninfo(base_dsn, options=f"-c search_path={namespace}"), tmp_path / "blobs")
    store.migrate()
    project = store.put(new_record("project", None, owner_account_id="manager-test", title="Manager"))
    conversation = store.put(new_record("conversation", project["id"], title="Story"))
    yield store, project["id"], conversation["id"]
    store.close()
    with psycopg.connect(base_dsn, autocommit=True) as connection:
        connection.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(namespace)))


def response(output):
    return {"id": "resp_" + uuid4().hex, "object": "response", "created_at": 1,
            "status": "completed", "model": "gpt-5.6-sol", "output": output,
            "parallel_tool_calls": False,
            "usage": {"input_tokens": 100, "output_tokens": 50, "total_tokens": 150,
                      "input_tokens_details": {"cached_tokens": 0},
                      "output_tokens_details": {"reasoning_tokens": 0}}}


def call(name, arguments, index):
    return response([{"id": "fc_" + str(index), "type": "function_call",
                      "call_id": "call_" + str(index), "name": name,
                      "arguments": json.dumps(arguments, ensure_ascii=False), "status": "completed"}])


def message(value):
    return response([{"id": "msg_" + uuid4().hex, "type": "message", "role": "assistant",
                      "status": "completed", "content": [{"type": "output_text",
                      "text": json.dumps(value, ensure_ascii=False), "annotations": []}]}])


def plain_message(value):
    return response([{"id": "msg_" + uuid4().hex, "type": "message", "role": "assistant",
                      "status": "completed", "content": [{"type": "output_text",
                      "text": value, "annotations": []}]}])


def step1_view_message(engine, project, original, request_data):
    """Answer either independently dispatched Step 1 view request."""
    source_ref = ref(engine.workflow.resolve(project, "source_text"))
    anchor = {"source_ref": source_ref, "start_utf16": 0, "end_utf16": 4,
              "exact_quote": original, "prefix": None, "suffix": None}
    instructions = request_data.get("instructions", "")
    schema_name = ("source_global_events" if "当前独立产物：全局事件" in instructions
                   else "source_character_events" if "当前独立产物：主要人物事件" in instructions
                   else "")
    assert schema_name in ("source_global_events", "source_character_events")
    payload = {"source_ref": source_ref, "covered_source_anchors": [anchor],
               "remaining_source_anchors": []}
    if schema_name == "source_global_events":
        payload["global_events"] = [{"event_id": "event-1", "title": "相遇",
            "summary": original, "narrative_order": 0, "story_time": None,
            "character_ids": [], "source_anchors": [anchor]}]
    else:
        payload["character_views"] = [{"character_id": "甲", "name": "甲", "aliases": [],
            "description": "原作人物", "events": [{"character_event_id": "character-event-1",
            "title": "甲见乙", "summary": original, "narrative_order": 0, "story_time": None,
            "involvement": "甲遇见乙", "source_anchors": [anchor]}]}]
    return message({"result_kind": "ready", "payload": payload, "questions": [],
                    "evidence_refs": [source_ref], "notes": []})


def test_ask_user_result_requires_a_real_tool_marker():
    assert ask_user_request('{"result_kind":"needs_input"}') is None
    assert ask_user_request('{"_harness_tool":"ask_user","questions":['
        '{"prompt":"玩家扮演谁？","suggested_answers":["韩立","其他"]}]}') == {
        "__ask_user__": [{"question_id": "ask-user-1", "prompt": "玩家扮演谁？",
                          "suggested_answers": ["韩立", "其他"]}]}


def test_manager_halt_marker_is_separate_from_user_questions():
    marker = '{"_harness_tool":"manager_halt","receipt":{"status":"needs_user_input"}}'
    assert manager_halt_request(marker) == {"status": "needs_user_input"}
    assert ask_user_request(marker) is None


def test_completed_workflow_receipt_stops_the_manager_before_another_model_turn():
    for tool_name in ("run_stage", "finish_workflow"):
        result = manager_tool_behavior(None, [SimpleNamespace(
            tool=SimpleNamespace(name=tool_name),
            output=json.dumps({"status": "completed", "workflow_id": "workflow-1"}))])
        assert result.is_final_output
        assert manager_halt_request(result.final_output)["status"] == "completed"


@pytest.mark.asyncio
async def test_step11_delivery_closes_manager_workflow_without_model_finish_call(runtime):
    store, project, conversation = runtime
    engine = Engine(store, ModelService(store), ConfigService(store))
    with store.transaction():
        source_message = engine._message(project, conversation, "完成改编", role="user")
        root = engine._new_task(project, conversation, source_message, "generate")
        data = engine._task_data(root)
        data.update(manager_controlled=True, is_workflow=True, stages=[11],
                    chapter_ids=["ch01"], fresh_start=True)
        engine._save_task_data(root, data)
        child = engine._new_task(project, conversation, source_message, "generate",
                                 stage=11, chapter="ch01", parent=root)

    async def deliver(task, _token):
        with store.transaction():
            engine._event(project, "project.delivered", {"artifact_ref": None},
                          conversation=conversation, task=task["id"])
            engine._transition(store.get(task["id"], project_id=project), "succeeded")

    engine._execute = deliver
    from branch_agent.manager import build_manager_tools
    tool = next(tool for tool in build_manager_tools(engine, None, None,
                     source_message, [], None) if tool.name == "run_stage")
    receipt = json.loads(await tool.on_invoke_tool(SimpleNamespace(tool_name="run_stage"),
                                                   '{"stage":11,"chapter_id":"ch01"}'))
    assert receipt["status"] == "completed"
    assert receipt["task_id"] == child["id"]
    assert store.get(root["id"], project_id=project)["state"] == "succeeded"


@pytest.mark.asyncio
async def test_child_question_stops_manager_before_next_stage_or_duplicate_confirmation(runtime):
    store, project, conversation = runtime
    calls = []

    def handler(request):
        calls.append(json.loads(request.content))
        number = len(calls)
        if number == 1:
            return httpx.Response(200, json=call("begin_adaptation", {"request": "完整改编",
                "source_is_current_message": False, "stage": None, "chapter_id": None}, number))
        if number == 2:
            return httpx.Response(200, json=call("run_stage", {"stage": 1,
                "chapter_id": None}, number))
        pytest.fail("等待用户回答后不应再次调用模型")

    client = AsyncOpenAI(api_key="local-mock", http_client=httpx.AsyncClient(
        transport=httpx.MockTransport(handler)), max_retries=0)
    engine = Engine(store, ModelService(store, client), ConfigService(store))
    async def wait_for_user(task, _token):
        with store.transaction():
            shown = engine._message(project, conversation, "请确定原作中的时间顺序。", task=task["id"])
            pending = engine._wait_item(task, "question", shown, "请确定原作中的时间顺序。")
            data = engine._task_data(task)
            data["pending_user_items"] = [pending]
            engine._save_task_data(task, data)
            engine._transition(task, "waiting_user")

    engine._execute = wait_for_user
    engine.import_source(project, conversation, "甲见乙。", "测试原作")
    engine.submit_message(project, conversation, "开始改编")
    await engine.tick(project, conversation)
    assert len(calls) == 2
    root = next(task for task in all_records(store, project, "task")
                if engine._task_data(task).get("manager_controlled"))
    assert root["state"] == "waiting_user"
    children = all_records(store, project, "task", parent_task_id=root["id"])
    assert [(engine._task_data(task)["stage"], task["state"]) for task in children] == [(1, "waiting_user")]
    cards = ActionService(engine).list_cards(project, conversation)["cards"]
    assert [card["kind"] for card in cards] == ["question"]
    assert "时间顺序" in cards[0]["description"]
    assert not any(task["pause_reason"] == "confirmation_candidate_ambiguous"
                   for task in all_records(store, project, "task"))
    from branch_agent.manager import build_manager_tools
    coordinator = next(task for task in all_records(store, project, "task")
                       if engine._task_data(task).get("coordinator"))
    run = next(run for run in all_records(store, project, "run")
               if run["task_id"] == coordinator["id"])
    source_message = store.get(root["requested_by_message_id"], project_id=project)
    stage_tool = next(tool for tool in build_manager_tools(engine, coordinator, run, source_message,
                           [], None, continuation=True) if tool.name == "run_stage")
    receipt = json.loads(await stage_tool.on_invoke_tool(SimpleNamespace(tool_name="run_stage"),
                                                         '{"stage":4,"chapter_id":null}'))
    assert receipt["status"] == "needs_user_input"
    assert receipt["pending"][0]["task_id"] == children[0]["id"]
    assert len(all_records(store, project, "task", parent_task_id=root["id"])) == 1
    await client.close()


@pytest.mark.asyncio
async def test_missing_upstream_does_not_dispatch_child_or_create_user_question(runtime):
    store, project, conversation = runtime
    calls = []

    def handler(request):
        calls.append(json.loads(request.content))
        if len(calls) == 1:
            return httpx.Response(200, json=call("begin_adaptation", {"request": "完整改编",
                "source_is_current_message": False, "stage": None, "chapter_id": None}, 1))
        if len(calls) == 2:
            return httpx.Response(200, json=call("run_stage", {"stage": 4, "chapter_id": None}, 2))
        pytest.fail("上游材料缺失后不应继续调用模型")

    client = AsyncOpenAI(api_key="local-mock", http_client=httpx.AsyncClient(
        transport=httpx.MockTransport(handler)), max_retries=0)
    engine = Engine(store, ModelService(store, client), ConfigService(store))
    engine.import_source(project, conversation, "甲见乙。", "测试原作")
    engine.submit_message(project, conversation, "开始改编")
    await engine.tick(project, conversation)
    root = next(task for task in all_records(store, project, "task")
                if engine._task_data(task).get("manager_controlled"))
    assert root["state"] == "paused"
    assert root["pause_reason"] == "manager_prerequisite_pending"
    assert all_records(store, project, "task", parent_task_id=root["id"]) == []
    assert ActionService(engine).list_cards(project, conversation)["cards"] == []
    await client.close()


@pytest.mark.asyncio
async def test_coordinator_ask_user_stops_then_resumes_from_answer(runtime):
    store, project, conversation = runtime
    calls = []

    def handler(request):
        data = json.loads(request.content)
        calls.append(data)
        if len(calls) == 1:
            assert "ask_user" in {tool["name"] for tool in data["tools"]}
            return httpx.Response(200, json=call("ask_user", {"questions": [{
                "prompt": "玩家扮演哪个角色？", "suggested_answers": ["韩立", "其他"]}]}, 1))
        assert len(calls) == 2
        assert any(item.get("type") == "function_call_output" and item.get("call_id") == "call_1"
                   for item in data["input"])
        final = {"result_kind": "ready", "payload": {"reply": "已记录玩家角色。",
                 "source_message_kind": "request", "task_requests": []},
                 "questions": [], "evidence_refs": [], "notes": []}
        return httpx.Response(200, json=message(final))

    client = AsyncOpenAI(api_key="local-mock", http_client=httpx.AsyncClient(
        transport=httpx.MockTransport(handler)), max_retries=0)
    engine = Engine(store, ModelService(store, client), ConfigService(store))
    engine.import_source(project, conversation, "甲见乙。", "测试原作")
    engine.submit_message(project, conversation, "开始改编")
    await engine.tick(project, conversation)
    cards = ActionService(engine).list_cards(project, conversation)["cards"]
    questions = [card for card in cards if card["kind"] == "question"]
    assert len(questions) == 1
    card = questions[0]
    assert "玩家扮演哪个角色" in card["title"] or "玩家扮演哪个角色" in card["description"]
    assert len(calls) == 1
    ActionService(engine).submit(project, conversation, card["id"], "answer", card["revision"], {"answer": "韩立"})
    await engine.tick(project, conversation)
    assert len(calls) == 2
    assert not [card for card in ActionService(engine).list_cards(project, conversation)["cards"]
                if card["kind"] == "question"]
    await client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("with_confirmation_id", [False, True])
async def test_coordinator_does_not_duplicate_an_existing_pending_confirmation(runtime, with_confirmation_id):
    store, project, conversation = runtime
    calls = []

    def handler(request):
        calls.append(json.loads(request.content))
        return httpx.Response(200, json=call("ask_user", {"questions": [{
            "prompt": "是否确认已有产物？", "suggested_answers": ["确认"],
            **({"confirmation_task_id": existing_task_id} if with_confirmation_id else {})}]}, 1))

    client = AsyncOpenAI(api_key="local-mock", http_client=httpx.AsyncClient(
        transport=httpx.MockTransport(handler)), max_retries=0)
    engine = Engine(store, ModelService(store, client), ConfigService(store))
    engine.import_source(project, conversation, "甲见乙。", "测试原作")
    existing_task_id = None
    with store.transaction():
        origin = engine._message(project, conversation, "开始改编", role="user")
        task = engine._new_task(project, conversation, origin, "generate", stage=2)
        existing_task_id = task["id"]
        shown = engine._message(project, conversation, "请确认 Step2 产物", task=task["id"])
        pending = engine._wait_item(task, "confirmation", shown, "请确认 Step2 产物")
        data = engine._task_data(task)
        data["pending_user_items"] = [pending]
        engine._save_task_data(task, data)
        update(store, task, state="waiting_user")
    engine.submit_message(project, conversation, "继续")
    await engine.tick(project, conversation)
    assert len(calls) == 1
    assert not [candidate for candidate in all_records(store, project, "task")
                if engine._task_data(candidate).get("coordinator")
                and any(item["state"] == "open" for item in engine._task_data(candidate)["pending_user_items"])]
    assert engine._task_data(store.get(task["id"], project_id=project))["pending_user_items"][0]["state"] == "open"
    await client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("structured", [True, False])
@pytest.mark.parametrize("finish_on_resume", [True, False])
async def test_manager_dispatches_stage_with_separate_audited_run(runtime, structured, finish_on_resume):
    store, project, conversation = runtime
    original = "甲见乙。"
    calls = []

    def handler(request):
        data = json.loads(request.content)
        calls.append(data)
        if len(calls) == 1:
            names = {tool["name"] for tool in data["tools"]}
            assert {"begin_adaptation", "run_stage", "confirm_pending", "propose_chapters"} <= names
            return httpx.Response(200, json=call("begin_adaptation", {"request": "只执行 Step1", "source_is_current_message": False,
                "stage": 1, "chapter_id": None}, 1))
        if len(calls) == 2:
            return httpx.Response(200, json=call("run_stage", {"stage": 1, "chapter_id": None}, 2))
        if len(calls) in (3, 4):
            return httpx.Response(200, json=step1_view_message(engine, project, original, data))
        if len(calls) == 6:
            return httpx.Response(200, json=call("finish_workflow", {}, 6) if finish_on_resume
                                  else message({"result_kind": "ready", "payload": {"reply": "等待继续。",
                                                "source_message_kind": "request", "task_requests": []},
                                                "questions": [], "evidence_refs": [], "notes": []})
                                  if structured else plain_message("等待继续。"))
        assert len(calls) == 5
        if len(calls) == 5:
            assert any(item.get("type") == "function_call_output" and item["call_id"] == "call_2"
                       for item in data["input"])
        final = {"result_kind": "ready", "payload": {"reply": "Step1 已完成。",
                 "source_message_kind": "request", "task_requests": []},
                 "questions": [], "evidence_refs": [], "notes": []}
        return httpx.Response(200, json=message(final) if structured else plain_message("Step1 已完成。"))

    client = AsyncOpenAI(api_key="local-mock", http_client=httpx.AsyncClient(
        transport=httpx.MockTransport(handler)), max_retries=0)
    engine = Engine(store, ModelService(store, client), ConfigService(store))
    if not structured:
        draft = engine.config_service.draft(project, {"output": {"structured": {"coordinator": False}}})
        engine.config_service.publish(project, draft["id"])
    engine.import_source(project, conversation, original, "测试原作")
    engine.submit_message(project, conversation, "开始改编")
    await engine.tick(project, conversation)
    stages = [(task["scope"]["stage"], task["state"])
              for task in all_records(store, project, "task")]
    assert (1, "succeeded") in stages
    assert not any(stage == 2 for stage, _ in stages)
    assert engine.workflow.resolve(project, "source_global_events")
    assert engine.workflow.resolve(project, "source_character_events")
    coordinator_runs = [run for run in all_records(store, project, "run") if run["agent_key"] == "conversation_coordinator"]
    child_runs = [run for run in all_records(store, project, "run")
                  if run["agent_key"] in ("source_global_parser", "source_character_parser")]
    assert len(coordinator_runs) == 1
    assert {run["agent_key"] for run in child_runs} == {"source_global_parser", "source_character_parser"}
    assert len({coordinator_runs[0]["session_id"], *(run["session_id"] for run in child_runs)}) == 3
    trace_rows=store._connection().execute(
        'SELECT run_id,kind,name FROM sdk_trace_spans WHERE project_id=%s',(project,)).fetchall()
    assert any(str(row['run_id'])==coordinator_runs[0]['id'] and row['kind']=='function'
               and row['name']=='run_stage' for row in trace_rows)
    assert all(any(str(row['run_id']) == child['id'] and row['kind'] == 'agent'
                   for row in trace_rows) for child in child_runs)
    assert {item["tool_name"] for item in all_records(store, project, "tool_call")} >= {"begin_adaptation", "run_stage"}
    assert len(calls) == 5
    assert [(item['state'], engine._projection(project, 'queue_context', item['id']).get('manager_resume'))
            for item in all_records(store, project, 'queued_request')] == [('dispatched', None), ('pending', True)]
    continuation = next(item for item in all_records(store, project, "queued_request")
                        if engine._projection(project, 'queue_context', item['id']).get('manager_resume'))
    assert store.get(continuation["source_message_id"], project_id=project)["visibility"] == "internal"
    await engine.tick(project, conversation)
    root = next(task for task in all_records(store, project, "task")
                if engine._task_data(task).get("manager_controlled"))
    assert store.get(root["id"], project_id=project)["state"] == ("succeeded" if finish_on_resume else "paused")
    if not finish_on_resume:
        assert store.get(root["id"], project_id=project)["pause_reason"] == "manager_no_progress"
        assert len([item for item in all_records(store, project, "queued_request") if item["state"] == "pending"]) == 0
    assert len(calls) == 6  # finish_workflow closes the SDK turn immediately.
    await client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("via_message", [False, True])
async def test_manager_requests_one_step2_confirmation_with_ask_user(runtime, via_message):
    store, project, conversation = runtime
    calls = []
    original = "甲见乙。"

    def handler(request):
        data = json.loads(request.content)
        calls.append(data)
        number = len(calls)
        if number == 1:
            return httpx.Response(200, json=call("begin_adaptation", {"request": "完整改编",
                "source_is_current_message": False, "stage": None, "chapter_id": None}, number))
        if number in (2, 5):
            return httpx.Response(200, json=call("run_stage", {"stage": 1 if number == 2 else 2,
                "chapter_id": None}, number))
        if number in (3, 4):
            return httpx.Response(200, json=step1_view_message(engine, project, original, data))
        if number == 6:
            return httpx.Response(200, json=plain_message(
                "[[GLOBAL_EVENT_ANALYSIS]]\n全局事件相遇。\n[[CHARACTER_EVENT_ANALYSIS]]\n人物甲与乙相遇。"))
        if number == 7:
            step2 = next(task for task in all_records(store, project, "task")
                         if engine._task_data(task).get("stage") == 2)
            assert step2["state"] == "waiting_user"
            assert not engine._task_data(step2)["pending_user_items"]
            return httpx.Response(200, json=call("ask_user", {"questions": [{
                "prompt": "这两份 Step2 分析是否可以作为后续改编依据？",
                "suggested_answers": ["确认"], "confirmation_task_id": step2["id"]}]}, number))
        if number == 8 and via_message:
            step2 = next(task for task in all_records(store, project, "task")
                         if engine._task_data(task).get("stage") == 2)
            pending_id = engine._task_data(step2)["pending_user_items"][0]["id"]
            return httpx.Response(200, json=call("confirm_pending", {"pending_item_id": pending_id}, number))
        final = {"result_kind": "ready", "payload": {"reply": "已收到确认。",
                 "source_message_kind": "request", "task_requests": []},
                 "questions": [], "evidence_refs": [], "notes": []}
        return httpx.Response(200, json=message(final))

    client = AsyncOpenAI(api_key="local-mock", http_client=httpx.AsyncClient(
        transport=httpx.MockTransport(handler)), max_retries=0)
    engine = Engine(store, ModelService(store, client), ConfigService(store))
    engine.import_source(project, conversation, original, "测试原作")
    engine.submit_message(project, conversation, "开始改编")
    await engine.tick(project, conversation)
    root = next(task for task in all_records(store, project, "task")
                if engine._task_data(task).get("manager_controlled"))
    step2 = next(task for task in all_records(store, project, "task")
                 if task["parent_task_id"] == root["id"] and engine._task_data(task).get("stage") == 2)
    assert step2["state"] == "waiting_user"
    assert len(engine._task_data(step2)["pending_user_items"]) == 1
    assert len(engine._task_data(step2)["pending_user_items"][0]["targets"]) == 2
    actions = ActionService(engine)
    assert actions.list_cards(project, conversation)["cards"][0]["description"] == "这两份 Step2 分析是否可以作为后续改编依据？"
    if via_message:
        engine.submit_message(project, conversation, "确认这两份分析")
        await engine.tick(project, conversation)
    else:
        for item in engine._task_data(step2)["pending_user_items"]:
            shown = next(card for card in actions.list_cards(project, conversation)["cards"]
                         if card["id"] == "pending:" + item["id"])
            actions.submit(project, conversation, shown["id"], "confirm", shown["revision"], {})
    assert store.get(step2["id"], project_id=project)["state"] == "succeeded"
    assert store.get(root["id"], project_id=project)["state"] == "queued"
    pending = [item for item in all_records(store, project, "queued_request") if item["state"] == "pending"]
    assert len(pending) == 1
    assert engine._projection(project, "queue_context", pending[0]["id"])["manager_resume"] is True
    if not via_message:
        await engine.tick(project, conversation)
    assert len([run for run in all_records(store, project, "run")
                if run["agent_key"] == "conversation_coordinator"]) == 2
    assert len(calls) == (9 if via_message else 8)
    await client.close()
