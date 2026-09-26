"""Account artifact defaults must behave like real, versioned project artifacts."""

from copy import deepcopy
import os
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import make_conninfo

from branch_agent.artifact_workspace import ArtifactWorkspace
from branch_agent.configuration import ConfigService
from branch_agent.context import build_materials, prepare_runtime_materials
from branch_agent.engine import Engine
from branch_agent.records import new_record
from branch_agent.storage import Store
from branch_agent.workflow import all_records, body, ref, update


ACCOUNT = "artifact-defaults-test"


@pytest.fixture
def runtime(tmp_path):
    base_dsn = os.getenv("BRANCH_AGENT_TEST_DSN", "postgresql:///branch_agent_local")
    namespace = "artifact_defaults_test_" + uuid4().hex
    with psycopg.connect(base_dsn, autocommit=True) as connection:
        connection.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(namespace)))
    store = Store(make_conninfo(base_dsn, options=f"-c search_path={namespace}"), tmp_path / "blobs")
    store.migrate()
    config = ConfigService(store)
    workspace = ArtifactWorkspace(store, config)
    yield store, config, workspace
    store.close()
    with psycopg.connect(base_dsn, autocommit=True) as connection:
        connection.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(namespace)))


def _plan_default():
    return {
        "result_kind": "ready",
        "payload": {
            "title": "人工设置的默认改编方案",
            "logline": "",
            "premise_and_scope": "",
            "source_global_analysis_ref": None,
            "source_character_analysis_ref": None,
            "strategy_ref": None,
            "player_role": {"character_ref": None, "description": "", "perspective": ""},
            "experience_goals": [],
            "world_and_character_changes": [],
            "entity_specs": [],
            "narrative_constraints": [],
            "writing_style": {"tone": "", "dialogue": "", "narration": "", "content_boundaries": []},
            "stage_artifact_refs": {
                "game_events": None,
                "event_functions": None,
                "ending_routes": None,
                "player_profiles": None,
            },
        },
        "questions": [],
        "evidence_refs": [],
        "notes": [],
    }


def _project(store, workspace):
    with store.transaction():
        project = store.put(new_record("project", None, owner_account_id=ACCOUNT, title="新项目"))
        workspace.seed_project(project["id"], ACCOUNT)
        conversation = store.put(new_record("conversation", project["id"], title="创作会话"))
    return project["id"], conversation["id"]


def test_account_defaults_are_copied_into_independent_effective_artifact_chains(runtime):
    store, _, workspace = runtime
    workspace.save_defaults(ACCOUNT, {"adaptation_plan": _plan_default()})
    first, _ = _project(store, workspace)
    second, _ = _project(store, workspace)

    for project_id in (first, second):
        for kind in ("adaptation_strategy", "adaptation_plan"):
            item = workspace.item(project_id, kind)
            assert item["version"] == item["effective_version"] == 1
            assert item["effective"] is True
            assert item["origin"] == "program"
            assert item["content"] == workspace.defaults(ACCOUNT)[kind]

    modified = deepcopy(workspace.item(first, "adaptation_plan")["content"])
    modified["payload"]["title"] = "第一个项目的方案"
    saved = workspace.save_project(first, "adaptation_plan", modified, expected_version=1)
    assert saved["version"] == saved["effective_version"] == 2
    assert saved["origin"] == "user"
    assert workspace.item(second, "adaptation_plan")["content"]["payload"]["title"] == "人工设置的默认改编方案"
    assert workspace.defaults(ACCOUNT)["adaptation_plan"]["payload"]["title"] == "人工设置的默认改编方案"


def test_seeded_artifacts_do_not_count_as_completed_step3_or_step4(runtime):
    store, config, workspace = runtime
    workspace.save_defaults(ACCOUNT, {"adaptation_plan": _plan_default()})
    project_id, conversation_id = _project(store, workspace)
    engine = Engine(store, object(), config, cost_gates_enabled=False)
    strategy = deepcopy(workspace.item(project_id, "adaptation_strategy")["content"])
    strategy["payload"]["adaptation_principles"] = ["由调试同学设置的策略"]
    workspace.save_project(project_id, "adaptation_strategy", strategy, expected_version=1)
    plan = deepcopy(workspace.item(project_id, "adaptation_plan")["content"])
    plan["payload"]["title"] = "由调试同学设置的方案"
    workspace.save_project(project_id, "adaptation_plan", plan, expected_version=1)
    with store.transaction():
        message = engine._message(project_id, conversation_id, "开始完整改编", role="user")
        root = engine._new_task(project_id, conversation_id, message, "generate",
                                is_workflow=True, stages=[3, 4], request="开始完整改编")
        engine._advance_root(root)
        first = [task for task in all_records(store, project_id, "task", parent_task_id=root["id"])]
        assert len(first) == 1 and first[0]["scope"]["stage"] == 3
        update(store, first[0], state="succeeded")
        engine._advance_root(store.get(root["id"], project_id=project_id))
        children = all_records(store, project_id, "task", parent_task_id=root["id"])
    assert [task["scope"]["stage"] for task in children] == [3, 4]


def test_step3_run_freezes_the_current_strategy_as_an_input(runtime):
    store, config, workspace = runtime
    project_id, conversation_id = _project(store, workspace)
    engine = Engine(store, object(), config, cost_gates_enabled=False)
    strategy = engine.workflow.resolve(project_id, "adaptation_strategy")

    with store.transaction():
        engine.workflow.save(project_id, "source_global_analysis", "全局事件分析", stage=2,
                             origin="program", effective=True)
        engine.workflow.save(project_id, "source_character_analysis", "人物事件分析", stage=2,
                             origin="program", effective=True)
        message = engine._message(project_id, conversation_id, "生成互动策略", role="user")
        task = engine._new_task(project_id, conversation_id, message, "generate", stage=3)
        materials = engine.workflow.materials(project_id, 3)
        baseline = [item for item in materials if item.get("material_role") == "current_stage_baseline"]
        assert len(baseline) == 1
        assert baseline[0]["schema_id"] == "adaptation_strategy"
        assert baseline[0]["ref"] == ref(strategy)
        assert baseline[0]["content"] == body(store, strategy)

        task, run, session, values = engine._start_run(task, 3, materials)
        assert ref(strategy) in run["input_refs"]
        prepared = prepare_runtime_materials("step3", task, run, session, values, materials, store)
        _, _, selections = build_materials("step3", prepared, values, store, project_id)
        assert any(selection["source_ref"] == ref(strategy) and selection["inclusion"] == "included"
                   for selection in selections)


@pytest.mark.parametrize("kind,stage", [
    ("adaptation_strategy", 3),
    ("adaptation_plan", 4),
])
def test_confirmed_version_does_not_depend_on_its_own_prior_baseline(runtime, kind, stage):
    store, config, workspace = runtime
    workspace.save_defaults(ACCOUNT, {"adaptation_plan": _plan_default()})
    project_id, conversation_id = _project(store, workspace)
    engine = Engine(store, object(), config, cost_gates_enabled=False)
    previous = engine.workflow.resolve(project_id, kind)
    content = deepcopy(body(store, previous))
    if kind == "adaptation_strategy":
        content["payload"]["adaptation_principles"] = ["Agent 更新的改编原则"]
    else:
        content["payload"]["title"] = "Agent 更新的改编方案"

    with store.transaction():
        candidate = engine.workflow.save(project_id, kind, content, stage=stage,
                                         inputs=[ref(previous)], effective=False)
        message = engine._message(project_id, conversation_id, "确认这一版", role="user")
        request = {"source_message_ids": [message["id"]], "target_ref": ref(candidate),
                   "requested_confirmation_paths": ["/payload"]}
        presented = [{"subject": ref(candidate),
                      "selections": [{"item_id": None, "json_pointer": "/payload"}]}]
        _, _, complete = engine.workflow.confirm(project_id, request, message, presented)

    assert complete
    assert engine.workflow.resolve(project_id, kind)["id"] == candidate["id"]
    assert engine.workflow.state(candidate)["dependency_status"] == "valid"
    assert not any(dep["producer_ref"] == ref(previous)
                   for dep in all_records(store, project_id, "dependency")
                   if dep["consumer_ref"] == ref(candidate))
