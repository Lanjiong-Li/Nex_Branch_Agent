import pytest

from branch_agent.context import tokens
from branch_agent.source_windows import source_window, validate_window, combine_windows
from branch_agent.workflow import WorkflowBlocked


SOURCE_REF = {"record_id": "source-id", "version": "1", "item_id": None, "json_pointer": None}


def anchor(start, end):
    return {"source_ref": dict(SOURCE_REF), "start_utf16": start, "end_utf16": end,
            "exact_quote": None, "prefix": None, "suffix": None}


def window_output(view, start, commit, window_end):
    event = {"title": "事件", "summary": "事件摘要", "narrative_order": 1,
             "story_time": None, "source_anchors": [anchor(start, commit)]}
    global_events = [{**event, "event_id": "local-1", "character_ids": ["CHAR-甲"]}] if view == "global" else []
    character_views = ([{"character_id": "CHAR-甲", "name": "甲", "aliases": [],
                         "description": "主要人物", "events": [{**event, "character_event_id": "local-1",
                         "involvement": "亲历"}]}] if view == "character" else [])
    return {"result_kind": "ready", "payload": {"source_ref": dict(SOURCE_REF),
            "global_events": global_events, "character_views": character_views,
            "covered_source_anchors": [anchor(start, commit)],
            "remaining_source_anchors": [] if commit == window_end else [anchor(commit, window_end)]},
            "questions": [], "evidence_refs": [], "notes": []}


def test_window_uses_utf16_boundary_and_restarts_at_committed_event():
    source = "甲😀乙丙"
    cap = tokens("甲😀乙", "deepseek-flash")
    first = source_window(source, 0, cap, "deepseek-flash")
    assert first["text"] == "甲😀乙"
    assert first["end_utf16"] == 4
    second = source_window(source, 3, cap, "deepseek-flash")
    assert second["text"] == "乙丙"
    assert second["start_utf16"] == 3
    assert second["end_utf16"] == 5


def test_independent_passes_only_advance_through_complete_events():
    source = "甲😀乙丙"
    validated = []
    for view in ("global", "character"):
        first, end = validate_window(window_output(view, 0, 3, 5), source, SOURCE_REF, view, 0, 5)
        assert end == 3
        assert first["payload"]["remaining_source_anchors"] == [anchor(3, 5)]
        second, end = validate_window(window_output(view, 3, 5, 5), source, SOURCE_REF, view, 3, 5)
        assert end == 5
        validated += [{"pass": view, "start_utf16": 0, "commit_utf16": 3, "output": first},
                      {"pass": view, "start_utf16": 3, "commit_utf16": 5, "output": second}]
    result = combine_windows(validated, SOURCE_REF, 5)
    assert [event["event_id"] for event in result["payload"]["global_events"]] == ["GEV-00001", "GEV-00002"]
    assert len(result["payload"]["character_views"]) == 1
    assert len(result["payload"]["character_views"][0]["events"]) == 2
    assert result["payload"]["remaining_source_anchors"] == []


def test_window_refuses_to_advance_without_completed_event_or_second_pass():
    source = "甲😀乙丙"
    empty = window_output("global", 0, 3, 5)
    empty["payload"]["global_events"] = []
    with pytest.raises(WorkflowBlocked) as error:
        validate_window(empty, source, SOURCE_REF, "global", 0, 5)
    assert error.value.reason == "source_window_no_complete_event"
    first, _ = validate_window(window_output("global", 0, 3, 5), source, SOURCE_REF, "global", 0, 5)
    with pytest.raises(WorkflowBlocked) as error:
        combine_windows([{"pass": "global", "start_utf16": 0,
                          "commit_utf16": 3, "output": first}], SOURCE_REF, 5)
    assert error.value.reason == "source_coverage_incomplete"


def test_character_pass_can_cover_a_span_without_major_character_event_at_verified_global_boundary():
    source = "甲😀乙丙"
    empty = window_output("character", 0, 3, 5)
    empty["payload"]["character_views"] = []
    validated, commit = validate_window(empty, source, SOURCE_REF, "character", 0, 5,
                                        empty_boundaries={3})
    assert commit == 3
    assert validated["payload"]["character_views"] == []
    with pytest.raises(WorkflowBlocked):
        validate_window(empty, source, SOURCE_REF, "character", 0, 5,
                        empty_boundaries={4})
