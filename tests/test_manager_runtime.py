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
from branch_agent.workflow import WorkflowBlocked, all_records, ref, update
from test_runtime import step2_response
from test_read_tools import source_event_case


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


def mock_transport(handler):
    """Serve the same mock response through the coordinator's streamed route."""
    def handle(request):
        result=handler(request)
        if not json.loads(request.content).get('stream') or result.status_code!=200:
            return result
        payload=result.json()
        events=[]
        for output_index,item in enumerate(payload['output']):
            if item['type']!='message':continue
            for content_index,part in enumerate(item['content']):
                if part['type']!='output_text':continue
                content=part['text'];middle=max(1,len(content)//2)
                for chunk in (content[:middle],content[middle:]):
                    if chunk:
                        events.append({'type':'response.output_text.delta',
                            'sequence_number':len(events)+1,'item_id':item['id'],
                            'output_index':output_index,'content_index':content_index,
                            'logprobs':[],'delta':chunk})
        events.append({'type':'response.completed','sequence_number':len(events)+1,
                       'response':payload})
        body=''.join('data: '+json.dumps(item,ensure_ascii=False)+'\n\n' for item in events)
        return httpx.Response(200,headers={'content-type':'text/event-stream'},
                              content=(body+'data: [DONE]\n\n').encode())
    return httpx.MockTransport(handle)


@pytest.mark.asyncio
async def test_coordinator_shows_fixed_event_source_without_inserting_excerpt_into_session(runtime):
    store, project, conversation = runtime
    _, _, view = source_event_case(store, project, "开头。甲🌍乙相遇。结尾。",
                                   quotes=["甲🌍乙相遇。"])
    calls = []

    def handler(request):
        data = json.loads(request.content)
        calls.append(data)
        if len(calls) == 1:
            assert "show_event_source" in {tool["name"] for tool in data["tools"]}
            return httpx.Response(200, json=call("show_event_source", {"event_query": "GEV-1"}, 1))
        receipt = next(item for item in data["input"]
                       if item.get("type") == "function_call_output" and item["call_id"] == "call_1")
        value = json.loads(receipt["output"])
        assert value["status"] == "ok" and value["message_id"]
        assert "甲🌍乙相遇。" not in receipt["output"]
        return httpx.Response(200, json=message({"result_kind": "ready", "payload": {
            "reply": "已展示该事件的原文。", "source_message_kind": "request", "task_requests": []},
            "questions": [], "evidence_refs": [], "notes": []}))

    client = AsyncOpenAI(api_key="local-mock", http_client=httpx.AsyncClient(
        transport=mock_transport(handler)), max_retries=0)
    engine = Engine(store, ModelService(store, client), ConfigService(store))
    engine.submit_message(project, conversation, "查看相遇事件的原文")
    await engine.tick(project, conversation)

    events = [row for row in all_records(store, project, "runtime_event")
              if row["event_name"] == "event_source.presented"]
    assert len(events) == 1
    assert events[0]["payload"]["event_ref"]["record_id"] == view["artifact_id"]
    shown = store.get(events[0]["payload"]["message_id"], project_id=project)
    assert "甲🌍乙相遇。" in shown["content"]["text"]
    assert shown["session_id"] is None
    assert len(calls) == 2
    await client.close()


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
    schema_name = ("source_global_events" if "当前独立产物：作品事件视图" in instructions
                   else "source_character_events" if "当前独立产物：主要人物事件视图" in instructions
                   else "")
    assert schema_name in ("source_global_events", "source_character_events")
    payload = {"source_ref": source_ref, "covered_source_anchors": [anchor],
               "remaining_source_anchors": []}
    if schema_name == "source_global_events":
        payload["global_events"] = [{"event_id": "event-1", "title": "相遇",
            "summary": original, "analysis": "相遇建立人物关系并推动后续情节。",
            "narrative_order": 0, "story_time": None,
            "character_ids": [], "source_anchors": [anchor]}]
    else:
        payload["character_views"] = [{"character_id": "甲", "name": "甲", "aliases": [],
            "description": "原作人物", "events": [{"character_event_id": "character-event-1",
            "title": "甲见乙", "summary": original, "narrative_order": 0, "story_time": None,
            "involvement": "甲遇见乙"}]}]
    result = {"result_kind": "ready", "payload": payload, "questions": [],
              "evidence_refs": [source_ref], "notes": []}
    return message(result)


@pytest.mark.asyncio
async def test_begin_adaptation_queues_step1_without_a_manager_run_stage_call(runtime):
    store, project, conversation = runtime
    original = "甲见乙。"
    calls = []

    def handler(request):
        data = json.loads(request.content)
        calls.append(data)
        if len(calls) == 1:
            return httpx.Response(200, json=call("begin_adaptation", {
                "request": "完整改编", "source_is_current_message": False,
                "stage": None, "chapter_id": None}, 1))
        if len(calls) == 2:
            return httpx.Response(200, json=message({
                "result_kind": "ready", "payload": {"reply": "已开始处理原作。",
                    "source_message_kind": "request", "task_requests": []},
                "questions": [], "evidence_refs": [], "notes": []}))
        if len(calls) in (3, 4):
            return httpx.Response(200, json=step1_view_message(engine, project, original, data))
        pytest.fail("Step1 审阅前不应自动调用其他 Agent")

    client = AsyncOpenAI(api_key="local-mock", http_client=httpx.AsyncClient(
        transport=mock_transport(handler)), max_retries=0)
    engine = Engine(store, ModelService(store, client), ConfigService(store))
    engine.import_source(project, conversation, original, "测试原作")
    engine.submit_message(project, conversation, "开始改编")
    await engine.tick(project, conversation)

    root = next(task for task in all_records(store, project, "task")
                if engine._task_data(task).get("manager_controlled"))
    step1 = [task for task in all_records(store, project, "task")
             if task["parent_task_id"] == root["id"]
             and engine._task_data(task).get("stage") == 1]
    assert len(step1) == 1 and step1[0]["state"] == "queued"
    assert engine._task_data(root)["harness_scheduled"] is True
    assert not [item for item in all_records(store, project, "queued_request")
                if item["state"] == "pending"
                and engine._projection(project, "queue_context", item["id"]).get("manager_resume")]

    await engine.tick(project, conversation)
    assert store.get(root["id"], project_id=project)["state"] == "waiting_user"
    assert {task["agent_key"] for task in all_records(store, project, "run")
            if task["task_id"] in {child["id"] for child in all_records(store, project, "task")
                                   if child["parent_task_id"] == step1[0]["id"]}} == {
        "source_global_parser", "source_character_parser"}
    assert not [tool for tool in all_records(store, project, "tool_call")
                if tool["tool_name"] == "run_stage"]
    await client.close()


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


def test_harness_scheduled_receipt_stops_the_manager_before_another_model_turn():
    result = manager_tool_behavior(None, [SimpleNamespace(
        tool=SimpleNamespace(name="run_stage"),
        output=json.dumps({"status": "harness_scheduled", "workflow_id": "workflow-1"}))])
    assert result.is_final_output
    assert manager_halt_request(result.final_output)["status"] == "harness_scheduled"


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
async def test_child_question_stops_next_stage_and_chat_answer_requeues_harness_child(runtime):
    store, project, conversation = runtime
    calls = []

    def handler(request):
        calls.append(json.loads(request.content))
        number = len(calls)
        if number == 1:
            return httpx.Response(200, json=call("begin_adaptation", {"request": "完整改编",
                "source_is_current_message": False, "stage": None, "chapter_id": None}, number))
        if number == 2:
            return httpx.Response(200, json=message({"result_kind": "ready", "payload": {
                "reply": "已开始处理原作。", "source_message_kind": "request", "task_requests": []},
                "questions": [], "evidence_refs": [], "notes": []}))
        if number == 3:
            child = next(task for task in all_records(store, project, "task")
                         if engine._task_data(task).get("stage") == 1)
            pending_id = engine._task_data(child)["pending_user_items"][0]["id"]
            return httpx.Response(200, json=call("answer_pending", {"pending_item_id": pending_id}, number))
        if number == 4:
            return httpx.Response(200, json=message({"result_kind": "ready", "payload": {
                "reply": "已记录时间顺序。", "source_message_kind": "request", "task_requests": []},
                "questions": [], "evidence_refs": [], "notes": []}))
        pytest.fail("回答问题后不应执行其他 Agent")

    client = AsyncOpenAI(api_key="local-mock", http_client=httpx.AsyncClient(
        transport=mock_transport(handler)), max_retries=0)
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
    await engine.tick(project, conversation)
    await engine.tick(project, conversation)  # Reflect the child question on its root.
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
    engine.submit_message(project, conversation, "按故事先后顺序")
    await engine.tick(project, conversation)
    answered = store.get(children[0]["id"], project_id=project)
    assert answered["state"] == "queued"
    assert store.get(root["id"], project_id=project)["state"] == "running"
    assert not engine._task_data(answered).get("parent_owned")
    assert not [item for item in engine._task_data(answered)["pending_user_items"]
                if item["state"] == "open"]
    assert len(calls) == 4
    await client.close()


@pytest.mark.asyncio
async def test_manual_stage_request_cannot_preempt_harness_scheduled_step1(runtime):
    store, project, conversation = runtime
    calls = []
    before_stage_call = {}

    def handler(request):
        calls.append(json.loads(request.content))
        if len(calls) == 1:
            return httpx.Response(200, json=call("begin_adaptation", {"request": "完整改编",
                "source_is_current_message": False, "stage": None, "chapter_id": None}, 1))
        if len(calls) == 2:
            root = next(task for task in all_records(store, project, "task")
                        if engine._task_data(task).get("manager_controlled"))
            child = next(task for task in all_records(store, project, "task", parent_task_id=root["id"])
                         if engine._task_data(task).get("stage") == 1)
            before_stage_call.update(root_id=root["id"], root_state=root["state"],
                                     child_id=child["id"], child_state=child["state"])
            return httpx.Response(200, json=call("run_stage", {"stage": 4, "chapter_id": None}, 2))
        if len(calls) in (3, 4):
            return httpx.Response(200, json=step1_view_message(engine, project, "甲见乙。", json.loads(request.content)))
        pytest.fail("Step1 审阅前不应执行其他 Agent")

    client = AsyncOpenAI(api_key="local-mock", http_client=httpx.AsyncClient(
        transport=mock_transport(handler)), max_retries=0)
    engine = Engine(store, ModelService(store, client), ConfigService(store))
    engine.import_source(project, conversation, "甲见乙。", "测试原作")
    engine.submit_message(project, conversation, "开始改编")
    await engine.tick(project, conversation)
    root = next(task for task in all_records(store, project, "task")
                if engine._task_data(task).get("manager_controlled"))
    assert root["id"] == before_stage_call["root_id"]
    assert root["state"] == before_stage_call["root_state"]
    step1 = [task for task in all_records(store, project, "task", parent_task_id=root["id"])
             if engine._task_data(task).get("stage") == 1]
    assert len(step1) == 1 and step1[0]["id"] == before_stage_call["child_id"]
    assert step1[0]["state"] == before_stage_call["child_state"] == "queued"
    assert not engine._task_data(step1[0]).get("parent_owned")
    assert not any(task["scope"]["stage"] == 4 for task in all_records(store, project, "task"))
    await engine.tick(project, conversation)
    assert store.get(root["id"], project_id=project)["state"] == "waiting_user"
    assert len([card for card in ActionService(engine).list_cards(project, conversation)["cards"]
                if card["kind"] == "confirmation"]) == 1
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
        transport=mock_transport(handler)), max_retries=0)
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
        transport=mock_transport(handler)), max_retries=0)
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
async def test_harness_dispatches_stage_with_separate_audited_run(runtime, structured):
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
            return httpx.Response(200, json=message({"result_kind": "ready", "payload": {
                "reply": "已开始处理原作。", "source_message_kind": "request", "task_requests": []},
                "questions": [], "evidence_refs": [], "notes": []}))
        if len(calls) in (3, 4):
            if "当前独立产物：作品事件视图" in data.get("instructions", ""):
                event_schema = data["text"]["format"]["schema"]["properties"]["payload"]["anyOf"][0]["properties"]["global_events"]["items"]
                assert "analysis" in event_schema["required"]
            return httpx.Response(200, json=step1_view_message(engine, project, original, data))
        pytest.fail("Step1 等待两份固定视图确认后，协调 Agent 不应继续调度")

    client = AsyncOpenAI(api_key="local-mock", http_client=httpx.AsyncClient(
        transport=mock_transport(handler)), max_retries=0)
    engine = Engine(store, ModelService(store, client), ConfigService(store))
    if not structured:
        draft = engine.config_service.draft(project, {"output": {"structured": {"coordinator": False}}})
        engine.config_service.publish(project, draft["id"])
    engine.import_source(project, conversation, original, "测试原作")
    engine.submit_message(project, conversation, "开始改编")
    await engine.tick(project, conversation)
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
    global_run = next(run for run in child_runs if run["agent_key"] == "source_global_parser")
    global_snapshot = next(snapshot for snapshot in all_records(store, project, "context_snapshot")
                           if snapshot["run_id"] == global_run["id"])
    assert global_snapshot["output_schema"]["schema_id"] == "source_global_events"
    assert len({coordinator_runs[0]["session_id"], *(run["session_id"] for run in child_runs)}) == 3
    trace_rows=store._connection().execute(
        'SELECT run_id,kind,name FROM sdk_trace_spans WHERE project_id=%s',(project,)).fetchall()
    assert any(str(row['run_id'])==coordinator_runs[0]['id'] and row['kind']=='function'
               and row['name']=='begin_adaptation' for row in trace_rows)
    assert all(any(str(row['run_id']) == child['id'] and row['kind'] == 'agent'
                   for row in trace_rows) for child in child_runs)
    assert {item["tool_name"] for item in all_records(store, project, "tool_call")} >= {"begin_adaptation"}
    assert not [item for item in all_records(store, project, "tool_call")
                if item["tool_name"] == "run_stage"]
    root = next(task for task in all_records(store, project, "task")
                if engine._task_data(task).get("manager_controlled"))
    assert len(calls) == 4
    assert root["state"] == "waiting_user"
    cards = ActionService(engine).list_cards(project, conversation)["cards"]
    assert len(cards) == 1 and cards[0]["kind"] == "confirmation"
    assert {target["record_id"] for target in cards[0]["targets"]} == {
        version["record_id"] for version in engine._task_data(
            next(task for task in all_records(store, project, "task")
                 if engine._task_data(task).get("stage") == 1))["result_refs"].values()}
    receipt = ActionService(engine).submit(project, conversation, cards[0]["id"],
                                           "confirm", cards[0]["revision"], {})
    assert receipt["status"] == "confirmed"
    assert store.get(root["id"], project_id=project)["state"] == "succeeded"
    followup = next(task for task in all_records(store, project, "task")
                    if engine._task_data(task).get("triggered_by_step1_task_id") == root["id"])
    assert followup["state"] == "queued"
    assert engine._task_data(followup)["stages"] == [2]
    await client.close()


@pytest.mark.asyncio
async def test_scoped_step1_approval_runs_step2_from_approved_fixed_views(runtime):
    store, project, conversation = runtime
    original = "甲见乙。"
    calls = []

    def handler(request):
        data = json.loads(request.content)
        calls.append(data)
        number = len(calls)
        if number == 1:
            return httpx.Response(200, json=call("begin_adaptation", {
                "request": "只执行 Step1", "source_is_current_message": False,
                "stage": 1, "chapter_id": None}, number))
        if number == 2:
            return httpx.Response(200, json=message({"result_kind": "ready", "payload": {
                "reply": "已开始处理原作。", "source_message_kind": "request", "task_requests": []},
                "questions": [], "evidence_refs": [], "notes": []}))
        if number in (3, 4):
            return httpx.Response(200, json=step1_view_message(engine, project, original, data))
        if number == 5:
            return httpx.Response(200, json=message(step2_response()))
        pytest.fail("确认 Step1 后只应执行 Step2 Agent")

    client = AsyncOpenAI(api_key="local-mock", http_client=httpx.AsyncClient(
        transport=mock_transport(handler)), max_retries=0)
    engine = Engine(store, ModelService(store, client), ConfigService(store))
    engine.import_source(project, conversation, original, "测试原作")
    engine.submit_message(project, conversation, "开始 Step1")
    await engine.tick(project, conversation)
    await engine.tick(project, conversation)
    assert len(calls) == 4
    old_root = next(task for task in all_records(store, project, "task")
                    if engine._task_data(task).get("manager_controlled")
                    and engine._task_data(task).get("stages") == [1])
    assert old_root["state"] == "waiting_user"
    approved_refs = engine._task_data(old_root)["step1_review"]["artifact_refs"]
    cards = ActionService(engine).list_cards(project, conversation)["cards"]
    assert len(cards) == 1 and cards[0]["kind"] == "confirmation"
    assert {target["record_id"] for target in cards[0]["targets"]} == {
        fixed_ref["record_id"] for fixed_ref in approved_refs.values()}

    ActionService(engine).submit(project, conversation, cards[0]["id"],
                                 "confirm", cards[0]["revision"], {})
    assert store.get(old_root["id"], project_id=project)["state"] == "succeeded"
    step2_root = next(task for task in all_records(store, project, "task")
                      if engine._task_data(task).get("triggered_by_step1_task_id") == old_root["id"])
    assert step2_root["state"] == "queued"
    assert engine._task_data(step2_root)["stages"] == [2]
    assert engine._task_data(step2_root)["step1_input_refs"] == approved_refs
    assert len([task for task in all_records(store, project, "task")
                if task["parent_task_id"] == step2_root["id"]]) == 0

    await engine.tick(project, conversation)  # Harness selects and executes Step2.
    assert len(calls) == 5
    step2_child = next(task for task in all_records(store, project, "task")
                       if task["parent_task_id"] == step2_root["id"]
                       and engine._task_data(task).get("stage") == 2)
    assert step2_child["state"] == "waiting_user"
    run = next(run for run in all_records(store, project, "run")
               if run["agent_key"] == "source_knowledge_analyst")
    assert {json.dumps(fixed_ref, sort_keys=True) for fixed_ref in run["input_refs"]} == {
        json.dumps(fixed_ref, sort_keys=True) for fixed_ref in approved_refs.values()}
    assert len(engine._task_data(step2_child)["pending_user_items"]) == 1
    assert store.get(old_root["id"], project_id=project)["state"] == "succeeded"
    await client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("via_message", [False, True])
async def test_manager_confirms_single_step2_knowledge_asset(runtime, via_message):
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
        if number == 2:
            return httpx.Response(200, json=message({"result_kind": "ready", "payload": {
                "reply": "已开始处理原作。", "source_message_kind": "request", "task_requests": []},
                "questions": [], "evidence_refs": [], "notes": []}))
        if number in (3, 4):
            return httpx.Response(200, json=step1_view_message(engine, project, original, data))
        if number == 5:
            return httpx.Response(200, json=message(step2_response()))
        if number == 6 and via_message:
            step2 = next(task for task in all_records(store, project, "task")
                         if engine._task_data(task).get("stage") == 2)
            pending_id = engine._task_data(step2)["pending_user_items"][0]["id"]
            return httpx.Response(200, json=call("confirm_pending", {"pending_item_id": pending_id}, number))
        assert number == 7 and via_message
        final = {"result_kind": "ready", "payload": {"reply": "已收到确认。",
                 "source_message_kind": "request", "task_requests": []},
                 "questions": [], "evidence_refs": [], "notes": []}
        return httpx.Response(200, json=message(final))

    client = AsyncOpenAI(api_key="local-mock", http_client=httpx.AsyncClient(
        transport=mock_transport(handler)), max_retries=0)
    engine = Engine(store, ModelService(store, client), ConfigService(store))
    engine.import_source(project, conversation, original, "测试原作")
    engine.submit_message(project, conversation, "开始改编")
    await engine.tick(project, conversation)
    await engine.tick(project, conversation)
    root = next(task for task in all_records(store, project, "task")
                if engine._task_data(task).get("manager_controlled"))
    assert root["state"] == "waiting_user"
    approved_refs = engine._task_data(root)["step1_review"]["artifact_refs"]
    actions = ActionService(engine)
    step1_card = next(card for card in actions.list_cards(project, conversation)["cards"]
                      if card["kind"] == "confirmation")
    assert {target["record_id"] for target in step1_card["targets"]} == {
        fixed_ref["record_id"] for fixed_ref in approved_refs.values()}
    actions.submit(project, conversation, step1_card["id"], "confirm", step1_card["revision"], {})
    assert store.get(root["id"], project_id=project)["state"] == "queued"
    await engine.tick(project, conversation)  # Harness selects and executes Step2.
    step2_run = next(run for run in all_records(store, project, "run")
                     if run["agent_key"] == "source_knowledge_analyst")
    assert {json.dumps(fixed_ref, sort_keys=True) for fixed_ref in step2_run["input_refs"]} == {
        json.dumps(fixed_ref, sort_keys=True) for fixed_ref in approved_refs.values()}
    step2 = next(task for task in all_records(store, project, "task")
                 if task["parent_task_id"] == root["id"] and engine._task_data(task).get("stage") == 2)
    assert step2["state"] == "waiting_user"
    assert len(engine._task_data(step2)["pending_user_items"]) == 1
    assert len(engine._task_data(step2)["pending_user_items"][0]["targets"]) == 1
    assert actions.list_cards(project, conversation)["cards"][0]["kind"] == "confirmation"
    if via_message:
        engine.submit_message(project, conversation, "确认这份知识资产")
        await engine.tick(project, conversation)
    else:
        for item in engine._task_data(step2)["pending_user_items"]:
            shown = next(card for card in actions.list_cards(project, conversation)["cards"]
                         if card["id"] == "pending:" + item["id"])
            actions.submit(project, conversation, shown["id"], "confirm", shown["revision"], {})
    assert store.get(step2["id"], project_id=project)["state"] == "succeeded"
    assert store.get(root["id"], project_id=project)["state"] in ("queued", "running", "waiting_user")
    assert not [item for item in all_records(store, project, "queued_request")
                if item["state"] == "pending"
                and engine._projection(project, "queue_context", item["id"]).get("manager_resume")]
    assert len([run for run in all_records(store, project, "run")
                if run["agent_key"] == "conversation_coordinator"]) == (2 if via_message else 1)
    assert len(calls) == (7 if via_message else 5)
    await client.close()


@pytest.mark.asyncio
async def test_step2_waits_for_knowledge_asset_confirmation_before_advancing(runtime):
    store, project, conversation = runtime
    original = "甲见乙。"
    calls = []

    def handler(request):
        data = json.loads(request.content)
        calls.append(data)
        number = len(calls)
        if number == 1:
            return httpx.Response(200, json=call("begin_adaptation", {
                "request": "完整改编", "source_is_current_message": False,
                "stage": None, "chapter_id": None}, number))
        if number == 2:
            return httpx.Response(200, json=message({"result_kind": "ready", "payload": {
                "reply": "已开始处理原作。", "source_message_kind": "request", "task_requests": []},
                "questions": [], "evidence_refs": [], "notes": []}))
        if number in (3, 4):
            return httpx.Response(200, json=step1_view_message(engine, project, original, data))
        if number == 5:
            return httpx.Response(200, json=message(step2_response()))
        pytest.fail("Step2 确认卡由 Harness 直接创建，不需要协调 Agent 提问")

    client = AsyncOpenAI(api_key="local-mock", http_client=httpx.AsyncClient(
        transport=mock_transport(handler)), max_retries=0)
    engine = Engine(store, ModelService(store, client), ConfigService(store))
    engine.import_source(project, conversation, original, "测试原作")
    engine.submit_message(project, conversation, "开始改编")
    await engine.tick(project, conversation)
    await engine.tick(project, conversation)

    root = next(task for task in all_records(store, project, "task")
                if engine._task_data(task).get("manager_controlled"))
    assert root["state"] == "waiting_user"
    assert not [task for task in all_records(store, project, "task")
                if engine._task_data(task).get("stage") == 2]
    cards = ActionService(engine).list_cards(project, conversation)["cards"]
    assert len(cards) == 1 and cards[0]["kind"] == "confirmation"
    ActionService(engine).submit(project, conversation, cards[0]["id"],
                                 "confirm", cards[0]["revision"], {})
    await engine.tick(project, conversation)
    await engine.tick(project, conversation)
    step2 = next(task for task in all_records(store, project, "task")
                 if task["parent_task_id"] == root["id"] and engine._task_data(task).get("stage") == 2)
    assert step2["state"] == "waiting_user"
    targets = engine._task_data(step2)["pending_user_items"][0]["targets"]
    assert len(targets) == 1
    assert not [task for task in all_records(store, project, "task")
                if engine._task_data(task).get("stage") == 3]
    step2_card = next(card for card in ActionService(engine).list_cards(project, conversation)["cards"]
                      if card["kind"] == "confirmation")
    assert step2_card["targets"] == [target["subject"] for target in targets]
    receipt = ActionService(engine).submit(project, conversation, step2_card["id"],
                                           "confirm", step2_card["revision"], {})
    assert receipt["status"] == "confirmed"
    current = store.get(step2["id"], project_id=project)
    assert current["state"] == "succeeded"
    assert not [item for item in engine._task_data(current)["pending_user_items"]
                if item["state"] == "open"]
    assert not [task for task in all_records(store, project, "task")
                if task["parent_task_id"] == root["id"] and engine._task_data(task).get("stage") == 3]
    assert len(calls) == 5  # Step2 has its own structured model call.
    await client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("manager_controlled", [False, True])
async def test_begin_adaptation_returns_active_workflow_receipt_without_starting_another(runtime, manager_controlled):
    store, project, conversation = runtime
    engine = Engine(store, ModelService(store), ConfigService(store))
    with store.transaction():
        origin = engine._message(project, conversation, "先执行 Step1", role="user")
        existing = engine._new_task(project, conversation, origin, "generate")
        data = engine._task_data(existing)
        data.update(is_workflow=True, manager_controlled=manager_controlled,
                    stages=[1], chapter_ids=["ch01"])
        engine._save_task_data(existing, data)
        engine._transition(existing, "running")
        incoming = engine._message(project, conversation, "开始 Step2", role="user")

    from branch_agent.manager import build_manager_tools
    tool = next(tool for tool in build_manager_tools(engine, None, None, incoming,
                     [], None) if tool.name == "begin_adaptation")
    receipt = json.loads(await tool.on_invoke_tool(SimpleNamespace(tool_name="begin_adaptation"),
        '{"request":"开始 Step2","source_is_current_message":true,"stage":2,"chapter_id":null}'))
    assert receipt == {
        "status": "workflow_active", "reason": "manager_workflow_active",
        "workflow_id": existing["id"], "task_id": existing["id"], "state": "running",
        "scope": {"stages": [1], "chapter_ids": ["ch01"]},
        "manager_controlled": manager_controlled,
    }
    assert store.get(existing["id"], project_id=project)["state"] == "running"
    assert [task["id"] for task in all_records(store, project, "task")
            if engine._task_data(task).get("is_workflow")] == [existing["id"]]
    assert not all_records(store, project, "artifact")


@pytest.mark.asyncio
async def test_begin_adaptation_preserves_same_message_idempotency(runtime):
    store, project, conversation = runtime
    engine = Engine(store, ModelService(store), ConfigService(store))
    with store.transaction():
        origin = engine._message(project, conversation, "开始 Step1", role="user")
        existing = engine._new_task(project, conversation, origin, "generate")
        data = engine._task_data(existing)
        data.update(is_workflow=True, manager_controlled=True, stages=[1], chapter_ids=[])
        engine._save_task_data(existing, data)

    from branch_agent.manager import build_manager_tools
    tool = next(tool for tool in build_manager_tools(engine, None, None, origin,
                     [], None) if tool.name == "begin_adaptation")
    receipt = json.loads(await tool.on_invoke_tool(SimpleNamespace(tool_name="begin_adaptation"),
        '{"request":"开始 Step1","stage":1,"chapter_id":null}'))
    assert receipt == {"status": "already_started", "workflow_id": existing["id"], "stages": [1]}


@pytest.mark.asyncio
async def test_duplicate_begin_adaptation_keeps_one_harness_step1_child(runtime):
    store, project, conversation = runtime
    engine = Engine(store, ModelService(store), ConfigService(store))
    engine.import_source(project, conversation, "甲见乙。", "原作")
    with store.transaction():
        origin = engine._message(project, conversation, "开始改编", role="user")

    from branch_agent.manager import build_manager_tools
    tool = next(tool for tool in build_manager_tools(engine, None, None, origin,
                     [], None) if tool.name == "begin_adaptation")
    args = '{"request":"开始改编","source_is_current_message":false,"stage":1,"chapter_id":null}'
    first = json.loads(await tool.on_invoke_tool(SimpleNamespace(tool_name="begin_adaptation"), args))
    second = json.loads(await tool.on_invoke_tool(SimpleNamespace(tool_name="begin_adaptation"), args))
    assert first["status"] == "started"
    assert second == {"status": "already_started", "workflow_id": first["workflow_id"], "stages": [1]}
    root = store.get(first["workflow_id"], project_id=project)
    assert engine._task_data(root)["harness_scheduled"] is True
    children = all_records(store, project, "task", parent_task_id=root["id"])
    assert len(children) == 1
    assert children[0]["id"] == first["stage_task_id"]
    assert children[0]["scope"]["stage"] == 1


@pytest.mark.asyncio
async def test_step1_root_confirmation_blocks_manager_completion(runtime):
    store, project, conversation = runtime
    engine = Engine(store, ModelService(store), ConfigService(store))
    with store.transaction():
        origin = engine._message(project, conversation, "只执行 Step1", role="user")
        root = engine._new_task(project, conversation, origin, "generate")
        data = engine._task_data(root)
        data.update(is_workflow=True, manager_controlled=True, stages=[1], chapter_ids=[])
        engine._save_task_data(root, data)
        child = engine._new_task(project, conversation, origin, "generate", stage=1, parent=root)

    async def finish_child_and_open_confirmation(task, _token):
        with store.transaction():
            engine._transition(store.get(task["id"], project_id=project), "succeeded")
            current_root = store.get(root["id"], project_id=project)
            shown = engine._message(project, conversation, "请确认两份 Step1 视图", task=root["id"])
            pending = engine._wait_item(current_root, "confirmation", shown, "请确认两份 Step1 视图")
            root_data = engine._task_data(current_root)
            root_data["pending_user_items"] = [pending]
            engine._save_task_data(current_root, root_data)
            engine._transition(current_root, "waiting_user")

    engine._execute = finish_child_and_open_confirmation
    from branch_agent.manager import build_manager_tools
    tools = {tool.name: tool for tool in build_manager_tools(engine, None, None, origin, [], None)}
    receipt = json.loads(await tools["run_stage"].on_invoke_tool(
        SimpleNamespace(tool_name="run_stage"), '{"stage":1,"chapter_id":null}'))
    assert receipt["status"] == "needs_user_input"
    assert receipt["task_id"] == child["id"]
    assert receipt["pending_item_ids"] == [engine._task_data(store.get(root["id"], project_id=project))
                                         ["pending_user_items"][0]["id"]]
    with pytest.raises(WorkflowBlocked, match="confirmation_required"):
        await tools["finish_workflow"].on_invoke_tool(SimpleNamespace(tool_name="finish_workflow"), "{}")
    assert store.get(root["id"], project_id=project)["state"] == "waiting_user"


@pytest.mark.asyncio
async def test_finish_workflow_cannot_reuse_unapproved_step1_views_without_a_child(runtime):
    store, project, conversation = runtime
    engine = Engine(store, ModelService(store), ConfigService(store))
    _, source, global_view = source_event_case(store, project, "甲见乙。")
    character_reply = step1_view_message(engine, project, "甲见乙。", {
        "instructions": "当前独立产物：主要人物事件视图"})
    character_output = json.loads(character_reply["output"][0]["content"][0]["text"])
    with store.transaction():
        character_view = engine.workflow.save(project, "source_character_events",
            character_output, stage=1, inputs=[ref(source)], effective=True)
        origin = engine._message(project, conversation, "结束 Step1", role="user")
        root = engine._new_task(project, conversation, origin, "generate")
        data = engine._task_data(root)
        data.update(is_workflow=True, manager_controlled=True, stages=[1], chapter_ids=[],
                    step1_review={"source_ref": ref(source),
                        "artifact_refs": {"source_global_events": ref(global_view),
                                          "source_character_events": ref(character_view)},
                        "step1_task_id": str(uuid4())})
        engine._save_task_data(root, data)

    assert not all_records(store, project, "task", parent_task_id=root["id"])
    from branch_agent.manager import build_manager_tools
    finish = next(tool for tool in build_manager_tools(engine, None, None, origin,
                  [], None) if tool.name == "finish_workflow")
    with pytest.raises(WorkflowBlocked, match="confirmation_required"):
        await finish.on_invoke_tool(SimpleNamespace(tool_name="finish_workflow"), "{}")
    assert store.get(root["id"], project_id=project)["state"] == "queued"
    assert not engine._task_data(store.get(root["id"], project_id=project)).get("step1_approved_refs")
