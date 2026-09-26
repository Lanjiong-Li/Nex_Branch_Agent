"""Step 1 saves two independent views of the same fixed original."""
import os
from copy import deepcopy
from uuid import uuid4

import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo
import pytest

from branch_agent.records import new_record, scope
from branch_agent.schemas import SchemaCatalog
from branch_agent.storage import Store
from branch_agent.workflow import PROGRAM_REQUIRES, STAGE_OUTPUTS, Workflow, WorkflowBlocked, body, ref


@pytest.fixture
def source_case(tmp_path):
    dsn = os.getenv("BRANCH_AGENT_TEST_DSN", "postgresql:///branch_agent_local")
    namespace = "split_source_test_" + uuid4().hex
    with psycopg.connect(dsn, autocommit=True) as connection:
        connection.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(namespace)))
    store = Store(make_conninfo(dsn, options="-c search_path=" + namespace), tmp_path / "blobs")
    store.migrate()
    project = store.put(new_record("project", None, owner_account_id="split-source-test", title="Split source"))
    workflow = Workflow(store)
    try:
        yield store, workflow, project["id"]
    finally:
        store.close()
        with psycopg.connect(dsn, autocommit=True) as connection:
            connection.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(namespace)))


def _anchor(source_ref):
    return {"source_ref": source_ref, "start_utf16": 0, "end_utf16": 4,
            "exact_quote": None, "prefix": None, "suffix": None}


def _output(kind, source_ref):
    anchor = _anchor(source_ref)
    payload = {"source_ref": source_ref, "covered_source_anchors": [anchor],
               "remaining_source_anchors": []}
    if kind == "source_global_events":
        payload["global_events"] = [{"event_id": "GEV-1", "title": "相遇", "summary": "甲见乙。",
            "narrative_order": 1, "story_time": None, "character_ids": ["C-甲"],
            "source_anchors": [anchor]}]
    else:
        payload["character_views"] = [{"character_id": "C-甲", "name": "甲", "aliases": [],
            "description": "人物", "events": [{"character_event_id": "CEV-1", "title": "遇见乙",
                "summary": "甲见乙。", "narrative_order": 1, "story_time": None,
                "involvement": "相遇"}]}]
    return {"result_kind": "ready", "payload": payload, "questions": [],
            "evidence_refs": [source_ref], "notes": []}


def _knowledge_asset(global_view, global_analysis, character_view):
    return {"result_kind": "ready", "payload": {
        "source_global_events_ref": ref(global_view), "source_global_analysis_ref": ref(global_analysis),
        "source_character_events_ref": ref(character_view), "premise": "甲见乙。",
        "world_rules": [], "themes": [], "conflicts": [], "characters": [], "key_event_refs": [],
        "preservation_items": [], "event_causality": [], "canon_constraints": [], "uncertainties": [],
    }, "questions": [], "evidence_refs": [], "notes": []}


def test_step1_schema_and_workflow_use_three_independent_artifacts(source_case, monkeypatch):
    store, workflow, project = source_case
    catalog = SchemaCatalog()
    assert STAGE_OUTPUTS[1] == ("source_global_events", "source_global_analysis", "source_character_events")
    with store.transaction():
        original = workflow.save(project, "source_text", "甲见乙。", effective=True)
        source_ref = ref(original)
        outputs = {kind: _output(kind, source_ref) for kind in (
            "source_global_events", "source_character_events")}
        for kind, output in outputs.items():
            catalog.validate(kind, output)
        global_view = workflow.save(project, "source_global_events", outputs["source_global_events"],
                                    stage=1, inputs=[source_ref], effective=True)
        character_view = workflow.save(project, "source_character_events", outputs["source_character_events"],
                                       stage=1, inputs=[source_ref], effective=True)
        global_analysis = workflow.save(project, "source_global_analysis", "全局分析", stage=1,
            inputs=[source_ref, ref(global_view)], effective=True)
        asset = workflow.save(project, "source_knowledge_asset",
            _knowledge_asset(global_view, global_analysis, character_view), stage=2,
            inputs=[ref(global_view), ref(global_analysis), ref(character_view)], effective=True)
    assert workflow.original_for(project, global_view)["id"] == original["id"]
    assert workflow.original_for(project, character_view)["id"] == original["id"]
    assert [item["kind"] for item in workflow.materials(project, 2)] == list(STAGE_OUTPUTS[1])
    monkeypatch.setitem(PROGRAM_REQUIRES, 5, [])  # Isolate source-view input contract from the plan guard.
    assert [item["kind"] for item in workflow.materials(project, 5)] == [
        "source_global_events", "source_character_events", "source_knowledge_asset"]
    assert workflow.materials(project, 3)[0]["ref"] == ref(asset)
    with store.transaction():
        revised = deepcopy(outputs["source_global_events"])
        revised["payload"]["global_events"][0]["summary"] = "再分析相遇。"
        global_v2 = workflow.save(project, "source_global_events", revised, stage=1,
                                  inputs=[source_ref], effective=True)
    assert global_v2["version"] == 2
    assert workflow.resolve(project, "source_global_events")["id"] == global_v2["id"]
    assert workflow.resolve(project, "source_character_events")["id"] == character_view["id"]
    assert body(store, character_view) == outputs["source_character_events"]


def test_step5_accepts_character_events_without_event_anchors(source_case, monkeypatch):
    store, workflow, project = source_case
    monkeypatch.setitem(PROGRAM_REQUIRES, 5, [])
    with store.transaction():
        original = workflow.save(project, "source_text", "甲见乙。", effective=True)
        source_ref = ref(original)
        global_view = workflow.save(project, "source_global_events", _output("source_global_events", source_ref),
                                    stage=1, inputs=[source_ref], effective=True)
        character_view = workflow.save(project, "source_character_events", _output("source_character_events", source_ref),
                                       stage=1, inputs=[source_ref], effective=True)
        global_analysis = workflow.save(project, "source_global_analysis", "全局分析", stage=1,
                                        inputs=[source_ref, ref(global_view)], effective=True)
        workflow.save(project, "source_knowledge_asset", _knowledge_asset(global_view, global_analysis, character_view),
                      stage=2, inputs=[ref(global_view), ref(global_analysis), ref(character_view)], effective=True)
    assert [item["kind"] for item in workflow.materials(project, 5)] == [
        "source_global_events", "source_character_events", "source_knowledge_asset"]


def test_step2_rejects_views_from_different_original_versions(source_case):
    store, workflow, project = source_case
    with store.transaction():
        first = workflow.save(project, "source_text", "甲见乙。", effective=True)
        second_artifact = store.put(new_record("artifact", project, artifact_kind="source_text",
            scope=scope(description="second original"), latest_version=0, current_effective_version=None))
        second = workflow.save(project, "source_text", "甲见乙。", artifact_id=second_artifact["id"], effective=True)
        workflow.save(project, "source_global_events", _output("source_global_events", ref(first)),
                      stage=1, inputs=[ref(first)], effective=True)
        workflow.save(project, "source_character_events", _output("source_character_events", ref(second)),
                      stage=1, inputs=[ref(second)], effective=True)
    with pytest.raises(WorkflowBlocked) as error:
        workflow.materials(project, 2)
    assert error.value.reason == "source_reference_mismatch"
