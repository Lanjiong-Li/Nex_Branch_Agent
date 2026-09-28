import pytest

from branch_agent.context import tokens
from branch_agent.source_windows import source_window, validate_window, combine_view_windows
from branch_agent.workflow import WorkflowBlocked


SOURCE_REF = {"record_id": "source-id", "version": "1", "item_id": None, "json_pointer": None}


def anchor(start, end):
    return {"source_ref": dict(SOURCE_REF), "start_utf16": start, "end_utf16": end,
            "exact_quote": None, "prefix": None, "suffix": None}


def window_output(view, start, commit, window_end):
    event = {"title": "事件", "summary": "事件摘要", "narrative_order": 1,
             "story_time": None, "source_anchors": [anchor(start, commit)]}
    global_events = [{**event, "event_id": "local-1", "character_ids": ["CHAR-甲"],
                      "analysis": f"事件分析：已核对原文 {start}–{commit}"}] if view == "global" else []
    character_views = ([{"character_id": "CHAR-甲", "name": "甲", "aliases": [],
                         "description": "主要人物", "events": [{
                         "character_event_id": "local-1", "title": event["title"],
                         "summary": event["summary"], "narrative_order": event["narrative_order"],
                         "story_time": event["story_time"], "involvement": "亲历"}]}] if view == "character" else [])
    return {"result_kind": "ready", "payload": {"source_ref": dict(SOURCE_REF),
            "global_events": global_events, "character_views": character_views,
            "covered_source_anchors": [anchor(start, commit)],
            "remaining_source_anchors": [] if commit == window_end else [anchor(commit, window_end)]},
            "questions": [], "evidence_refs": [], "notes": []}


def separate_window_output(view, start, commit, window_end):
    output = window_output(view, start, commit, window_end)
    output["payload"].pop("character_views" if view == "global" else "global_events")
    return output


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
    global_result = combine_view_windows(validated, "global", SOURCE_REF, 5)
    character_result = combine_view_windows(validated, "character", SOURCE_REF, 5)
    assert [event["event_id"] for event in global_result["payload"]["global_events"]] == ["GEV-00001", "GEV-00002"]
    assert [event["analysis"] for event in global_result["payload"]["global_events"]] == [
        "事件分析：已核对原文 0–3", "事件分析：已核对原文 3–5"]
    assert len(character_result["payload"]["character_views"]) == 1
    assert len(character_result["payload"]["character_views"][0]["events"]) == 2
    assert "source_anchors" not in character_result["payload"]["character_views"][0]["events"][0]
    assert global_result["payload"]["remaining_source_anchors"] == []


def test_window_refuses_to_advance_without_completed_event_or_second_pass():
    source = "甲😀乙丙"
    empty = window_output("global", 0, 3, 5)
    empty["payload"]["global_events"] = []
    with pytest.raises(WorkflowBlocked) as error:
        validate_window(empty, source, SOURCE_REF, "global", 0, 5)
    assert error.value.reason == "source_window_no_complete_event"
    first, _ = validate_window(window_output("global", 0, 3, 5), source, SOURCE_REF, "global", 0, 5)
    with pytest.raises(WorkflowBlocked) as error:
        combine_view_windows([{"pass": "global", "start_utf16": 0,
                              "commit_utf16": 3, "output": first}], "global", SOURCE_REF, 5)
    assert error.value.reason == "source_coverage_incomplete"


def test_character_pass_can_commit_an_explicit_empty_interval_without_global_boundaries():
    source = "甲😀乙丙"
    empty = window_output("character", 0, 3, 5)
    empty["payload"]["character_views"] = []
    empty["notes"] = ["空人物事件区间：已阅读该前缀，没有完整人物事件。"]
    validated, commit = validate_window(empty, source, SOURCE_REF, "character", 0, 5)
    assert commit == 3
    assert validated["payload"]["character_views"] == []
    assert "analysis" not in validated


def test_character_pass_may_finish_a_window_without_any_character_event_boundary():
    source = "甲😀乙丙"
    character = separate_window_output("character", 0, 5, 5)
    character["payload"]["character_views"] = []
    character["notes"] = ["空人物事件区间：已阅读该窗口，没有完整人物事件。"]
    validated, commit = validate_window(character, source, SOURCE_REF, "character", 0, 5)
    assert commit == 5
    assert validated["payload"]["remaining_source_anchors"] == []
    assert combine_view_windows([{"pass": "character", "start_utf16": 0,
                                  "commit_utf16": commit, "output": validated}],
                                "character", SOURCE_REF, 5)["payload"]["character_views"] == []

    global_output = separate_window_output("global", 0, 5, 5)
    global_output["payload"]["global_events"] = []
    with pytest.raises(WorkflowBlocked, match="source_window_no_complete_event"):
        validate_window(global_output, source, SOURCE_REF, "global", 0, 5)


def test_empty_character_interval_must_have_a_contiguous_right_remainder():
    source = "甲😀乙丙"
    empty = window_output("character", 0, 3, 5)
    empty["payload"]["character_views"] = []
    empty["notes"] = ["空人物事件区间：已阅读该前缀，没有完整人物事件。"]
    empty["payload"]["remaining_source_anchors"] = [anchor(4, 5)]
    with pytest.raises(WorkflowBlocked) as error:
        validate_window(empty, source, SOURCE_REF, "character", 0, 5)
    assert error.value.reason == "source_window_coverage_invalid"


def test_empty_character_interval_cannot_claim_text_beyond_the_window():
    source = "甲😀乙丙"
    empty = window_output("character", 0, 5, 5)
    empty["payload"]["character_views"] = []
    empty["notes"] = ["空人物事件区间：已阅读该窗口，没有完整人物事件。"]
    with pytest.raises(WorkflowBlocked) as error:
        validate_window(empty, source, SOURCE_REF, "character", 0, 4)
    assert error.value.reason == "source_window_coverage_invalid"


def test_each_view_can_be_combined_and_saved_independently():
    source = "甲😀乙丙"
    first_global, _ = validate_window(window_output("global", 0, 3, 5), source, SOURCE_REF, "global", 0, 5)
    second_global, _ = validate_window(window_output("global", 3, 5, 5), source, SOURCE_REF, "global", 3, 5)
    empty_character = window_output("character", 0, 3, 5)
    empty_character["payload"]["character_views"] = []
    empty_character["notes"] = ["空人物事件区间：已阅读该前缀，没有完整人物事件。"]
    first_character, _ = validate_window(empty_character, source, SOURCE_REF, "character", 0, 5)
    second_character, _ = validate_window(window_output("character", 3, 5, 5), source, SOURCE_REF,
                                          "character", 3, 5)
    windows = [
        {"pass": "global", "start_utf16": 0, "commit_utf16": 3, "output": first_global},
        {"pass": "character", "start_utf16": 0, "commit_utf16": 3, "output": first_character},
        {"pass": "character", "start_utf16": 3, "commit_utf16": 5, "output": second_character},
        {"pass": "global", "start_utf16": 3, "commit_utf16": 5, "output": second_global},
    ]
    global_result = combine_view_windows(windows, "global", SOURCE_REF, 5)
    character_result = combine_view_windows(windows, "character", SOURCE_REF, 5)
    assert len(global_result["payload"]["global_events"]) == 2
    assert "character_views" not in global_result["payload"]
    assert "global_events" not in character_result["payload"]
    assert len(character_result["payload"]["character_views"][0]["events"]) == 1
    assert character_result["payload"]["character_views"][0]["events"][0]["character_event_id"] == "CEV-00001"
    # Each event retains its own analysis; there is no top-level analysis.
    assert "analysis" not in global_result
    assert all(event["analysis"] for event in global_result["payload"]["global_events"])
    assert "analysis" not in character_result


def test_character_identity_must_remain_stable_across_windows():
    source = "甲😀乙丙"
    first, _ = validate_window(separate_window_output("character", 0, 3, 5),
                               source, SOURCE_REF, "character", 0, 5)
    second, _ = validate_window(separate_window_output("character", 3, 5, 5),
                                source, SOURCE_REF, "character", 3, 5)
    second["payload"]["character_views"][0]["character_id"] = "CHAR-另一人"
    with pytest.raises(WorkflowBlocked, match="source_character_id_conflict"):
        combine_view_windows([
            {"pass": "character", "start_utf16": 0, "commit_utf16": 3, "output": first},
            {"pass": "character", "start_utf16": 3, "commit_utf16": 5, "output": second},
        ], "character", SOURCE_REF, 5)


@pytest.mark.parametrize("view,only_field,absent_field", [
    ("global", "global_events", "character_views"),
    ("character", "character_views", "global_events"),
])
def test_separate_view_schema_validates_and_combines_without_other_view_field(
        view, only_field, absent_field):
    source = "甲😀乙丙"
    first, commit = validate_window(separate_window_output(view, 0, 3, 5),
                                    source, SOURCE_REF, view, 0, 5)
    second, final_commit = validate_window(separate_window_output(view, 3, 5, 5),
                                           source, SOURCE_REF, view, 3, 5)
    combined = combine_view_windows([
        {"pass": view, "start_utf16": 0, "commit_utf16": commit, "output": first},
        {"pass": view, "start_utf16": 3, "commit_utf16": final_commit, "output": second},
    ], view, SOURCE_REF, 5)
    assert only_field in combined["payload"]
    assert absent_field not in combined["payload"]
    assert len(combined["payload"][only_field]) > 0
