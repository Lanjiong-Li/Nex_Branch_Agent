"""Deterministic UTF-16 source windows and independently checked view progress."""
from __future__ import annotations

from copy import deepcopy

from .context import tokens, utf16_length
from .workflow import WorkflowBlocked, locate_source_anchors


def source_window(source: str, start_utf16: int, limit_tokens: int, model: str):
    raw = source.encode("utf-16-le")
    if not 0 <= start_utf16 < len(raw) // 2:
        raise WorkflowBlocked("source_window_invalid", {"start_utf16": start_utf16})
    tail = raw[start_utf16 * 2:].decode("utf-16-le")
    if tokens(tail, model) <= limit_tokens:
        return {"text": tail, "start_utf16": start_utf16, "end_utf16": len(raw) // 2}
    low, high = 1, len(tail)
    while low < high:
        middle = (low + high + 1) // 2
        if tokens(tail[:middle], model) <= limit_tokens:
            low = middle
        else:
            high = middle - 1
    text = tail[:low]
    if not text or tokens(text, model) > limit_tokens:
        raise WorkflowBlocked("source_window_budget", {"window_tokens": limit_tokens})
    return {"text": text, "start_utf16": start_utf16,
            "end_utf16": start_utf16 + utf16_length(text)}


def _anchor(source_ref, start, end):
    return {"source_ref": deepcopy(source_ref), "start_utf16": start, "end_utf16": end,
            "exact_quote": None, "prefix": None, "suffix": None}


def validate_global_event_analysis(events):
    """Require a substantive analysis on every completed global event."""
    for index, event in enumerate(events):
        value = event.get("analysis") if isinstance(event, dict) else None
        if not isinstance(value, str) or not value.strip():
            raise WorkflowBlocked("source_event_analysis_incomplete", {"event_index": index})


def validate_window(output, source, source_ref, pass_name, start, end):
    """Validate one view's independently committed prefix of a source window.

    A character pass certifies what it has read, whether or not a character
    event ends at that position. It never borrows the global pass's event
    boundaries.
    """
    if pass_name not in ("global", "character"):
        raise WorkflowBlocked("source_window_wrong_view")
    if not 0 <= start < end <= utf16_length(source):
        raise WorkflowBlocked("source_window_coverage_invalid", {"start_utf16": start, "window_end_utf16": end})
    if output.get("result_kind") != "ready" or output.get("payload") is None or output.get("questions"):
        raise WorkflowBlocked("source_window_incomplete")
    value = locate_source_anchors(output, source, source_ref, full_coverage=False)
    payload = value["payload"]
    if pass_name == "global":
        if payload.get("character_views"):
            raise WorkflowBlocked("source_window_wrong_view")
        events = payload["global_events"]
        validate_global_event_analysis(events)
    else:
        if payload.get("global_events"):
            raise WorkflowBlocked("source_window_wrong_view")
        events = [event for view in payload["character_views"] for event in view["events"]]
    covered = payload["covered_source_anchors"]
    if len(covered) != 1 or covered[0]["start_utf16"] != start:
        raise WorkflowBlocked("source_window_coverage_invalid", {"expected_start_utf16": start})
    commit = covered[0]["end_utf16"]
    if not start < commit <= end:
        raise WorkflowBlocked("source_window_coverage_invalid", {"window_end_utf16": end})
    remaining = payload["remaining_source_anchors"]
    expected_remaining = [] if commit == end else [(commit, end)]
    actual_remaining = [(anchor["start_utf16"], anchor["end_utf16"]) for anchor in remaining]
    if actual_remaining != expected_remaining:
        raise WorkflowBlocked("source_window_coverage_invalid",
                              {"view": pass_name, "commit_utf16": commit, "window_end_utf16": end})
    if not events and pass_name == "global":
        raise WorkflowBlocked("source_window_no_complete_event",
                              {"view": pass_name, "start_utf16": start, "end_utf16": end})
    if pass_name == "global":
        terminal = 0
        for event in events:
            if not event["source_anchors"]:
                raise WorkflowBlocked("source_anchor_invalid", {"view": pass_name})
            for anchor in event["source_anchors"]:
                a, b = anchor["start_utf16"], anchor["end_utf16"]
                if not start <= a < b <= commit:
                    raise WorkflowBlocked("source_window_event_outside_commit", {"view": pass_name})
                terminal = max(terminal, b)
        if events and terminal != commit:
            raise WorkflowBlocked("source_window_boundary_invalid",
                                  {"view": pass_name, "last_event_end_utf16": terminal, "commit_utf16": commit})
    if pass_name == "character" and not events and not any(
            isinstance(note, str) and note.startswith("空人物事件区间：") and note.removeprefix("空人物事件区间：").strip()
            for note in value.get("notes", [])):
        raise WorkflowBlocked("source_window_empty_interval_unverified",
                              {"view": pass_name, "start_utf16": start, "commit_utf16": commit})
    payload["remaining_source_anchors"] = [] if commit == end else [_anchor(source_ref, commit, end)]
    return value, commit


def _blank_result(source_ref, source_length, pass_name):
    result = {"result_kind": "ready", "payload": {"source_ref": deepcopy(source_ref),
              "covered_source_anchors": [_anchor(source_ref, 0, source_length)],
              "remaining_source_anchors": []}, "questions": [],
              "evidence_refs": [deepcopy(source_ref)], "notes": []}
    result["payload"]["global_events" if pass_name == "global" else "character_views"] = []
    return result


def combine_view_windows(windows, pass_name, source_ref, source_length):
    """Assemble one independently complete view for its own saved artifact."""
    if pass_name not in ("global", "character"):
        raise WorkflowBlocked("source_window_wrong_view")
    result = _blank_result(source_ref, source_length, pass_name)
    cursor = 0
    for item in (item for item in windows if item["pass"] == pass_name):
        if item["start_utf16"] != cursor or not cursor < item["commit_utf16"] <= source_length:
            raise WorkflowBlocked("source_coverage_incomplete", {"view": pass_name})
        payload = item["output"]["payload"]
        covered = payload["covered_source_anchors"]
        if (len(covered) != 1 or covered[0]["start_utf16"] != cursor or
                covered[0]["end_utf16"] != item["commit_utf16"]):
            raise WorkflowBlocked("source_coverage_incomplete", {"view": pass_name})
        cursor = item["commit_utf16"]
        if pass_name == "global":
            if payload.get("character_views"):
                raise WorkflowBlocked("source_window_wrong_view")
            validate_global_event_analysis(payload["global_events"])
            result["payload"]["global_events"].extend(deepcopy(payload["global_events"]))
        else:
            if payload.get("global_events"):
                raise WorkflowBlocked("source_window_wrong_view")
            for view in payload["character_views"]:
                existing = next((candidate for candidate in result["payload"]["character_views"]
                                 if candidate["character_id"] == view["character_id"] or
                                 candidate["name"] == view["name"]), None)
                if existing is None:
                    result["payload"]["character_views"].append(deepcopy(view))
                elif existing["name"] == view["name"] and existing["character_id"] == view["character_id"]:
                    existing["events"].extend(deepcopy(view["events"]))
                    existing["aliases"] = list(dict.fromkeys(existing["aliases"] + view["aliases"]))
                else:
                    raise WorkflowBlocked("source_character_id_conflict", {"character_id": view["character_id"]})
    if cursor != source_length:
        raise WorkflowBlocked("source_coverage_incomplete", {"view": pass_name})
    character_ids = [view["character_id"] for view in result["payload"].get("character_views", [])]
    if len(character_ids) != len(set(character_ids)):
        raise WorkflowBlocked("source_window_duplicate_id")
    # Local window numbering is not a global identity. Assign stable sequence
    # numbers only after this view has completely covered the source.
    for index, event in enumerate(result["payload"].get("global_events", []), 1):
        event["event_id"] = f"GEV-{index:05d}"
        event["narrative_order"] = index
    index = 0
    for view in result["payload"].get("character_views", []):
        for event in view["events"]:
            index += 1
            event["character_event_id"] = f"CEV-{index:05d}"
            event["narrative_order"] = index
    return result
