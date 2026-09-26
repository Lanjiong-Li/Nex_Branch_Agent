"""Real PostgreSQL scheduler tests, using explicitly synthetic model responses."""
import asyncio
from copy import deepcopy
import os

import pytest
import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo
from uuid import uuid4

from branch_agent.configuration import ConfigService
from branch_agent.engine import Engine, _after
from branch_agent.records import new_record
from branch_agent.storage import Store
from branch_agent.workflow import WorkflowBlocked, all_records, ref, update


class FakeModel:
    def __init__(self):
        self.calls = []
        self.responses = []

    async def run(self, stage, task, run, session, config, materials, message, control=None,
                  step1_view=None, step1_window=None, instructions_override=None):
        self.calls.append(stage)
        from branch_agent.context import prepare_runtime_materials, build_materials
        prepared = prepare_runtime_materials(stage, task, run, session, config, materials, self.store,
                                             step1_window=step1_window)
        build_materials(stage, prepared, config, self.store, task["project_id"])
        if control:
            await control()
        response = self.responses.pop(0) if self.responses else (source_response if stage == "step1" else None)
        assert response is not None, f"No synthetic response for {stage}"
        result = response(task, materials) if callable(response) else deepcopy(response)
        if stage == "step1" and step1_view and isinstance(result, dict) and result.get("payload"):
            result = deepcopy(result)
            result["payload"].pop("character_views" if step1_view == "global" else "global_events", None)
        return result


@pytest.fixture
def runtime(tmp_path):
    base_dsn = os.getenv("BRANCH_AGENT_TEST_DSN", "postgresql:///branch_agent_local")
    namespace = "runtime_test_" + uuid4().hex
    with psycopg.connect(base_dsn, autocommit=True) as conn:
        conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(namespace)))
    store = Store(make_conninfo(base_dsn, options=f"-c search_path={namespace}"), tmp_path / "blobs")
    store.migrate()
    project = store.put(new_record("project", None, owner_account_id="runtime-test", title="Runtime test"))
    conversation = store.put(new_record("conversation", project["id"], title="Test"))
    model = FakeModel()
    model.store = store
    # Retain coverage of the production billing policy while the app defaults
    # to the test rollout. Disabled-policy regressions opt out explicitly.
    engine = Engine(store, model, ConfigService(store), cost_gates_enabled=True)
    yield engine, model, project["id"], conversation["id"]
    store.close()
    with psycopg.connect(base_dsn, autocommit=True) as conn:
        conn.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(namespace)))


def coordinator(task, materials):
    return {"result_kind": "ready", "payload": {"reply": "已接受整剧改编请求。",
        "source_message_kind": "request", "task_requests": [{
        "intent": "generate", "stage": None, "chapter_id": None, "target_ref": None,
        "request": "完成整剧改编", "source_message_ids": [task["requested_by_message_id"]], "requested_confirmation_paths": []}]},
        "questions": [], "evidence_refs": [], "notes": []}


def source_response(task, materials):
    source = next(m for m in materials if m["schema_id"] == "source_text")
    anchor = {"source_ref": source["ref"], "start_utf16": None, "end_utf16": None,
              "exact_quote": source["content"], "prefix": None, "suffix": None}
    return {"result_kind": "ready", "payload": {"source_ref": source["ref"],
        "global_events": [{"event_id": "event-1", "title": "见面", "summary": "甲见乙。",
            "narrative_order": 0, "story_time": None, "character_ids": [], "source_anchors": [anchor]}],
        "character_views": [], "covered_source_anchors": [anchor], "remaining_source_anchors": []},
        "questions": [], "evidence_refs": [source["ref"]], "notes": []}


def test_source_views_character_events_have_independent_direct_anchors():
    from branch_agent.schemas import SchemaCatalog
    from branch_agent.workflow import locate_source_anchors
    original = "甲见乙。"
    source_ref = {"record_id": str(uuid4()), "version": "1", "item_id": None, "json_pointer": None}
    result = source_response(None, [{"schema_id": "source_text", "ref": source_ref, "content": original}])
    result["payload"]["character_views"] = [{
        "character_id": "character-甲", "name": "甲", "aliases": [], "description": "人物",
        "events": [{"character_event_id": "character-event-1", "title": "遇见乙", "summary": "甲遇见乙",
                    "narrative_order": 0, "story_time": None, "involvement": "主动相遇",
                    "source_anchors": [{"source_ref": source_ref, "start_utf16": 0, "end_utf16": 3,
                                        "exact_quote": "甲见乙", "prefix": None, "suffix": None}]}]}]
    catalog = SchemaCatalog()
    catalog.validate("source_views", result)
    located = locate_source_anchors(result, original, source_ref)
    character_event = located["payload"]["character_views"][0]["events"][0]
    assert character_event["character_event_id"] != located["payload"]["global_events"][0]["event_id"]
    assert character_event["source_anchors"][0]["source_ref"] == source_ref
    old = deepcopy(result)
    old["payload"]["character_views"][0]["events"] = [{"event_id": "event-1", "involvement": "主动相遇"}]
    with pytest.raises(ValueError):
        catalog.validate("source_views", old)


def step2_response(global_analysis="甲与乙相遇。", character_analysis="甲与乙的人物关系保持不变。"):
    return ("[[GLOBAL_EVENT_ANALYSIS]]\n" + global_analysis.strip()
            + "\n[[CHARACTER_EVENT_ANALYSIS]]\n" + character_analysis.strip())


def test_persistent_queue_and_idempotent_message(runtime):
    engine, model, pid, cid = runtime
    first = engine.submit_message(pid, cid, "hello", idempotency_key="one")
    assert engine.submit_message(pid, cid, "hello", idempotency_key="one")["id"] == first["id"]
    with pytest.raises(WorkflowBlocked, match="idempotency_conflict"):
        engine.submit_message(pid, cid, "different", idempotency_key="one")
    restarted = Engine(engine.store, model, engine.config_service)
    assert restarted.status(pid)["queue"][0]["source_message_id"] == first["id"]


def test_steer_closed_target_is_retained(runtime):
    engine, model, pid, cid = runtime
    from uuid import uuid4
    # Unknown IDs cannot pass Store's project reference integrity, so use no target error.
    with pytest.raises(WorkflowBlocked, match="target_run_required"):
        engine.submit_message(pid, cid, "change", mode="steer")
    assert not engine.status(pid)["queue"]


def test_full_workflow_accepts_plain_step2_and_stops_at_confirmation(runtime):
    engine, model, pid, cid = runtime
    engine.import_source(pid, cid, "甲见乙。", "故事")
    analysis = step2_response("故事前提\n甲与乙相遇。\n\n原作保留建议\n保留相遇因果。",
                              "人物关系\n保留甲与乙的相遇关系。")
    model.responses = [coordinator, source_response, source_response, analysis]
    engine.submit_message(pid, cid, "请完成整剧改编")
    async def drive():
        for _ in range(6):
            await engine.tick(pid, cid)
    asyncio.run(drive())
    tasks = engine.status(pid)["tasks"]
    assert model.calls == ["coordinator", "step1", "step1", "step2"]
    assert any(t["scope"]["stage"] == 1 and t["state"] == "succeeded" for t in tasks)
    assert any(t["scope"]["stage"] == 2 and t["state"] == "waiting_user" for t in tasks)
    assert not any(r["state"] == "failed" and r.get("error", {}).get("code") == "output_validation_failed" for r in engine.status(pid)["runs"])
    shown = [h["content"]["text"] for h in all_records(engine.store, pid, "history_record")
             if h["visibility"] == "conversation" and h["role"] == "assistant"]
    assert any("Step 1 · 原作切分" in text and "甲见乙。" in text for text in shown)
    assert any("Step 2A · 全局事件分析" in text for text in shown)
    assert any("Step 2B · 主要人物事件分析" in text for text in shown)
    assert any("甲与乙相遇" in text for text in shown)
    assert all('"global_events"' not in text for text in shown)


def test_pasted_complete_source_is_saved_verbatim_and_runs_steps_1_and_2(runtime):
    engine, model, pid, cid = runtime
    source_text = "第一章\n韩立出生在一个普通农家。\n第二章\n他告别家人，踏上未知旅程。"

    def pasted_source_request(task, materials):
        return {"result_kind": "ready", "payload": {
            "reply": "已识别为完整线性原作，开始切片和分析。",
            "source_message_kind": "complete_source_text",
            "task_requests": [{
                "intent": "generate", "stage": None, "chapter_id": None, "target_ref": None,
                "request": "完成整剧改编", "source_message_ids": [task["requested_by_message_id"]],
                "requested_confirmation_paths": [],
            }],
        }, "questions": [], "evidence_refs": [], "notes": []}

    analysis = step2_response("故事前提\n韩立离家启程。", "人物关系\n保留韩立与家人的关系。")

    model.responses = [pasted_source_request, source_response, source_response, analysis]
    message = engine.submit_message(pid, cid, source_text)

    async def drive():
        for _ in range(7):
            await engine.tick(pid, cid)
    asyncio.run(drive())

    assert all_records(engine.store, pid, "artifact", artifact_kind="source_text"), engine.status(pid)
    source = engine.workflow.resolve(pid, "source_text")
    assert source["content"] == {"storage": "inline_text", "text": source_text}
    assert source["source_refs"] == [ref(message)]
    assert model.calls == ["coordinator", "step1", "step1", "step2"]
    tasks = engine.status(pid)["tasks"]
    assert any(t["scope"]["stage"] == 1 and t["state"] == "succeeded" for t in tasks)
    assert any(t["scope"]["stage"] == 2 and t["state"] == "waiting_user" for t in tasks)


def test_coordinator_is_told_that_an_effective_source_already_exists(runtime):
    engine, model, pid, cid = runtime
    imported = engine.import_source(pid, cid, "甲见乙。", "故事")["source"]
    captured = []

    async def capture(stage, task, run, session, config, materials, message, control=None):
        captured.append(message)
        return coordinator(task, materials)

    model.run = capture
    engine.submit_message(pid, cid, "开始改编")
    asyncio.run(engine.tick(pid, cid))

    assert captured
    assert "当前项目已有可用原作" in captured[0]
    assert imported["artifact_id"] in captured[0]
    assert "不得再次要求用户提供原作" in captured[0]


def test_stop_waiting_task_does_not_restart_on_new_engine(runtime):
    engine, model, pid, cid = runtime
    message = engine.submit_message(pid, cid, "work")
    with engine.store.transaction():
        task = engine._new_task(pid, cid, message, "generate", 2)
        engine._transition(task, "waiting_user")
    engine.control_task(pid, task["id"], "stop")
    restarted = Engine(engine.store, model, engine.config_service)
    assert restarted.store.get(task["id"], pid)["state"] == "stopped"
    assert restarted._control(pid, cid)["holds"]
    restarted.resume_queue(pid, cid)
    assert restarted.store.get(task["id"], pid)["state"] == "stopped"
    assert not restarted._control(pid, cid)["holds"]


def test_leased_conversation_cannot_be_claimed_by_second_worker(runtime):
    engine, model, pid, cid = runtime
    with engine.store.transaction():
        gate = engine._control(pid, cid)
        gate.update(lease_owner="another-worker", lease_expires_at=_after(engine.store.now(), 60), fencing_token=4)
        engine._save_projection(pid, "conversation_control", cid, gate)
    asyncio.run(engine.tick(pid, cid))
    assert engine._control(pid, cid)["fencing_token"] == 4
    assert not model.calls


def test_confirmation_uses_submission_snapshot_and_original_user(runtime):
    engine, model, pid, cid = runtime
    engine.import_source(pid, cid, "甲见乙。", "故事")
    message = engine.submit_message(pid, cid, "生成分析")
    with engine.store.transaction():
        task = engine._new_task(pid, cid, message, "generate", 2)
        # Schema-shaped synthetic stage result; no production fixture shortcut.
        source = engine.workflow.resolve(pid, "source_text")
        output = {"result_kind": "ready", "payload": {"source_views_ref": ref(source), "premise": "相遇",
            "world_rules": [], "themes": [], "conflicts": [], "characters": [], "key_event_refs": [], "preservation_items": []},
            "questions": [], "evidence_refs": [], "notes": []}
        version = engine.workflow.save(pid, "source_analysis", output, stage=2)
        shown = engine._message(pid, cid, "请确认这个分析版本。")
        presentations = [{"subject": ref(version), "selections": [{"item_id": None, "json_pointer": ""}], "message_id": shown["id"]}]
        engine._save_projection(pid, "presentations", cid, {"targets": presentations})
    user = engine.submit_message(pid, cid, "就用这个版本")
    request = {"source_message_ids": [user["id"]], "target_ref": ref(version), "requested_confirmation_paths": ["/payload"]}
    with engine.store.transaction():
        confirmation, _, complete = engine.workflow.confirm(pid, request, user, presentations)
    assert complete
    assert confirmation["source_message_ids"] == [user["id"]]
    assert engine.workflow.resolve(pid, "source_analysis")["id"] == version["id"]
    forged = dict(request, source_message_ids=[shown["id"]])
    with pytest.raises(WorkflowBlocked, match="invalid_confirmation_source"):
        with engine.store.transaction():
            engine.workflow.confirm(pid, forged, user, presentations)
    with pytest.raises(WorkflowBlocked, match="confirmation_not_presented"):
        with engine.store.transaction():
            engine.workflow.confirm(pid, request, user, [])


def test_orphan_task_pauses_once_without_model(runtime):
    engine, model, pid, cid = runtime
    message = engine.submit_message(pid, cid, "placeholder")
    with engine.store.transaction():
        orphan = engine.store.put(new_record("task", pid, conversation_id=cid,
            requested_by_message_id=message["id"], intent="generate"))
        q = engine.status(pid)["queue"][0]
        update(engine.store, q, state="cancelled")
    asyncio.run(engine.tick(pid, cid))
    asyncio.run(engine.tick(pid, cid))
    assert engine.store.get(orphan["id"], pid)["pause_reason"] == "checkpoint_invalid"
    assert not model.calls


def test_expired_unknown_request_is_not_reissued(runtime):
    engine, model, pid, cid = runtime
    message = engine.submit_message(pid, cid, "work")
    with engine.store.transaction():
        q = engine.status(pid)["queue"][0]
        update(engine.store, q, state="cancelled")
        task = engine._new_task(pid, cid, message, "generate", 1)
        task, run, session, values = engine._start_run(task, 1, [])
        data = engine._task_data(task)
        data["model_dispatched"] = True
        engine._save_task_data(task, data)
        update(engine.store, engine.store.get(run["id"], pid), lease_expires_at=_after(engine.store.now(), -60))
        update(engine.store, session, lease_expires_at=_after(engine.store.now(), -60))
    restarted = Engine(engine.store, model, engine.config_service)
    asyncio.run(restarted.tick(pid, cid))
    assert engine.store.get(task["id"], pid)["pause_reason"] == "operation_uncertain"
    assert engine.store.get(run["id"], pid)["state"] == "interrupted"
    assert not model.calls


def _synthetic(schema, root, evidence):
    """Produce schema-shaped test data, never used by production code."""
    if "$ref" in schema:
        if schema["$ref"].endswith("/EvidenceRef"):
            return deepcopy(evidence)
        return _synthetic(root["$defs"][schema["$ref"].split("/")[-1]], root, evidence)
    if "anyOf" in schema:
        choices = schema["anyOf"]
        if any(c.get("type") == "null" for c in choices):
            return None
        return _synthetic(choices[0], root, evidence)
    if "const" in schema:
        return schema["const"]
    if "enum" in schema:
        return schema["enum"][0]
    kind = schema.get("type")
    if kind == "object":
        return {k: _synthetic(v, root, evidence) for k, v in schema["properties"].items()}
    if kind == "array":
        return []
    if kind in ("number", "integer"):
        return schema.get("minimum", 0)
    if kind == "boolean":
        return False
    return "synthetic"


def test_step4_program_binds_fixed_upstream_references(runtime):
    from branch_agent.graph import schema

    engine, _, pid, cid = runtime
    source = engine.import_source(pid, cid, "甲见乙。", "引用绑定测试")["source"]
    with engine.store.transaction():
        global_analysis = engine.workflow.save(pid, "source_global_analysis", "甲与乙相遇。", stage=2,
                                               effective=True, origin="program", inputs=[ref(source)])
        character_analysis = engine.workflow.save(pid, "source_character_analysis", "甲与乙的人物关系。", stage=2,
                                                  effective=True, origin="program", inputs=[ref(source)])
        strategy_contract = schema("adaptation_strategy")
        strategy_payload = _synthetic(strategy_contract["properties"]["payload"]["anyOf"][0],
                                      strategy_contract, ref(global_analysis))
        strategy_payload["source_global_analysis_ref"] = ref(global_analysis)
        strategy_payload["source_character_analysis_ref"] = ref(character_analysis)
        strategy = engine.workflow.save(pid, "adaptation_strategy", {
            "result_kind": "ready", "payload": strategy_payload,
            "questions": [], "evidence_refs": [ref(global_analysis), ref(character_analysis)], "notes": [],
        }, stage=3, effective=True, origin="program", inputs=[ref(global_analysis), ref(character_analysis)])

        message = engine._message(pid, cid, "生成改编方案", role="user")
        task = engine._new_task(pid, cid, message, "generate", stage=4)
        materials = engine.workflow.materials(pid, 4)
        task, run, _, _ = engine._start_run(task, 4, materials)

        plan_contract = schema("adaptation_plan")
        plan_payload = _synthetic(plan_contract["properties"]["payload"]["anyOf"][0],
                                  plan_contract, ref(global_analysis))
        invented = {"record_id": str(uuid4()), "version": "1",
                    "item_id": None, "json_pointer": None}
        plan_payload["source_global_analysis_ref"] = deepcopy(invented)
        plan_payload["source_character_analysis_ref"] = deepcopy(invented)
        plan_payload["strategy_ref"] = deepcopy(invented)
        plan_payload["stage_artifact_refs"] = {
            "game_events": deepcopy(invented),
            "event_functions": deepcopy(invented),
            "ending_routes": deepcopy(invented),
            "player_profiles": deepcopy(invented),
        }
        engine._apply_stage(task, run, {
            "result_kind": "ready", "payload": plan_payload,
            "questions": [], "evidence_refs": [deepcopy(invented)], "notes": [],
        }, materials)

    result_ref = engine._task_data(engine.store.get(task["id"], pid))["result_ref"]
    saved = engine.workflow.fixed_version(pid, result_ref)["content"]["value"]
    assert saved["payload"]["source_global_analysis_ref"] == ref(global_analysis)
    assert saved["payload"]["source_character_analysis_ref"] == ref(character_analysis)
    assert saved["payload"]["strategy_ref"] == ref(strategy)
    assert saved["payload"]["stage_artifact_refs"] == {
        "game_events": None,
        "event_functions": None,
        "ending_routes": None,
        "player_profiles": None,
    }
    assert saved["evidence_refs"] == [ref(global_analysis), ref(character_analysis), ref(strategy)]


def test_step9_program_rebinds_model_database_coordinates(runtime):
    engine, _, _, _ = runtime

    def fixed(kind, content):
        reference = {"record_id": str(uuid4()), "version": "1",
                     "item_id": None, "json_pointer": None}
        return reference, {"kind": kind, "schema_id": kind, "ref": reference, "content": content}

    plan_ref, plan = fixed("adaptation_plan", {"payload": {"entity_specs": []}})
    events_ref, events = fixed("game_event_view", {"payload": {"events": []}})
    routes_ref, routes = fixed("ending_routes", {
        "payload": {"state_requirements": [{"state_id": "route-state", "description": "路线状态"}]},
    })
    profiles_ref, profiles = fixed("player_profiles", {"payload": {"profiles": []}})
    materials = [plan, events, routes, profiles]
    run = {"input_refs": [plan_ref, events_ref, routes_ref, profiles_ref]}
    invented = {"record_id": str(uuid4()), "version": "9",
                "item_id": None, "json_pointer": "/payload/state_requirements/0"}
    result = {"result_kind": "ready", "payload": {
        "plan_ref": deepcopy(invented),
        "state_requirements": [{"used_by_refs": [deepcopy(invented)]}],
    }, "questions": [], "evidence_refs": [deepcopy(invented)], "notes": []}

    bound = engine._bind_program_provenance(9, run, result, materials)

    assert bound["payload"]["plan_ref"] == plan_ref
    assert bound["payload"]["state_requirements"][0]["used_by_refs"] == [{
        **routes_ref, "json_pointer": "/payload/state_requirements/0",
    }]
    assert bound["evidence_refs"] == [plan_ref, events_ref, routes_ref, profiles_ref]
    assert invented["record_id"] not in repr(bound)


def test_two_chapter_workflow_confirm_audit_and_deliver_fixed_candidate(runtime):
    import json
    from pathlib import Path
    from branch_agent.graph import schema, validate_output
    engine, _, pid, cid = runtime
    engine.import_source(pid, cid, "甲见乙。丙离开。", "合成测试原作")

    class WorkflowModel(FakeModel):
        async def run(self, stage, task, run, session, config, materials, message, control=None,
                      step1_view=None, step1_window=None, instructions_override=None):
            self.calls.append(stage)
            from branch_agent.context import prepare_runtime_materials, build_materials
            prepared = prepare_runtime_materials(stage, task, run, session, config, materials, engine.store)
            packed, _, _ = build_materials(stage, prepared, config, engine.store, task["project_id"])
            if stage == "step5":
                schema_ids = {material.get("schema_id") for material in materials}
                assert "adaptation_plan" not in schema_ids
                business_schema_ids = {schema_id for schema_id in schema_ids
                                       if schema_id and not schema_id.startswith("runtime.")}
                assert business_schema_ids == {
                    "source_global_events", "source_character_events",
                    "source_global_analysis", "source_character_analysis"
                }
                assert not any(block.get("builtin") == "runtime.source_block" for block in packed)
            if stage == "step6":
                assert {material.get("schema_id") for material in materials
                        if material.get("schema_id")} == {"game_event_view"}
                assert {block.get("source_kind") for block in packed
                        if block.get("source_kind") and not block.get("source_kind").startswith("runtime.")} == {"game_event_view"}
                assert not any(material["ref"] in run["input_refs"] for material in materials
                               if material.get("schema_id") == "game_event_view")
            if stage == "step7":
                assert {material.get("schema_id") for material in materials} == {"game_event_view"}
                assert run["input_refs"] == [materials[0]["ref"]]
                assert session["session_key"] == "ending_routes"
                assert {block.get("source_kind") for block in packed} == {"game_event_view"}
                assert not any(block.get("builtin") for block in packed)
                assert all(event["narrative_function"] for event in materials[0]["content"]["payload"]["events"])
            if stage == "step8":
                assert [material.get("schema_id") for material in materials] == ["game_event_view", "ending_routes"]
                assert run["input_refs"] == [material["ref"] for material in materials]
                assert session["session_key"] == "player_profiles"
                assert {block.get("source_kind") for block in packed} == {"game_event_view", "ending_routes"}
                assert not any(block.get("builtin") for block in packed)
                plan = engine.workflow.resolve(pid, "adaptation_plan")
                plan_refs = plan["content"]["value"]["payload"]["stage_artifact_refs"]
                assert plan_refs["game_events"] == materials[0]["ref"]
                assert plan_refs["ending_routes"] == materials[1]["ref"]
            if stage == "step9":
                assert [material.get("schema_id") for material in materials] == [
                    "adaptation_plan", "source_text"
                ]
                assert run["input_refs"] == [material["ref"] for material in materials]
                plan_refs = materials[0]["content"]["payload"]["stage_artifact_refs"]
                assert all(plan_refs[key] for key in ("game_events", "ending_routes", "player_profiles"))
                assert {block.get("source_kind") for block in packed} == {"adaptation_plan", "source_text"}
                assert [block.get("builtin") for block in packed if block.get("builtin")] == ["runtime.source_block"]
            if stage == "step10":
                assert {m["schema_id"] for m in materials} == {"chapter_design", "source_text"}
                assert {block["source_kind"] for block in packed} == {"chapter_design", "source_text"}
                assert {block.get("builtin") for block in packed if block.get("builtin")} == {"runtime.source_block"}
                assert session["session_key"].endswith(":writer")
                original = next(m for m in materials if m.get("schema_id") == "source_text")
                spans = [block for block in packed if block.get("builtin") == "runtime.source_block"]
                assert len(spans) == 1 and spans[0]["data"]["text"] == "甲见乙。"
                assert spans[0]["data"]["text"] != original["content"]
            if control:
                await control()
            if stage == "coordinator":
                original = engine.store.get(task["requested_by_message_id"], pid)["content"]["text"]
                base = {"intent": "generate", "stage": None, "chapter_id": None, "target_ref": None,
                        "request": original, "source_message_ids": [task["requested_by_message_id"]], "requested_confirmation_paths": []}
                if original == "propose chapters" or engine._task_data(task).get("chapter_planning"):
                    requests = [dict(base, stage=9, chapter_id=c) for c in ("chapter-one", "chapter-two")]
                elif original == "revise chapter":
                    requests = [dict(base, intent="modify", stage=10, chapter_id="chapter-one", target_ref=ref(engine.workflow.resolve(pid, "chapter_graph", "chapter-one")))]
                elif original == "audit again":
                    requests = [dict(base, stage=11)]
                elif original == "confirm":
                    runtime_material = next(m for m in materials if m.get("builtin") == "runtime.status")
                    target = runtime_material["content"]["presented_at_submission"][-1]["subject"]
                    requests = [dict(base, intent="confirm", target_ref=target, requested_confirmation_paths=[""])]
                else:
                    requests = [base]
                return {"result_kind": "ready", "payload": {"reply": "合成协调回复",
                    "source_message_kind": "request", "task_requests": requests},
                    "questions": [], "evidence_refs": [], "notes": []}
            if stage == "step1":
                response = source_response(task, materials)
                response["payload"].pop("character_views" if step1_view == "global" else "global_events", None)
                return response
            if stage == "step10":
                value = json.loads((Path(__file__).resolve().parents[1] / "docs/output-schemas/v2/examples/chapter_graph.example.json").read_text())
                chapter = task["scope"]["chapter_ids"][0]
                def ids(node):
                    found = set()
                    if isinstance(node, dict):
                        if "id" in node:
                            found.add(node["id"])
                        for v in node.values(): found.update(ids(v))
                    elif isinstance(node, list):
                        for v in node: found.update(ids(v))
                    return found
                mapping = {i: chapter + ":" + i for i in ids(value["payload"]["chapter"])}
                mapping[value["payload"]["chapter"]["id"]] = chapter
                def replace(node):
                    if isinstance(node, dict): return {k: replace(v) for k, v in node.items()}
                    if isinstance(node, list): return [replace(v) for v in node]
                    return mapping.get(node, node) if isinstance(node, str) else node
                value["payload"]["chapter"] = replace(value["payload"]["chapter"])
                if engine.store.get(task["requested_by_message_id"], pid)["content"]["text"] == "revise chapter":
                    value["payload"]["chapter"]["nodes"][0]["body"] += "修订后的正文。"
                for node in value["payload"]["chapter"]["nodes"]:
                    if "sceneId" in node: node["sceneId"] = ""
                value["payload"]["shared_scenes"] = []
                value["evidence_refs"] = []
                return value
            if stage == "step2":
                return step2_response("故事前提\n甲与乙相遇。\n保留事件因果。", "人物关系\n保留人物关系。")
            kind = {3: "adaptation_strategy", 4: "adaptation_plan", 5: "game_event_view",
                    6: "game_event_narrative_patch", 7: "ending_routes", 8: "player_profiles", 9: "chapter_design"}[int(stage[4:])]
            contract = schema(kind)
            payload_schema = contract["properties"]["payload"]["anyOf"][0]
            payload = _synthetic(payload_schema, contract, materials[0]["ref"])
            if kind == "game_event_view":
                assert "plan_ref" not in payload
                event_schema = payload_schema["properties"]["events"]["items"]
                payload["events"] = [_synthetic(event_schema, contract, materials[0]["ref"])]
                payload["events"][0]["game_event_id"] = "game-event-one"
                views = next(m for m in materials if m.get("schema_id") == "source_global_events")
                view_anchor = views["content"]["payload"]["global_events"][0]["source_anchors"][0]
                payload["events"][0]["source_anchors"] = [{**view_anchor,
                    "end_utf16": len("甲见乙。"), "exact_quote": None}]
            if kind == "game_event_narrative_patch":
                payload["updates"] = [{"game_event_id": "game-event-one", "narrative_function": "推动玩家理解角色动机"}]
            if kind == "ending_routes":
                ending_schema = payload_schema["properties"]["endings"]["items"]
                route_schema = payload_schema["properties"]["routes"]["items"]
                ending = _synthetic(ending_schema, contract, materials[0]["ref"])
                ending["ending_id"] = "ending-one"
                route = _synthetic(route_schema, contract, materials[0]["ref"])
                route.update(route_id="route-one", ending_ids=["ending-one"], game_event_ids=["game-event-one"])
                payload.update(endings=[ending], routes=[route])
            if kind == "chapter_design":
                payload["chapter_id"] = task["scope"]["chapter_ids"][0]
                plan = next(m for m in materials if m.get("schema_id") == "adaptation_plan")
                events = engine.workflow.planned_game_events(pid, plan=plan["record"])
                original = next(m for m in materials if m.get("schema_id") == "source_text")
                payload["game_event_refs"] = [{**ref(events), "item_id": "game-event-one", "json_pointer": "/payload/events/0"}]
                # An overbroad model hint must be replaced by the Harness's
                # exact union of the selected game events' original anchors.
                payload["chapter_source_anchors"] = [{"source_ref": original["ref"],
                    "start_utf16": 0, "end_utf16": len(original["content"]),
                    "exact_quote": None, "prefix": None, "suffix": None}]
                payload["linear_body"]["segments"] = [{"segment_id": "segment", "text": "甲走向乙。", "character_refs": [], "location_refs": [], "game_event_refs": []}]
            return {"result_kind": "ready", "payload": payload, "questions": [], "evidence_refs": [], "notes": []}

    model = WorkflowModel()
    engine.model_service = model
    engine.submit_message(pid, cid, "start")
    async def drive():
        from branch_agent.actions import ActionService
        actions = ActionService(engine)
        confirmed = set()
        chapter_requested = False
        for _ in range(100):
            await engine.tick(pid, cid)
            state = engine.status(pid)
            if state["latest_delivery"]:
                return state
            paused = [t for t in state["tasks"] if t["state"] == "paused"]
            assert not paused, [(t["scope"], t["pause_reason"],
                                 [r['error'] for r in all_records(engine.store,pid,'run',task_id=t['id'])]) for t in paused]
            for waiting in state["pending_user_items"]:
                if waiting["id"] in confirmed:
                    continue
                if waiting["kind"] == "confirmation":
                    current = next(card for card in actions.list_cards(pid, cid)["cards"]
                                   if card["id"] == "pending:" + waiting["id"])
                    actions.submit(pid, cid, current["id"], "confirm", current["revision"], {})
                    confirmed.add(waiting["id"])
                    break
                elif waiting["description"] == "确定完整章节列表及顺序" and not chapter_requested:
                    engine.submit_message(pid, cid, "propose chapters")
                    chapter_requested = True
        pytest.fail("workflow did not deliver: " + repr([(t["scope"]["stage"], t["state"], t["pause_reason"], engine._task_data(t).get("pending_user_items")) for t in state["tasks"]]) + repr([h["content"] for h in all_records(engine.store, pid, "history_record")[-5:]]))
    final = asyncio.run(drive())
    delivery = final["latest_delivery"]
    version = engine.workflow.fixed_version(pid, delivery["artifact_ref"])
    graph = version["content"]["value"]
    validate_output("nexo_graph", graph)
    assert [c["id"] for c in graph["chapters"]] == ["chapter-one", "chapter-two"]
    assert [c for c in model.calls if c != "coordinator"] == ["step1", "step1"] + [f"step{i}" for i in range(2, 9)] + ["step9", "step10", "step9", "step10"]
    game_events = engine.workflow.resolve(pid, "game_event_view")
    assert game_events["version"] == 2
    assert game_events["content"]["value"]["payload"]["events"][0]["narrative_function"] == "推动玩家理解角色动机"
    first_events = engine.workflow.versions(pid, game_events["artifact_id"])[0]
    original_payload = deepcopy(first_events["content"]["value"]["payload"])
    enriched_payload = deepcopy(game_events["content"]["value"]["payload"])
    for event in enriched_payload["events"]:
        event["narrative_function"] = None
    assert enriched_payload == original_payload
    assert ref(first_events) in game_events["source_refs"]
    current_plan = engine.workflow.resolve(pid, "adaptation_plan")
    assert current_plan["content"]["value"]["payload"]["stage_artifact_refs"]["game_events"] == ref(game_events)
    ending_routes = engine.workflow.resolve(pid, "ending_routes")
    assert ending_routes["source_refs"] == [ref(game_events)]
    assert current_plan["content"]["value"]["payload"]["stage_artifact_refs"]["ending_routes"] == ref(ending_routes)
    assert "plan_ref" not in ending_routes["content"]["value"]["payload"]
    assert "game_event_view_ref" not in ending_routes["content"]["value"]["payload"]
    assert ending_routes["content"]["value"]["payload"]["routes"][0]["game_event_ids"] == ["game-event-one"]
    player_profiles = engine.workflow.resolve(pid, "player_profiles")
    assert player_profiles["source_refs"] == [ref(game_events), ref(ending_routes)]
    assert current_plan["content"]["value"]["payload"]["stage_artifact_refs"]["player_profiles"] == ref(player_profiles)
    assert player_profiles["content"]["value"]["payload"]["game_event_view_ref"] == ref(game_events)
    assert player_profiles["content"]["value"]["payload"]["ending_routes_ref"] == ref(ending_routes)
    events = all_records(engine.store, pid, "runtime_event")
    delivered = next(e for e in events if e["event_name"] == "project.delivered")
    checked = next(e for e in events if e["id"] == delivered["payload"]["check_ref"]["record_id"])
    assert checked["payload"]["artifact_ref"] == delivery["artifact_ref"]
    assert delivered["payload"]["review_ref"] is None
    assert delivered["payload"]["validation_agent_enabled"] is False
    # A later candidate is a pinned program snapshot, not the old effective Graph.
    audit_task = engine.store.get(delivered["task_id"], pid)
    with engine.store.transaction():
        candidate = engine._assemble(audit_task)
        assert engine._dependencies_valid({"project_id": pid, "input_refs": [ref(candidate)]})
    candidate_graph = candidate["content"]["value"]
    assert candidate_graph["revision"] == graph["revision"] + 1
    assert candidate_graph["chapters"] == graph["chapters"]
    assert engine.status(pid)["latest_delivery"]["artifact_ref"] == delivery["artifact_ref"]
    # Modify one already-delivered chapter, confirm it, then review and deliver again.
    engine.submit_message(pid, cid, "revise chapter")
    async def revise():
        approved = set()
        for _ in range(20):
            await engine.tick(pid, cid)
            state = engine.status(pid)
            assert not [t for t in state["tasks"] if t["state"] == "paused"], [(t["pause_reason"], t["scope"]) for t in state["tasks"] if t["state"] == "paused"]
            chapter_version = engine.workflow.resolve(pid, "chapter_graph", "chapter-one")
            if chapter_version["version"] > 1: return chapter_version
            for item in state["pending_user_items"]:
                if item["kind"] == "confirmation" and item["id"] not in approved:
                    engine.submit_message(pid, cid, "confirm"); approved.add(item["id"])
        pytest.fail("chapter revision did not become effective")
    revised = asyncio.run(revise())
    assert engine.workflow.state(revised)["dependency_status"] == "valid"
    assert engine._control(pid, cid)["holds"] == []
    engine.submit_message(pid, cid, "audit again")
    async def audit_again():
        for _ in range(12):
            await engine.tick(pid, cid)
            status = engine.status(pid)
            assert not [t for t in status["tasks"] if t["state"] == "paused"], [(t["pause_reason"], t["scope"]) for t in status["tasks"] if t["state"] == "paused"]
            if status["latest_delivery"]["artifact_ref"] != delivery["artifact_ref"]: return status["latest_delivery"]
        pytest.fail("revised graph did not deliver: " + repr({
            "tasks": [(t["scope"], t["state"], t["pause_reason"], engine._task_data(t).get("pending_user_items")) for t in status["tasks"]],
            "queue": [(q["state"], q.get("blocked_reason")) for q in status["queue"]],
            "history": [h["content"] for h in all_records(engine.store, pid, "history_record")[-5:]],
            "calls": model.calls,
        }))
    second_delivery = asyncio.run(audit_again())
    second_graph = engine.workflow.fixed_version(pid, second_delivery["artifact_ref"])["content"]["value"]
    assert second_graph["chapters"][1] == graph["chapters"][1]
    assert second_graph["chapters"][0]["nodes"][0]["body"].endswith("修订后的正文。")
    assert second_graph["chapters"][0]["nodes"][0]["id"] == graph["chapters"][0]["nodes"][0]["id"]


def test_step10_keeps_graph_baseline_internal_and_recovers_fixed_model_inputs(runtime):
    from branch_agent.context import prepare_runtime_materials, build_materials
    from branch_agent.graph import schema, quality_checks
    engine, _, pid, cid = runtime
    engine.import_source(pid, cid, "甲见乙。", "故事")
    source = engine.workflow.resolve(pid, "source_text")
    with engine.store.transaction():
        message = engine._message(pid, cid, "生成章节Graph", role="user")
        root = engine._new_task(pid, cid, message, "generate", is_workflow=True, chapter_ids=["chapter-one"])
        for kind, stage in (("adaptation_plan", 4), ("chapter_design", 9)):
            definition = schema(kind)
            payload = _synthetic(definition["properties"]["payload"]["anyOf"][0], definition, ref(source))
            if stage == 9:
                payload["chapter_id"] = "chapter-one"
                payload["chapter_source_anchors"] = [{"source_ref": ref(source),
                    "start_utf16": 0, "end_utf16": 4, "exact_quote": None,
                    "prefix": None, "suffix": None}]
            output = {"result_kind": "ready", "payload": payload, "questions": [], "evidence_refs": [], "notes": []}
            engine.workflow.save(pid, kind, output, stage=stage,
                                 chapter_id="chapter-one" if stage == 9 else None,
                                 effective=True, origin="program", inputs=[ref(source)])
        task = engine._new_task(pid, cid, message, "generate", stage=10, chapter="chapter-one", parent=root)
        materials = engine.workflow.materials(pid, 10, "chapter-one")
        assert {m["schema_id"] for m in materials} == {"chapter_design", "source_text"}
        assert engine._graph_write_materials(task) == []
        data = engine._task_data(task)
        baseline = engine.workflow.fixed_version(pid, data["baseline_ref"])
        graph = engine.store.get(data["graph_write_ref"]["record_id"], project_id=pid)
        assert graph["payload"]["baseline_ref"] == data["baseline_ref"]
        assert "contract" not in baseline["content"]["value"]
        assert [check["check_id"] for check in quality_checks(baseline["content"]["value"])] == [
            "schema_contract", "unreachable_nodes"]
        task, run, session, values = engine._start_run(task, 10, materials)
        assert session["session_key"] == "chapter:chapter-one:writer"
        assert set(str(r) for r in run["input_refs"]) == set(str(m["ref"]) for m in materials)
    prepared = prepare_runtime_materials("step10", task, run, session, values, materials, engine.store)
    packed, _, _ = build_materials("step10", prepared, values, engine.store, pid)
    assert {item["source_kind"] for item in packed} == {"chapter_design", "source_text"}
    assert {item.get("builtin") for item in packed if item.get("builtin")} == {"runtime.source_block"}
    recovered = engine._fixed_run_materials(run)
    replay_prepared = prepare_runtime_materials("step10", task, run, session, values, recovered, engine.store)
    replay, _, _ = build_materials("step10", replay_prepared, values, engine.store, pid)
    assert replay == packed


def _review_scope_runtime(runtime, *, enable_validation=True):
    import json
    from branch_agent.graph import SCHEMA_DIR, schema
    engine, model, pid, cid = runtime
    # The optional validation Agent is disabled by default.  These tests
    # explicitly enable it because they exercise its review/repair contract.
    if enable_validation:
        engine.config_service.base["prompts"]["validation"] = "检查最终 Graph 的语义质量。"
        engine.config_service.base["prompts"]["validation_enabled"] = True
    engine.import_source(pid, cid, "甲见乙。", "审核合成原作")
    source = engine.workflow.resolve(pid, "source_text")
    with engine.store.transaction():
        message = engine._message(pid, cid, "审核固定Graph", role="user")
        root = engine._new_task(pid, cid, message, "generate", is_workflow=True, stages=[11], chapter_ids=["ch:station"])
        for kind, stage in (("adaptation_plan", 4), ("ending_routes", 7), ("chapter_design", 9)):
            definition = schema(kind)
            payload = _synthetic(definition["properties"]["payload"]["anyOf"][0], definition, ref(source))
            if stage == 9: payload["chapter_id"] = "ch:station"
            engine.workflow.save(pid, kind, {"result_kind": "ready", "payload": payload, "questions": [], "evidence_refs": [], "notes": []},
                stage=stage, chapter_id="ch:station" if stage == 9 else None, effective=True, origin="program", inputs=[ref(source)])
        chapter = json.loads((SCHEMA_DIR / "examples/chapter_graph.example.json").read_text())
        chapter["evidence_refs"] = []
        chapter["payload"]["shared_scenes"] = []
        for node in chapter["payload"]["chapter"]["nodes"]: node["sceneId"] = ""
        engine.workflow.save(pid, "chapter_graph", chapter, stage=10, chapter_id="ch:station", effective=True, origin="program", inputs=[ref(source)])
        task = engine._new_task(pid, cid, message, "generate", stage=11, parent=root)
    def report(task, materials, checked=None, unchecked=None):
        graph = next(m for m in materials if m["schema_id"] == "nexo_graph")
        assert len(materials) == 1
        location = {**graph["ref"], "item_id": "n:check", "json_pointer": "/chapters/0/nodes/4"}
        finding = {"issue_id": "missing-block", "severity": "major", "category": "structure", "location_ref": location,
            "statement": "条件需要补充明确的默认阻断路径。", "evidence_refs": [location], "impact": "避免无条件进入后续剧情。",
            "suggested_fix": "在当前条件节点补全默认阻断路径，保留已确认正文及节点ID。", "suggested_owner": "chapter_writer",
            "graph_targets": [{"object_kind": "node", "object_id": "n:check", "chapter_id": "ch:station", "node_id": None,
                               "json_pointer": "/chapters/0/nodes/4"}]}
        return {"result_kind": "ready", "payload": {"reviewed_artifact_refs": [graph["ref"]], "criteria_ref": graph["ref"],
            "proposed_verdict": "needs_revision", "checked_scope": ["ch:station"] if checked is None else checked,
            "unchecked_scope": [] if unchecked is None else unchecked, "findings": [finding], "metrics": [],
            "graph_checks": []},
            "questions": [], "evidence_refs": [graph["ref"]], "notes": []}
    return engine, model, pid, cid, task, report


def test_validation_agent_is_disabled_by_default_and_script_checks_deliver(runtime):
    engine, model, pid, cid, task, _ = _review_scope_runtime(runtime, enable_validation=False)

    asyncio.run(engine.tick(pid, cid))

    current = engine.store.get(task["id"], pid)
    assert current["state"] == "succeeded"
    assert model.calls == []
    delivery = engine.status(pid)["latest_delivery"]
    event = next(record for record in all_records(engine.store, pid, "runtime_event", event_name="project.delivered")
                 if record["payload"]["artifact_ref"] == delivery["artifact_ref"])
    assert event["payload"]["review_ref"] is None
    assert event["payload"]["validation_agent_enabled"] is False
    shown = [record["content"]["text"] for record in all_records(engine.store, pid, "history_record")
             if record["role"] == "assistant" and record["visibility"] == "conversation"]
    assert any("schema_contract" in text and "不存在不可达节点" in text for text in shown)


def test_enabled_validation_agent_receives_only_final_graph(runtime):
    engine, model, pid, cid, task, report = _review_scope_runtime(runtime)

    def passing(task, materials):
        graph = next(material for material in materials if material["schema_id"] == "nexo_graph")
        assert len(materials) == 1
        return {"result_kind": "ready", "payload": {
            "reviewed_artifact_refs": [graph["ref"]], "criteria_ref": graph["ref"],
            "proposed_verdict": "pass", "checked_scope": ["ch:station"],
            "unchecked_scope": [], "findings": [], "metrics": [], "graph_checks": [],
        }, "questions": [], "evidence_refs": [graph["ref"]], "notes": []}

    model.responses = [passing]
    asyncio.run(engine.tick(pid, cid))

    assert model.calls == ["step11"]
    assert engine.store.get(task["id"], pid)["state"] == "succeeded"
    event = all_records(engine.store, pid, "runtime_event", event_name="project.delivered")[-1]
    assert event["payload"]["validation_agent_enabled"] is True
    assert event["payload"]["review_ref"] is not None


def test_script_validation_failure_reason_is_shown_in_chat(runtime):
    engine, model, pid, cid, task, _ = _review_scope_runtime(runtime, enable_validation=False)
    current = engine.workflow.resolve(pid, "chapter_graph", "ch:station")
    value = deepcopy(current["content"]["value"])
    island = deepcopy(next(node for node in value["payload"]["chapter"]["nodes"]
                              if node["kind"] == "story"))

    def rename_ids(node):
        if isinstance(node, dict):
            return {key: (child + "-island" if key == "id" and isinstance(child, str)
                          else rename_ids(child)) for key, child in node.items()}
        if isinstance(node, list):
            return [rename_ids(child) for child in node]
        return node

    island = rename_ids(island)
    island["id"] = "unreachable-island"
    island["chapterStart"] = False
    value["payload"]["chapter"]["nodes"].append(island)
    with engine.store.transaction():
        engine.workflow.save(pid, "chapter_graph", value, stage=10, chapter_id="ch:station",
                             effective=True, origin="program", inputs=current["source_refs"])

    asyncio.run(engine.tick(pid, cid))

    current_task = engine.store.get(task["id"], pid)
    assert current_task["state"] == "paused"
    assert current_task["pause_reason"] == "script_validation_failed"
    assert model.calls == []
    shown = [record["content"]["text"] for record in all_records(engine.store, pid, "history_record")
             if record["role"] == "assistant" and record["visibility"] == "conversation"]
    assert any("unreachable_nodes" in text and "unreachable-island" in text and "没有任何入边" in text
               for text in shown)


@pytest.mark.parametrize("case", ["description", "unknown", "missing", "unchecked", "mixed_unchecked"])
def test_review_scope_format_repairs_without_faking_coverage(runtime, case):
    engine, model, pid, cid, task, report = _review_scope_runtime(runtime)
    checked = {"description": ["已审读 ch:station 的全部条件分支"], "unknown": ["unknown-chapter"], "missing": [], "unchecked": ["ch:station"], "mixed_unchecked": ["已审读 ch:station 的全部条件分支"]}[case]
    model.responses = [lambda task, materials: report(task, materials, checked, ["ch:station"] if case in ("unchecked", "mixed_unchecked") else [])]
    asyncio.run(engine.tick(pid, cid))
    current = engine.store.get(task["id"], pid)
    if case in ("description", "unknown"):
        assert current["state"] == "running" and current["repair_rounds_used"] == 1
        event = all_records(engine.store, pid, "runtime_event", event_name="repair.scheduled")[-1]
        assert "review_scope_format_invalid" in event["payload"]["error"]
        assert "expected_chapter_ids" in event["payload"]["error"] and "actual_checked_scope" in event["payload"]["error"]
        assert "保留真实审核发现" in event["payload"]["error"]
        assert checked[0] in event["payload"]["error"]
    else:
        assert current["state"] == "paused" and current["pause_reason"] == "review_scope_incomplete"
        assert current["repair_rounds_used"] == 0
    assert not engine.status(pid)["latest_delivery"]


@pytest.mark.parametrize("prior_repairs,repeat_bad", [(0, False), (1, False), (1, True)])
def test_legacy_paused_review_gets_bounded_format_repair_and_never_delivers_revision(runtime, monkeypatch, prior_repairs, repeat_bad):
    engine, model, pid, cid, task, report = _review_scope_runtime(runtime)
    with engine.store.transaction(): update(engine.store, task, repair_rounds_used=prior_repairs)
    legacy = engine._deliver_or_repair
    def old_scope_check(task, run, version, result):
        if result["payload"]["checked_scope"] == ["已审读 ch:station 的全部条件分支"]:
            raise WorkflowBlocked("review_scope_incomplete")
        return legacy(task, run, version, result)
    monkeypatch.setattr(engine, "_deliver_or_repair", old_scope_check)
    bad = lambda task, materials: report(task, materials, ["已审读 ch:station 的全部条件分支"])
    model.responses = [bad, bad, report] if repeat_bad else [bad, report]
    asyncio.run(engine.tick(pid, cid))
    current = engine.store.get(task["id"], pid)
    assert current["state"] == "paused" and current["pause_reason"] == "review_scope_incomplete"
    old_run = engine._task_data(current)["current_run_id"]
    original = deepcopy(engine._projection(pid, "run_result", old_run)["output"])
    monkeypatch.setattr(engine, "_deliver_or_repair", legacy)
    engine.control_task(pid, task["id"], "continue")
    asyncio.run(engine.tick(pid, cid))
    assert model.calls == ["step11"]  # The existing response is classified before asking for repair.
    current = engine.store.get(task["id"], pid)
    assert current["state"] == "running" and current["repair_rounds_used"] == prior_repairs + 1
    assert old_run in engine._task_data(current)["rejected_output_run_ids"]
    asyncio.run(engine.tick(pid, cid))
    current = engine.store.get(task["id"], pid)
    assert model.calls == ["step11", "step11"]
    assert not engine.status(pid)["latest_delivery"]
    assert engine._projection(pid, "run_result", old_run)["output"] == original
    if prior_repairs:
        assert current["state"] == "paused" and current["pause_reason"] == "repair_exhausted"
        assert current["repair_rounds_used"] == 2
        engine.control_task(pid, task["id"], "continue")
        asyncio.run(engine.tick(pid, cid))
        assert model.calls == ["step11"] * (3 if repeat_bad else 2)
        # Corrected needs_revision reuses its cache; a rejected format needs a new call.
    current = engine.store.get(task["id"], pid)
    assert current["state"] == "waiting_user"
    repairs = all_records(engine.store, pid, "runtime_event", event_name="review.repair_scheduled")
    assert len(repairs) == 1 and not engine.status(pid)["latest_delivery"]
    fix = engine.store.get(repairs[0]["payload"]["task_ids"][0], pid)
    assert fix["scope"]["stage"] == 10
    assert "条件需要补充明确的默认阻断路径" in engine._task_data(fix)["request"]


def test_evidence_pointer_and_item_identity_are_verified(runtime):
    engine, _, pid, cid = runtime
    imported = engine.import_source(pid, cid, "甲见乙。", "故事")
    source = engine.workflow.resolve(pid, "source_text")
    missing = {**ref(source), "json_pointer": "/invented"}
    with pytest.raises(WorkflowBlocked, match="evidence_pointer_missing") as error:
        engine.workflow.validate_evidence(pid, {"evidence_refs": [missing]})
    assert error.value.details["reference"] == missing
    assert error.value.details["json_pointer"] == "/invented"
    with pytest.raises(WorkflowBlocked, match="evidence_item"):
        engine.workflow.validate_evidence(pid, {"evidence_refs": [{**ref(source), "item_id": "invented"}]})


@pytest.mark.parametrize("case", ["missing_id", "missing_version", "invalid_version", "unversioned_business", "unrelated"])
def test_output_evidence_reference_errors_are_narrowly_classified(runtime, monkeypatch, case):
    from branch_agent.storage import InvalidRecord
    engine, _, pid, cid = runtime
    engine.import_source(pid, cid, "甲见乙。", "故事")
    source = engine.workflow.resolve(pid, "source_text")
    evidence = ref(source)
    if case == "missing_id": evidence["record_id"] = str(uuid4())
    elif case == "missing_version": evidence["version"] = "99"
    elif case == "invalid_version": evidence["version"] = "01"
    elif case == "unversioned_business": evidence.update(record_id=source["id"], version=None)
    else:
        def unrelated(*args): raise InvalidRecord("Projection requires a project_id")
        monkeypatch.setattr(engine.store, "resolve_ref", unrelated)
        with pytest.raises(InvalidRecord, match="Projection requires"):
            engine.workflow.validate_evidence(pid, {"evidence_refs": [evidence]})
        return
    with pytest.raises(WorkflowBlocked, match="evidence_reference_invalid") as error:
        engine.workflow.validate_evidence(pid, {"evidence_refs": [evidence]})
    assert error.value.details["reference"] == evidence
    assert error.value.details["validation_error"]


def test_model_database_reference_is_program_bound_without_repair(runtime):
    engine, model, pid, cid = runtime
    engine.import_source(pid, cid, "甲见乙。", "故事")
    with engine.store.transaction():
        message = engine._message(pid, cid, "切分", role="user")
        task = engine._new_task(pid, cid, message, "generate", stage=1)
    def invalid(task, materials):
        output = source_response(task, materials)
        output["evidence_refs"] = [{**output["evidence_refs"][0], "record_id": str(uuid4())}]
        return output
    model.responses = [invalid]
    asyncio.run(engine.tick(pid, cid))
    current = engine.store.get(task["id"], pid)
    assert current["state"] == "succeeded"
    assert current["repair_rounds_used"] == 0
    assert len(model.calls) == 2
    for kind in ("source_global_events", "source_character_events"):
        saved = engine.workflow.resolve(pid, kind)
        assert saved["content"]["value"]["evidence_refs"] == [ref(engine.workflow.resolve(pid, "source_text"))]


def test_step1_runs_two_agents_concurrently_and_saves_independent_artifacts(runtime):
    engine, _, pid, cid = runtime
    engine.import_source(pid, cid, "甲见乙。", "故事")
    with engine.store.transaction():
        message = engine._message(pid, cid, "切分", role="user")
        task = engine._new_task(pid, cid, message, "generate", stage=1)

    class ParallelModel(FakeModel):
        def __init__(self):
            super().__init__()
            self.started = set()
            self.both_started = asyncio.Event()

        async def run(self, stage, task, run, session, config, materials, message, control=None,
                      step1_view=None, step1_window=None, instructions_override=None):
            self.started.add(step1_view)
            if len(self.started) == 2:
                self.both_started.set()
            await asyncio.wait_for(self.both_started.wait(), 2)
            return await super().run(stage, task, run, session, config, materials, message,
                                     control=control, step1_view=step1_view,
                                     step1_window=step1_window,
                                     instructions_override=instructions_override)

    model = ParallelModel()
    model.store = engine.store
    engine.model_service = model
    asyncio.run(engine.tick(pid, cid))
    assert model.started == {"global", "character"}
    assert engine.store.get(task["id"], pid)["state"] == "succeeded"
    original = engine.workflow.resolve(pid, "source_text")
    children = [child for child in all_records(engine.store, pid, "task") if child["parent_task_id"] == task["id"]]
    assert len(children) == 2
    runs = [run for run in all_records(engine.store, pid, "run") if run["task_id"] in {child["id"] for child in children}]
    assert {run["agent_key"] for run in runs} == {"source_global_parser", "source_character_parser"}
    assert len({run["session_id"] for run in runs}) == 2
    for kind in ("source_global_events", "source_character_events"):
        saved = engine.workflow.resolve(pid, kind)
        assert saved["source_refs"] == [ref(original)]
        assert saved["content"]["value"]["payload"]["source_ref"] == ref(original)


def test_step1_sliding_window_keeps_independent_runs_and_saves_two_views(runtime):
    from branch_agent.context import prepare_runtime_materials, build_materials, utf16_length
    engine, _, pid, cid = runtime
    original = "甲见乙。丙离开。"
    draft = engine.config_service.draft(pid, {"context": {"step1_source": {
        "trigger_tokens": 1, "window_tokens": 7}}})
    engine.config_service.publish(pid, draft["id"])
    engine.import_source(pid, cid, original, "故事")
    with engine.store.transaction():
        message = engine._message(pid, cid, "切分", role="user")
        task = engine._new_task(pid, cid, message, "generate", stage=1)

    class WindowModel(FakeModel):
        async def run(self, stage, task, run, session, config, materials, message, control=None,
                      instructions_override=None, step1_window=None, step1_view=None):
            assert stage == "step1"
            assert step1_window is not None
            prepared = prepare_runtime_materials(stage, task, run, session, config, materials,
                                                 self.store, step1_window=step1_window)
            _, visible, _ = build_materials(stage, prepared, config, self.store, task["project_id"])
            assert visible == step1_window["text"]
            assert step1_window["view"] == step1_view
            self.calls.append((step1_view, session["id"]))
            if control:
                await control()
            source_ref = next(item["ref"] for item in materials if item["schema_id"] == "source_text")
            start, end = step1_window["start_utf16"], step1_window["end_utf16"]
            anchor = {"source_ref": source_ref, "start_utf16": start, "end_utf16": end,
                      "exact_quote": None, "prefix": None, "suffix": None}
            event = {"title": "相遇", "summary": "甲见乙", "narrative_order": 1,
                     "story_time": None, "source_anchors": [anchor]}
            global_events = [{**event, "event_id": "local-1", "character_ids": ["CHAR-甲"]}] if step1_view == "global" else []
            character_views = ([{"character_id": "CHAR-甲", "name": "甲", "aliases": [],
                                 "description": "主人物", "events": [{**event,
                                 "character_event_id": "local-1", "involvement": "见到乙"}]}]
                               if step1_view == "character" else [])
            payload = {"source_ref": source_ref,
                    "covered_source_anchors": [anchor], "remaining_source_anchors": []}
            payload["global_events" if step1_view == "global" else "character_views"] = (
                global_events if step1_view == "global" else character_views)
            return {"result_kind": "ready", "payload": payload,
                    "questions": [], "evidence_refs": [], "notes": []}

    model = WindowModel()
    model.store = engine.store
    engine.model_service = model
    asyncio.run(engine.tick(pid, cid))
    assert engine.store.get(task["id"], pid)["state"] == "succeeded"
    assert sorted(view for view, _ in model.calls) == ["character", "character", "global", "global"]
    assert len({session_id for _, session_id in model.calls}) == 4
    global_view = engine.workflow.resolve(pid, "source_global_events")
    character_view = engine.workflow.resolve(pid, "source_character_events")
    assert len(global_view["content"]["value"]["payload"]["global_events"]) == 2
    assert len(character_view["content"]["value"]["payload"]["character_views"]) == 1


def test_step1_failed_window_keeps_other_view_and_independent_cursor(runtime):
    engine, _, pid, cid = runtime
    draft = engine.config_service.draft(pid, {"context": {"step1_source": {
        "trigger_tokens": 1, "window_tokens": 7}}})
    engine.config_service.publish(pid, draft["id"])
    engine.import_source(pid, cid, "甲见乙。丙离开。", "故事")
    with engine.store.transaction():
        message = engine._message(pid, cid, "切分", role="user")
        task = engine._new_task(pid, cid, message, "generate", stage=1)

    class IncompleteSecondWindow(FakeModel):
        async def run(self, stage, task, run, session, config, materials, message, control=None,
                      instructions_override=None, step1_window=None, step1_view=None):
            source_ref = next(item["ref"] for item in materials if item["schema_id"] == "source_text")
            start, end = step1_window["start_utf16"], step1_window["end_utf16"]
            anchor = {"source_ref": source_ref, "start_utf16": start, "end_utf16": end,
                      "exact_quote": None, "prefix": None, "suffix": None}
            events = ([{"event_id": "local", "title": "事件", "summary": "摘要", "narrative_order": 1,
                        "story_time": None, "character_ids": [], "source_anchors": [anchor]}]
                      if start == 0 and step1_view == "global" else [])
            payload = {"source_ref": source_ref,
                    "covered_source_anchors": [anchor], "remaining_source_anchors": []}
            payload["global_events" if step1_view == "global" else "character_views"] = (
                events if step1_view == "global" else [])
            return {"result_kind": "ready", "payload": payload,
                    "questions": [], "evidence_refs": [], "notes": []}

    model = IncompleteSecondWindow()
    model.store = engine.store
    engine.model_service = model
    asyncio.run(engine.tick(pid, cid))
    assert engine.store.get(task["id"], pid)["state"] == "paused"
    children = {engine._task_data(child)["step1_view"]: child for child in all_records(
        engine.store, pid, "task") if child["parent_task_id"] == task["id"]}
    assert engine._task_data(children["global"])["source_window_state"]["cursor"] == 4
    assert engine._task_data(children["character"])["source_window_state"]["cursor"] == len("甲见乙。丙离开。")
    assert children["character"]["state"] == "succeeded"
    assert engine.workflow.resolve(pid, "source_character_events")
    with pytest.raises(WorkflowBlocked, match="missing_material"):
        engine.workflow.resolve(pid, "source_global_events")


def test_step1_window_resume_adopts_new_size_without_repeating_completed_view(runtime):
    engine, _, pid, cid = runtime
    draft = engine.config_service.draft(pid, {"context": {"step1_source": {
        "trigger_tokens": 1, "window_tokens": 7}}})
    engine.config_service.publish(pid, draft["id"])
    engine.import_source(pid, cid, "甲见乙。丙离开。", "故事")
    with engine.store.transaction():
        message = engine._message(pid, cid, "切分", role="user")
        task = engine._new_task(pid, cid, message, "generate", stage=1)

    class ResizeModel(FakeModel):
        def __init__(self):
            super().__init__()
            self.windows = []

        async def run(self, stage, task, run, session, config, materials, message, control=None,
                      instructions_override=None, step1_window=None, step1_view=None):
            if control:
                await control()
            start, end = step1_window["start_utf16"], step1_window["end_utf16"]
            size = config["context"]["step1_source"]["window_tokens"]
            self.windows.append((step1_view, start, size))
            source_ref = next(item["ref"] for item in materials if item["schema_id"] == "source_text")
            anchor = {"source_ref": source_ref, "start_utf16": start, "end_utf16": end,
                      "exact_quote": None, "prefix": None, "suffix": None}
            event = {"event_id": "local", "title": "事件", "summary": "摘要",
                     "narrative_order": 1, "story_time": None, "character_ids": [],
                     "source_anchors": [anchor]}
            global_events = ([event] if step1_view == "global" and (start == 0 or size > 7)
                             else [])
            payload = {"source_ref": source_ref, "covered_source_anchors": [anchor],
                       "remaining_source_anchors": []}
            payload["global_events" if step1_view == "global" else "character_views"] = (
                global_events if step1_view == "global" else [])
            return {"result_kind": "ready", "payload": payload,
                    "questions": [], "evidence_refs": [], "notes": []}

    model = ResizeModel()
    model.store = engine.store
    engine.model_service = model
    asyncio.run(engine.tick(pid, cid))
    assert engine.store.get(task["id"], pid)["state"] == "paused"
    character = engine.workflow.resolve(pid, "source_character_events")
    first_character_calls = [call for call in model.windows if call[0] == "character"]
    assert ("global", 4, 7) in model.windows

    enlarged = engine.config_service.draft(pid, {"context": {"step1_source": {
        "trigger_tokens": 1, "window_tokens": 12}}})
    engine.config_service.publish(pid, enlarged["id"])
    engine.control_task(pid, task["id"], "continue")
    asyncio.run(engine.tick(pid, cid))
    assert engine.store.get(task["id"], pid)["state"] == "succeeded"
    assert ("global", 4, 12) in model.windows
    assert [call for call in model.windows if call[0] == "character"] == first_character_calls
    assert engine.workflow.resolve(pid, "source_character_events")["id"] == character["id"]
    assert engine.workflow.resolve(pid, "source_global_events")


@pytest.mark.parametrize("entry", ["continue", "legacy_continue", "stale_resume", "crash_recovery"])
def test_rejected_output_does_not_consume_repair_rounds_after_resume(runtime, entry):
    engine, model, pid, cid = runtime
    engine.import_source(pid, cid, "甲见乙。", "故事")
    with engine.store.transaction():
        source = engine.workflow.resolve(pid, "source_text")
        materials = [{"schema_id": "source_text", "content": "甲见乙。", "ref": ref(source)}]
        views = source_response(None, materials)
        global_view = deepcopy(views)
        global_view["payload"].pop("character_views")
        character_view = deepcopy(views)
        character_view["payload"].pop("global_events")
        engine.workflow.save(pid, "source_global_events", global_view, stage=1,
                             inputs=[ref(source)], effective=True)
        engine.workflow.save(pid, "source_character_events", character_view, stage=1,
                             inputs=[ref(source)], effective=True)
        message = engine._message(pid, cid, "分析原作", role="user")
        task = engine._new_task(pid, cid, message, "generate", stage=2)
    invalid = "缺少 Step2 双分析分隔标记"
    model.responses = [invalid, invalid, step2_response()]
    asyncio.run(engine.tick(pid, cid))
    prior_id = engine._task_data(task)["current_run_id"]
    prior_output = engine._projection(pid, "run_result", prior_id)["output"]
    if entry == "legacy_continue":
        with engine.store.transaction():
            data = engine._task_data(task)
            data.pop("rejected_output_run_ids")
            engine._save_task_data(task, data)
    engine.control_task(pid, task["id"], "stop")
    engine.control_task(pid, task["id"], "continue")
    if entry == "stale_resume":
        with engine.store.transaction():
            data = engine._task_data(task)
            data["resume_saved_result"] = {"source_run_id": prior_id, "output": prior_output}
            engine._save_task_data(task, data)
    if entry == "crash_recovery":
        with engine.store.transaction():
            current = engine.store.get(task["id"], pid)
            current, replay, session, _ = engine._start_run(current, 2, engine.workflow.materials(pid, 2), recovery=True)
            engine._save_projection(pid, "run_result", replay["id"], {"source_run_id": prior_id, "output": prior_output})
            update(engine.store, engine.store.get(replay["id"], pid), lease_expires_at=_after(engine.store.now(), -60))
            update(engine.store, session, lease_expires_at=_after(engine.store.now(), -60))
        engine = Engine(engine.store, model, engine.config_service)
        asyncio.run(engine._recover(pid, cid))
        with engine.store.transaction():
            data = engine._task_data(task)
            data["next_retry_at"] = _after(engine.store.now(), -1)
            engine._save_task_data(task, data)
    asyncio.run(engine.tick(pid, cid))
    assert model.calls == ["step2", "step2"]
    current = engine.store.get(task["id"], pid)
    assert current["state"] == "running" and current["repair_rounds_used"] == 2
    asyncio.run(engine.tick(pid, cid))
    current = engine.store.get(task["id"], pid)
    assert current["state"] == "waiting_user" and current["repair_rounds_used"] == 2
    assert model.calls == ["step2"] * 3
    assert engine.workflow.resolve(pid, "source_global_analysis", effective=False)
    assert engine.workflow.resolve(pid, "source_character_analysis", effective=False)
    assert len(all_records(engine.store, pid, "runtime_event", event_name="repair.scheduled")) == 2


def test_repair_exhaustion_rejects_last_output_before_explicit_continue(runtime):
    engine, model, pid, cid = runtime
    engine.import_source(pid, cid, "甲见乙。", "故事")
    with engine.store.transaction():
        source = engine.workflow.resolve(pid, "source_text")
        materials = [{"schema_id": "source_text", "content": "甲见乙。", "ref": ref(source)}]
        views = source_response(None, materials)
        global_view = deepcopy(views)
        global_view["payload"].pop("character_views")
        character_view = deepcopy(views)
        character_view["payload"].pop("global_events")
        engine.workflow.save(pid, "source_global_events", global_view, stage=1,
                             inputs=[ref(source)], effective=True)
        engine.workflow.save(pid, "source_character_events", character_view, stage=1,
                             inputs=[ref(source)], effective=True)
        message = engine._message(pid, cid, "分析原作", role="user")
        task = engine._new_task(pid, cid, message, "generate", stage=2)
    invalid = "缺少 Step2 双分析分隔标记"
    model.responses = [invalid, invalid, invalid, step2_response()]
    for _ in range(3): asyncio.run(engine.tick(pid, cid))
    current = engine.store.get(task["id"], pid)
    assert current["state"] == "paused" and current["pause_reason"] == "repair_exhausted"
    data = engine._task_data(current)
    assert data["current_run_id"] in data.get("rejected_output_run_ids", [])
    assert len(data["rejected_output_run_ids"]) == 3
    engine.control_task(pid, task["id"], "continue")
    asyncio.run(engine.tick(pid, cid))
    assert model.calls == ["step2"] * 4
    current = engine.store.get(task["id"], pid)
    assert current["state"] == "waiting_user" and current["repair_rounds_used"] == 2
    assert engine.workflow.resolve(pid, "source_global_analysis", effective=False)
    assert engine.workflow.resolve(pid, "source_character_analysis", effective=False)


@pytest.mark.parametrize("missing_input", [False, True])
def test_output_provenance_is_bound_but_missing_input_pauses(runtime, missing_input):
    from branch_agent.context import MaterialError
    engine, _, pid, cid = runtime
    source = engine.import_source(pid, cid, "甲见乙。", "故事")["source"]
    with engine.store.transaction():
        message = engine._message(pid, cid, "切分", role="user")
        task = engine._new_task(pid, cid, message, "generate", stage=1)

    class ProvenanceModel(FakeModel):
        async def run(self, stage, task, run, session, config, materials, message,
                      control=None, step1_view=None, step1_window=None, instructions_override=None):
            if step1_view == "global" and missing_input:
                self.calls.append(stage)
                raise MaterialError("missing_material_field", "Required input field missing")
            output = await super().run(stage, task, run, session, config, materials, message,
                                       control=control, step1_view=step1_view,
                                       step1_window=step1_window,
                                       instructions_override=instructions_override)
            if step1_view == "global":
                output["evidence_refs"][0]["json_pointer"] = "/invented"
            return output

    model = ProvenanceModel()
    model.store = engine.store
    engine.model_service = model
    asyncio.run(engine.tick(pid, cid))
    current = engine.store.get(task["id"], pid)
    children = {engine._task_data(child)["step1_view"]: child for child in all_records(
        engine.store, pid, "task") if child["parent_task_id"] == task["id"]}
    if missing_input:
        assert current["state"] == "paused"
        assert current["pause_reason"] == "child_blocked"
        assert children["global"]["state"] == "paused"
        assert children["global"]["pause_reason"] == "missing_material_field"
        assert children["global"]["repair_rounds_used"] == 0
        assert children["character"]["state"] == "succeeded"
        assert engine.workflow.resolve(pid, "source_character_events")
        assert not all_records(engine.store, pid, "runtime_event", event_name="repair.scheduled")
    else:
        assert current["state"] == "succeeded"
        assert all(child["repair_rounds_used"] == 0 for child in children.values())
        assert model.calls == ["step1", "step1"]
        global_view = engine.workflow.resolve(pid, "source_global_events")
        assert global_view["content"]["value"]["evidence_refs"] == [ref(source)]


def test_revision_dependency_pause_preserves_the_request_coordinator(runtime):
    engine, _, pid, cid = runtime
    engine.import_source(pid, cid, "甲见乙。", "故事")
    source = engine.workflow.resolve(pid, "source_text")
    with engine.store.transaction():
        message = engine._message(pid, cid, "修改这个原作", role="user")
        coordinator_task = engine._new_task(pid, cid, message, "query", coordinator=True)
        production_task = engine._new_task(pid, cid, message, "generate", stage=1)
        materials = [{"ref": ref(source), "schema_id": "source_text", "content": "甲见乙。"}]
        coordinator_task, _, _, _ = engine._start_run(coordinator_task, "coordinator", materials)
        production_task, _, _, _ = engine._start_run(production_task, 1, materials)
        engine._pause_dependents(pid, source, "unrelated-new-task")
    assert engine.store.get(coordinator_task["id"], pid)["state"] == "running"
    assert engine.store.get(production_task["id"], pid)["state"] == "paused"
    assert [h["task_id"] for h in engine._control(pid, cid)["holds"]] == [production_task["id"]]


def test_coordinator_directory_preserves_confirmation_scope_without_historical_summary_text(runtime):
    from branch_agent.context import tokens
    engine, _, pid, cid = runtime
    engine.import_source(pid, cid, "甲见乙。", "故事")
    source = engine.workflow.resolve(pid, "source_text")
    with engine.store.transaction():
        request = engine._message(pid, cid, "原始请求" * 150, role="user")
        tasks = [engine._new_task(pid, cid, request, "generate", stage=2,
                                 request="重复的原始需求说明" * 100) for _ in range(12)]
        for task in tasks[:-1]:
            engine._transition(task, "succeeded")
        task = tasks[-1]
        shown = engine._message(pid, cid, "请确认所展示的精确范围", task=task["id"])
        target = {"subject": ref(source), "selections": [{"item_id": None, "json_pointer": ""}]}
        presented = [{**target, "message_id": shown["id"], "task_id": task["id"]}]
        pending = engine._wait_item(task, "confirmation", shown, "确认所展示范围", [target])
        data = engine._task_data(task)
        data["pending_user_items"] = [pending]
        engine._save_task_data(task, data)
        engine._transition(task, "waiting_user")
        engine._save_projection(pid, "presentations", cid, {"targets": presented})
        artifact=engine.store.put(new_record('artifact',pid,artifact_kind='work_summary'))
        saved=engine.store.put(new_record('artifact_version',pid,artifact_id=artifact['id'],version=1,
            content={'storage':'inline_text','text':'旧会话工作摘要'},output_schema=None,
            source_refs=[ref(source)],origin='program'))
        engine.store.put(new_record('artifact_state',pid,artifact_id=artifact['id'],artifact_version_id=saved['id'],version=1,
            confirmation_status='not_required',dependency_status='valid',quality_status='passed'))
        artifact.update(latest_version=1,current_effective_version=1);engine.store.update(artifact,artifact['row_version'])
    submitted = engine.submit_message(pid, cid, "确认以上范围")
    queued = next(q for q in engine.status(pid)["queue"] if q["source_message_id"] == submitted["id"])
    before = all_records(engine.store, pid, "task")
    with engine.store.transaction():
        materials = engine._coordinator_materials(pid, cid, queued)
    state = next(m for m in materials if m.get("builtin") == "runtime.status")["content"]
    assert state["presented_at_submission"] == presented
    assert state["pending_user_items"] == [pending]
    assert not any(m["schema_id"] == "work_summary" for m in materials)
    assert len(state["tasks"]) == len(before)
    assert tokens(state["tasks"]) < tokens(before) / 3
    for entry in state["tasks"]:
        original = engine.store.resolve_ref(entry["task_ref"], pid)
        assert entry["scope"] == {k: original["scope"][k] for k in ("stage", "chapter_ids", "branch_ids", "target_refs")}
        assert original["scope"]["description"]
    active = next(t for t in state["tasks"] if t["state"] == "waiting_user")
    assert active["request_ref"] == ref(request)
    assert set(state["queue_gate"]) == {"row_version", "holds", "last_applied_event_seq"}


@pytest.mark.parametrize("recheck_saved,ask_user,invalid_ref", [(False, False, None), (True, False, None), ("restart", False, None), ("legacy_gap", False, None), (False, True, None), (False, "legacy", None), (False, "ancestor", None), (False, False, "once"), (False, False, "always"), (False, False, "continue_exhausted"), (False, False, "crash_rejected")])
def test_long_stage_uses_real_batch_children_and_coverage_before_aggregation(runtime, monkeypatch, recheck_saved, ask_user, invalid_ref):
    from branch_agent.context import BudgetExceeded, prepare_runtime_materials, build_materials
    from branch_agent.context_batching import batch_coverage
    from branch_agent.graph import schema
    engine, _, pid, cid = runtime
    engine.import_source(pid, cid, "甲见乙。", "故事")
    source = engine.workflow.resolve(pid, "source_text")
    response = source_response(None, [{"schema_id": "source_text", "ref": ref(source), "content": "甲见乙。"}])
    from branch_agent.workflow import locate_source_anchors
    response = locate_source_anchors(response, "甲见乙。", ref(source))
    response["payload"]["global_events"] = [dict(response["payload"]["global_events"][0], event_id=f"e{i}", narrative_order=i, summary="局部事件" * 30) for i in range(8)]
    global_response = deepcopy(response)
    global_response["payload"].pop("character_views")
    character_response = deepcopy(response)
    character_response["payload"].pop("global_events")
    with engine.store.transaction():
        engine.workflow.save(pid, "source_global_events", global_response, stage=1,
                             effective=True, inputs=[ref(source)], origin="program")
        engine.workflow.save(pid, "source_character_events", character_response, stage=1,
                             effective=True, inputs=[ref(source)], origin="program")
        message = engine._message(pid, cid, "分析原作", role="user")
        workflow_root = engine._new_task(pid, cid, message, "generate", is_workflow=True, stages=[2], request="完整流程") if ask_user == "ancestor" else None
        task = engine._new_task(pid, cid, message, "generate", stage=2, request="分析原作", parent=workflow_root)
    draft = engine.config_service.draft(pid, {"context": {"batching": {"target_tokens": 700, "hard_max_tokens": 1200, "max_items": 2}}}, scope_kind="stage", scope_key="step2")
    engine.config_service.publish(pid, draft["id"])
    evidence_checked = engine.workflow.validate_evidence
    rejected_once = False
    def check_evidence(project, value):
        nonlocal rejected_once
        if recheck_saved and not rejected_once and value.get("payload", {}).get("task") == "局部分析":
            rejected_once = True
            raise WorkflowBlocked("evidence_item_ambiguous_or_missing")
        return evidence_checked(project, value)
    monkeypatch.setattr(engine.workflow, "validate_evidence", check_evidence)
    validate = engine.workflow.catalog.validate
    legacy_rejected = False
    def validate_result(schema_id, value, schemas=None):
        nonlocal legacy_rejected
        validate(schema_id, value, schemas)
        if ask_user == "legacy" and schema_id == "subtask_result" and value["result_kind"] == "needs_input" and not legacy_rejected:
            legacy_rejected = True
            raise WorkflowBlocked("batch_needs_input")
    monkeypatch.setattr(engine.workflow.catalog, "validate", validate_result)

    class BatchModel:
        def __init__(self): self.calls = []; self.overflowed = False; self.asked = None; self.auxiliary = []
        async def run(self, stage, task, run, session, config, materials, message, control=None):
            self.calls.append(stage)
            prepared = prepare_runtime_materials(stage, task, run, session, config, materials, engine.store)
            packed, _, selections = build_materials(stage, prepared, config, engine.store, pid)
            if control: await control()
            if stage == "coordinator":
                assert message.endswith("以当前已确认的字段为准，旧说明已经过时。")
                return {"result_kind": "ready", "payload": {"reply": "已记录补充，继续原批次。",
                    "source_message_kind": "request", "task_requests": [{
                    "intent": "continue", "stage": None if workflow_root else 2, "chapter_id": None,
                    "target_ref": ref(workflow_root or engine.store.get(self.asked["task_id"], pid)),
                    "request": "继续同一批次", "source_message_ids": [task["requested_by_message_id"]], "requested_confirmation_paths": []}]},
                    "questions": [], "evidence_refs": [], "notes": []}
            if stage == "step2" and not self.overflowed:
                self.overflowed = True
                raise BudgetExceeded("input_budget_exceeded", "Synthetic budget boundary")
            if stage == "aux.subtask":
                ranges = next(m["content"] for m in materials if m.get("builtin") == "runtime.batch_state")
                self.auxiliary.append(task["id"])
                result = {"result_kind": "ready", "payload": {"task": "局部分析", "findings": [], "recommendations": ["保留冲突"],
                        "limitations": [], "artifact_refs": [ranges["manifest_ref"]]}, "questions": [], "evidence_refs": [], "notes": []}
                if ask_user and self.asked is None and len(self.auxiliary) == 2:
                    self.asked = {"task_id": task["id"], "run_id": run["id"], "session_id": session["id"],
                                  "config_version_id": run["config_version_id"], "ranges": deepcopy(ranges)}
                    result = {"result_kind": "needs_input", "payload": None, "questions": [{"question_id": "which-version",
                              "prompt": "字段与旧说明冲突，应以哪个为准？", "reason": "需确定权威来源", "target_field": None,
                              "suggested_answers": [], "blocking_scope": "batch"}], "evidence_refs": [], "notes": []}
                elif self.asked and task["id"] == self.asked["task_id"]:
                    assert "以当前已确认的字段为准，旧说明已经过时。" in message
                    assert session["id"] == self.asked["session_id"]
                    assert run["config_version_id"] == self.asked["config_version_id"]
                    assert ranges == self.asked["ranges"]
                if invalid_ref and (len(self.auxiliary) == 1 or invalid_ref == "always" or (invalid_ref == "continue_exhausted" and len(self.auxiliary) <= 3)):
                    result["payload"]["artifact_refs"] = [{**ranges["manifest_ref"], "record_id": str(uuid4())}]
                elif invalid_ref and len(self.auxiliary) == 2:
                    assert "evidence_reference_invalid" in message
                    assert self.auxiliary[0] == task["id"]
                # Match the real SDK's durable final-response boundary before
                # Engine validates and commits the result in its own transaction.
                with engine.store.transaction():
                    engine._save_projection(pid, "run_result", run["id"], {"output": result})
                return result
            assert stage == "step2"
            assert any("batch" in s["reason"] for s in selections if s["inclusion"] == "omitted")
            return step2_response("故事前提\n批次全局事件分析已汇总。", "人物关系\n保留冲突及人物因果。")

    model = BatchModel(); engine.model_service = model
    async def drive():
        nonlocal engine
        resumed = False
        answered = False
        for _ in range(45):
            await engine.tick(pid, cid)
            current = engine.store.get(task["id"], pid)
            if invalid_ref == "crash_rejected" and len(model.auxiliary) == 1 and not resumed:
                with engine.store.transaction():
                    child = engine.store.get(model.auxiliary[0], pid)
                    previous_id = engine._task_data(child)["current_run_id"]
                    previous = engine.store.get(previous_id, pid)
                    child, replay, session, _ = engine._start_run(child, "aux.subtask", engine._fixed_run_materials(previous), recovery=True)
                    engine._save_projection(pid, "run_result", replay["id"], {
                        "source_run_id": previous_id, "output": engine._projection(pid, "run_result", previous_id)["output"]})
                    update(engine.store, engine.store.get(replay["id"], pid), lease_expires_at=_after(engine.store.now(), -60))
                    update(engine.store, session, lease_expires_at=_after(engine.store.now(), -60))
                engine = Engine(engine.store, model, engine.config_service)
                resumed = True
                continue
            if recheck_saved and current["state"] == "paused" and not resumed:
                assert current["pause_reason"] == "evidence_item_ambiguous_or_missing"
                before = len(model.calls)
                engine.control_task(pid, task["id"], "continue")
                if recheck_saved in ("restart", "legacy_gap"):
                    class SimulatedCrash(BaseException): pass
                    original_assert = engine._assert_run
                    interrupted = []
                    def crash_after_recovery_commit(child, run, token):
                        if run["execution_kind"] == "recovery" and not interrupted:
                            persisted = engine._projection(pid, "run_result", run["id"])
                            assert persisted["output"]["result_kind"] == "ready"
                            assert persisted["source_run_id"] != run["id"]
                            assert not engine._task_data(child)["model_dispatched"]
                            interrupted.append(run)
                            raise SimulatedCrash()
                        return original_assert(child, run, token)
                    monkeypatch.setattr(engine, "_assert_run", crash_after_recovery_commit)
                    with pytest.raises(SimulatedCrash):
                        await engine.tick(pid, cid)
                    assert len(model.calls) == before
                    crashed = interrupted[0]
                    with engine.store.transaction():
                        update(engine.store, engine.store.get(crashed["id"], pid), lease_expires_at=_after(engine.store.now(), -60))
                        update(engine.store, engine.store.get(crashed["session_id"], pid), lease_expires_at=_after(engine.store.now(), -60))
                        if recheck_saved == "legacy_gap":
                            # Reproduce an older binary's committed recovery Run
                            # without its copied response, retaining the real source.
                            engine._save_projection(pid, "run_result", crashed["id"], {})
                            child = engine.store.get(crashed["task_id"], pid)
                            child_data = engine._task_data(child)
                            child_data["model_dispatched"] = True
                            engine._save_task_data(child, child_data)
                    engine = Engine(engine.store, model, engine.config_service)
                await engine.tick(pid, cid)
                assert len(model.calls) == before
                resumed = True
                current = engine.store.get(task["id"], pid)
            if ask_user == "legacy" and current["state"] == "paused" and not resumed:
                assert current["pause_reason"] == "batch_needs_input"
                before = len(model.calls)
                engine.control_task(pid, task["id"], "continue")
                await engine.tick(pid, cid)
                assert len(model.calls) == before
                resumed = True
                current = engine.store.get(task["id"], pid)
            if invalid_ref in ("always", "continue_exhausted") and current["state"] == "paused":
                child = engine.store.get(model.auxiliary[0], pid)
                assert child["state"] == "paused" and child["pause_reason"] == "repair_exhausted"
                assert child["repair_rounds_used"] == 2
                assert len(engine._task_data(child)["rejected_output_run_ids"]) == 3
                assert len(model.auxiliary) == 3 and len(set(model.auxiliary)) == 1
                ledger = batch_coverage(engine.store, pid, engine._task_data(current)["batch_manifest_ref"])
                assert not ledger["complete"] and not ledger["result_refs"]
                if invalid_ref == "continue_exhausted":
                    assert not resumed
                    engine.control_task(pid, task["id"], "continue")
                    resumed = True
                    continue
                return
            assert current["state"] != "paused", current["pause_reason"]
            if ask_user and current["state"] == "waiting_user" and model.asked and not answered:
                waiting_child = engine.store.get(model.asked["task_id"], pid)
                assert waiting_child["state"] == "waiting_user"
                pending = engine._task_data(waiting_child)["pending_user_items"]
                assert pending[0]["description"] == "字段与旧说明冲突，应以哪个为准？"
                data = engine._task_data(current)
                ledger = batch_coverage(engine.store, pid, data["batch_manifest_ref"])
                assert not ledger["complete"] and len(ledger["result_refs"]) == 1
                assert not engine._control(pid, cid)["holds"]
                before = len(model.calls)
                await engine.tick(pid, cid)
                assert len(model.calls) == before
                answer = engine.submit_message(pid, cid, f"针对待办 {pending[0]['id']}，以当前已确认的字段为准，旧说明已经过时。")
                await engine.tick(pid, cid)
                resolved = engine._task_data(waiting_child)["pending_user_items"][0]
                assert resolved["state"] == "resolved" and resolved["answer_message_ids"] == [answer["id"]]
                if workflow_root:
                    route = all_records(engine.store, pid, "runtime_event", event_name="control.continuation_routed")[-1]
                    assert route["payload"]["requested_task_ref"] == ref(workflow_root)
                    assert route["payload"]["resolved_task_ref"]["record_id"] == waiting_child["id"]
                    assert route["payload"]["source_message_id"] == answer["id"]
                answered = True
                continue
            if current["state"] == "waiting_user": return
        pytest.fail("batch parent did not reach a complete presented artifact")
    asyncio.run(drive())
    if invalid_ref == "always": return
    data = engine._task_data(task)
    ledger = batch_coverage(engine.store, pid, data["batch_manifest_ref"])
    assert ledger["complete"]
    assert model.calls.count("aux.subtask") > 1
    assert model.calls.count("step2") == 2
    rejected_calls = 3 if invalid_ref == "continue_exhausted" else 1 if ask_user or invalid_ref else 0
    assert len(ledger["result_refs"]) == model.calls.count("aux.subtask") - rejected_calls
    if invalid_ref:
        assert model.auxiliary.count(model.auxiliary[0]) == rejected_calls + 1
        assert all(model.auxiliary.count(i) == 1 for i in set(model.auxiliary) - {model.auxiliary[0]})
    if ask_user:
        assert model.auxiliary.count(model.asked["task_id"]) == 2
        assert all(model.auxiliary.count(i) == 1 for i in set(model.auxiliary) - {model.asked["task_id"]})
    for result in ledger["result_refs"]:
        artifact = engine.workflow.fixed_version(pid, result)
        producer = engine.store.get(artifact["producer_run_id"], pid)
        child = engine.store.get(producer["task_id"], pid)
        assert child["parent_task_id"] == task["id"] and child["state"] == "succeeded"
    final_analyses = [engine.workflow.fixed_version(pid, data["result_refs"][kind])
                      for kind in ("source_global_analysis", "source_character_analysis")]
    assert all(analysis["output_schema"] is None for analysis in final_analyses)
    assert all(analysis["content"]["storage"] == "inline_text" for analysis in final_analyses)
    if recheck_saved:
        recovered = [r for r in all_records(engine.store, pid, "run") if r["execution_kind"] == "recovery"]
        expected = 2 if recheck_saved in ("restart", "legacy_gap") else 1
        assert len(recovered) == expected and recovered[-1]["state"] == "succeeded"
        assert all(r["model_turns_used"] == 0 for r in recovered)


def test_coordinator_budget_pause_reuses_persisted_response(runtime):
    engine, _, pid, cid = runtime
    class BudgetModel:
        def __init__(self): self.calls = 0
        async def run(self, stage, task, run, session, config, materials, message, control=None):
            self.calls += 1
            with engine.store.transaction():
                engine._save_projection(pid, "run_result", run["id"], {"output": {
                    "result_kind": "ready", "payload": {"reply": "已持久保存的回复",
                        "source_message_kind": "request", "task_requests": []},
                    "questions": [], "evidence_refs": [], "notes": []}})
            raise WorkflowBlocked("cost_limit")
    model = BudgetModel(); engine.model_service = model
    engine.submit_message(pid, cid, "查询状态")
    asyncio.run(engine.tick(pid, cid))
    task = next(t for t in engine.status(pid)["tasks"] if t["state"] == "paused")
    engine.control_task(pid, task["id"], "continue")
    asyncio.run(engine.tick(pid, cid))
    assert model.calls == 1
    assert engine.store.get(task["id"], pid)["state"] == "succeeded"
    assert engine.status(pid)["queue"][0]["state"] == "dispatched"
    histories = [h for h in all_records(engine.store, pid, "history_record") if h["content"].get("text") == "已持久保存的回复"]
    assert len(histories) == 1


@pytest.mark.parametrize("case", ["sole", "ambiguous", "explicit", "confirmation", "unshown"])
def test_ancestor_continue_uses_only_unique_previously_shown_pending(runtime, case):
    engine, _, pid, cid = runtime
    with engine.store.transaction():
        initial = engine._message(pid, cid, "开始全流程", role="user")
        root = engine._new_task(pid, cid, initial, "generate", is_workflow=True)
        engine._transition(root, "waiting_user")
        early = engine._message(pid, cid, "继续", role="user") if case == "unshown" else None
        children, items = [], []
        for index in range(2 if case in ("ambiguous", "explicit") else 1):
            child = engine._new_task(pid, cid, initial, "generate", stage=7, parent=root)
            shown = engine._message(pid, cid, "请确定当前依赖", task=child["id"])
            item = engine._wait_item(child, "confirmation" if case == "confirmation" else "question", shown, "请确定当前依赖")
            data = engine._task_data(child); data["pending_user_items"] = [item]; engine._save_task_data(child, data)
            engine._transition(child, "waiting_user"); children.append(child); items.append(item)
        answer = early or engine._message(pid, cid, (items[0]["id"] if case == "explicit" else "") + "以当前字段为准，继续", role="user")
        request = {"intent": "continue", "stage": None, "chapter_id": None, "target_ref": ref(root),
                   "request": "继续原流程", "source_message_ids": [answer["id"]], "requested_confirmation_paths": []}
        if case in ("ambiguous", "confirmation", "unshown"):
            with pytest.raises(WorkflowBlocked, match="confirmation_required" if case == "confirmation" else "continuation_target_ambiguous"):
                engine._apply_request(request, answer, [], root, {})
            assert all(engine._task_data(c)["pending_user_items"][0]["state"] == "open" for c in children)
        else:
            receipt = engine._apply_request(request, answer, [], root, {})
            assert receipt["task_id"] == children[0]["id"]
            assert engine._task_data(children[0])["pending_user_items"][0]["answer_message_ids"] == [answer["id"]]
            if case == "explicit":
                assert engine._task_data(children[1])["pending_user_items"][0]["state"] == "open"


def test_subtree_stop_restore_and_budget_root_increment(runtime):
    engine, _, pid, cid = runtime
    with engine.store.transaction():
        message = engine._message(pid, cid, "开始", role="user")
        root = engine._new_task(pid, cid, message, "generate", is_workflow=True)
        child = engine._new_task(pid, cid, message, "generate", stage=2, parent=root)
        engine._transition(child, "waiting_user")
    engine.control_task(pid, root["id"], "stop")
    engine.control_task(pid, root["id"], "continue", additional_seconds=10, additional_cost="1")
    assert engine.store.get(child["id"], pid)["state"] == "waiting_user"
    with engine.store.transaction():
        child = engine.store.get(child["id"], pid)
        engine._transition(child, "paused", "cost_limit")
        engine._transition(engine.store.get(root["id"], pid), "paused", "child_blocked")
    initial = engine.store.get(root["id"], pid)["budget"]["max_active_seconds"]
    engine.control_task(pid, child["id"], "continue", additional_seconds=25, additional_cost="2")
    assert engine.store.get(root["id"], pid)["budget"]["max_active_seconds"] == initial + 25
    assert engine.store.get(root["id"], pid)["state"] == "queued"
    assert not engine._control(pid, cid)["holds"]


@pytest.mark.parametrize("hold_case", ["same_stop", "operation_uncertain", "usage_uncertain", "dependency_changed", "other_stop", "legacy"])
def test_parent_continue_releases_only_matching_failed_summary_stop_hold(runtime, hold_case):
    engine, _, pid, cid = runtime
    with engine.store.transaction():
        message = engine._message(pid, cid, "开始", role="user")
        parent = engine._new_task(pid, cid, message, "query")
        summary = engine._new_task(pid, cid, message, "query", parent=parent, parent_owned=True)
        data = engine._task_data(summary); data["stage"] = "aux.summary"; engine._save_task_data(summary, data)
    engine.control_task(pid, parent["id"], "stop")
    with engine.store.transaction():
        summary = update(engine.store, engine.store.get(summary["id"], pid), state="failed", pause_reason="summary_failed")
        data = engine._task_data(summary)
        if hold_case == "other_stop":
            data["stop_command_id"] = str(uuid4())
        if hold_case == "legacy":
            data.pop("stop_command_id")
        engine._save_task_data(summary, data)
        gate = engine._control(pid, cid)
        if hold_case in ("operation_uncertain", "usage_uncertain", "dependency_changed"):
            next(h for h in gate["holds"] if h["task_id"] == summary["id"])["reason_code"] = hold_case
            engine._save_projection(pid, "conversation_control", cid, gate)
    saved_summary = engine.store.get(summary["id"], pid)
    engine.control_task(pid, parent["id"], "continue")
    assert engine.store.get(parent["id"], pid)["state"] == "queued"
    assert engine.store.get(summary["id"], pid) == saved_summary
    remaining = engine._control(pid, cid)["holds"]
    if hold_case == "same_stop":
        assert remaining == []
        events = all_records(engine.store, pid, "runtime_event", event_name="queue.gate_changed")
        assert events[-1]["payload"]["released_holds"][0]["task_id"] == summary["id"]
    else:
        assert len(remaining) == 1 and remaining[0]["task_id"] == summary["id"]


def test_new_source_recursively_invalidates_current_stage_materials(runtime):
    from branch_agent.workflow import locate_source_anchors
    engine, _, pid, cid = runtime
    engine.import_source(pid, cid, "甲见乙。", "旧原作")
    source = engine.workflow.resolve(pid, "source_text")
    output = locate_source_anchors(source_response(None, [{"schema_id": "source_text", "ref": ref(source), "content": "甲见乙。"}]), "甲见乙。", ref(source))
    with engine.store.transaction():
        global_output = deepcopy(output)
        global_output["payload"].pop("character_views")
        character_output = deepcopy(output)
        character_output["payload"].pop("global_events")
        first = engine.workflow.save(pid, "source_global_events", global_output,
                                     inputs=[ref(source)], effective=True, origin="program")
        other = engine.workflow.save(pid, "source_character_events", character_output,
                                     inputs=[ref(source)], effective=True, origin="program")
        value = "故事前提\n甲与乙相遇。\n\n原作保留建议\n保留相遇事件。"
        second = engine.workflow.save(pid, "source_global_analysis", value,
                                      inputs=[ref(first), ref(other)], effective=True, origin="program")
    engine.import_source(pid, cid, "甲拒绝见乙。", "新原作")
    assert engine.workflow.state(first)["dependency_status"] == "review_required"
    assert engine.workflow.state(other)["dependency_status"] == "review_required"
    assert engine.workflow.state(second)["dependency_status"] == "review_required"
    with pytest.raises(WorkflowBlocked): engine.workflow.resolve(pid, "source_global_analysis")


def test_recovery_consumes_saved_run_steer_before_artifact_commit(runtime):
    engine, model, pid, cid = runtime
    engine.import_source(pid, cid, "甲见乙。", "原作")
    source = engine.workflow.resolve(pid, "source_text")
    output = source_response(None, [{"schema_id": "source_text", "ref": ref(source), "content": "甲见乙。"}])
    from branch_agent.workflow import locate_source_anchors
    output = locate_source_anchors(output, "甲见乙。", ref(source))
    with engine.store.transaction():
        for kind, omitted in (("source_global_events", "character_views"),
                              ("source_character_events", "global_events")):
            value = deepcopy(output)
            value["payload"].pop(omitted)
            engine.workflow.save(pid, kind, value, stage=1, effective=True,
                                 inputs=[ref(source)], origin="program")
        message = engine._message(pid, cid, "分析原作", role="user")
        task = engine._new_task(pid, cid, message, "generate", stage=2)
        materials = engine.workflow.materials(pid, 2)
        task, run, session, config = engine._start_run(task, 2, materials)
        result = step2_response()
        engine._save_projection(pid, "run_result", run["id"], {"output": result})
        update(engine.store, engine.store.get(run["id"], pid), lease_expires_at=_after(engine.store.now(), -60))
        update(engine.store, session, lease_expires_at=_after(engine.store.now(), -60))
    steer = engine.submit_message(pid, cid, "补充：先告诉我状态", mode="steer", target_run_id=run["id"])
    model.responses = [{"result_kind": "ready", "payload": {"reply": "已读取运行补充",
        "source_message_kind": "request", "task_requests": []},
        "questions": [], "evidence_refs": [], "notes": []}]
    restarted = Engine(engine.store, model, engine.config_service)
    asyncio.run(restarted.tick(pid, cid))
    with engine.store.transaction():
        current = engine.store.get(task["id"], pid); data = engine._task_data(current)
        data["next_retry_at"] = _after(engine.store.now(), -1); engine._save_task_data(current, data)
    asyncio.run(restarted.tick(pid, cid))
    queued = next(q for q in engine.status(pid)["queue"] if q["source_message_id"] == steer["id"])
    assert queued["state"] == "applied"
    assert model.calls == ["coordinator"]
    assert engine.store.get(task["id"], pid)["state"] == "waiting_user"
    events = all_records(engine.store, pid, "runtime_event")
    adoption = next(e for e in events if e["event_name"] == "control.applied")
    completed = max((e for e in events if e["task_id"] == task["id"] and e["event_name"] == "task.transitioned"), key=lambda e:e["sequence"])
    assert adoption["sequence"] < completed["sequence"]


def test_long_source_coverage_can_use_exact_offsets_without_repeating_text():
    from branch_agent.workflow import locate_source_anchors
    original = "甲见乙。😀" * 5000
    target = {"record_id": str(uuid4()), "version": "1", "item_id": None, "json_pointer": None}
    anchor = {"source_ref": target, "start_utf16": 0, "end_utf16": len(original.encode("utf-16-le")) // 2,
              "exact_quote": None, "prefix": None, "suffix": None}
    result = {"result_kind": "ready", "payload": {"source_ref": target, "global_events": [], "character_views": [],
              "covered_source_anchors": [anchor], "remaining_source_anchors": []}, "questions": [], "evidence_refs": [], "notes": []}
    assert locate_source_anchors(result, original, target) == result
    for start, end in ((None, None), (0, 0), (0, 5), (0, 999999), (False, 4)):
        broken = deepcopy(result); broken["payload"]["covered_source_anchors"][0].update(start_utf16=start, end_utf16=end)
        with pytest.raises(WorkflowBlocked): locate_source_anchors(broken, original, target)


def test_archived_project_queue_is_not_dispatched(runtime):
    engine, model, pid, cid = runtime
    engine.submit_message(pid, cid, "queued before archive")
    with engine.store.transaction():
        update(engine.store, engine.store.get(pid, pid), state="archived")
    asyncio.run(engine.tick(pid, cid))
    assert model.calls == []
    assert engine.status(pid)["queue"][0]["state"] == "pending"
