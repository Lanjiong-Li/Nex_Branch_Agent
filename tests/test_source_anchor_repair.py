"""Ambiguous source quotes use content repair against the fixed original."""

import asyncio
from copy import deepcopy

import pytest

from branch_agent.workflow import WorkflowBlocked, all_records, locate_source_anchors, ref
from test_runtime import runtime, source_response


@pytest.mark.parametrize("quote, expected_count, starts", [
    ("甲见乙。", 2, [2, 6]),
    ("不存在", 0, []),
])
def test_ambiguous_source_anchor_reports_bounded_utf16_diagnostics(quote, expected_count, starts):
    source = "😀甲见乙。甲见乙。"
    source_ref = {"record_id": "source", "version": "1"}
    output = source_response(None, [{"schema_id": "source_text", "ref": source_ref,
                                     "content": source}])
    output["payload"]["global_events"][0]["source_anchors"][0] = {
        "source_ref": source_ref, "start_utf16": None, "end_utf16": None,
        "exact_quote": quote, "prefix": None, "suffix": None,
    }

    with pytest.raises(WorkflowBlocked) as blocked:
        locate_source_anchors(output, source, source_ref)

    assert blocked.value.reason == "source_anchor_ambiguous"
    problem = blocked.value.details["validation_errors"][0]
    assert problem["path"] == "/payload/global_events/0/source_anchors/0"
    assert problem["validator"] == "source_anchor_unique_match"
    assert problem["category"] == "content"
    assert problem["match_count"] == expected_count
    assert problem["candidate_start_utf16"] == starts
    assert source not in str(problem)


@pytest.mark.parametrize("bad_quote", ["甲见乙。", "不存在"])
def test_step1_ambiguous_anchor_retries_content_in_fresh_session(runtime, bad_quote):
    engine, model, project, conversation = runtime
    source = "😀甲见乙。甲见乙。"
    engine.import_source(project, conversation, source, "故事")
    with engine.store.transaction():
        message = engine._message(project, conversation, "分析原作", role="user")
        task = engine._new_task(project, conversation, message, "generate", stage=1)

    global_attempts = 0

    def response(subtask, materials):
        nonlocal global_attempts
        output = source_response(subtask, materials)
        if engine._task_data(subtask)["step1_view"] == "global":
            global_attempts += 1
            if global_attempts == 1:
                anchor = deepcopy(output["payload"]["global_events"][0]["source_anchors"][0])
                anchor["exact_quote"] = bad_quote
                output["payload"]["global_events"][0]["source_anchors"] = [anchor]
        return output

    model.responses = [response, response, response]
    asyncio.run(engine.tick(project, conversation))

    assert global_attempts == 2
    assert engine.store.get(task["id"], project)["state"] == "succeeded"
    children = all_records(engine.store, project, "task", parent_task_id=task["id"])
    global_child = next(child for child in children
                        if engine._task_data(child)["step1_view"] == "global")
    global_runs = all_records(engine.store, project, "run", task_id=global_child["id"])
    assert len(global_runs) == 2
    assert len({run["session_id"] for run in global_runs}) == 2
    data = engine._task_data(global_child)
    assert data["content_repair_rounds_used"] == 1
    assert data["repair_brief"]["error_code"] == "source_anchor_ambiguous"
    assert data["repair_brief"]["errors"][0]["path"] == "/payload/global_events/0/source_anchors/0"
    assert data["repair_brief"]["errors"][0]["match_count"] == (2 if bad_quote == "甲见乙。" else 0)
    assert data["repair_brief"]["candidate_ref"]
    events = all_records(engine.store, project, "runtime_event", event_name="repair.scheduled")
    assert len(events) == 1 and events[0]["payload"]["category"] == "content"
    saved = engine.workflow.resolve(project, "source_global_events")
    assert saved["content"]["value"]["payload"]["source_ref"] == ref(
        engine.workflow.resolve(project, "source_text"))
