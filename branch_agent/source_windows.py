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


def validate_window(output, source, source_ref, pass_name, start, end, empty_boundaries=()):
    """Only a complete event boundary may advance a pass's cursor."""
    if output.get("result_kind") != "ready" or output.get("payload") is None or output.get("questions"):
        raise WorkflowBlocked("source_window_incomplete")
    value = locate_source_anchors(output, source, source_ref, full_coverage=False)
    payload = value["payload"]
    if pass_name == "global":
        if payload["character_views"]:
            raise WorkflowBlocked("source_window_wrong_view")
        events = payload["global_events"]
    else:
        if payload["global_events"]:
            raise WorkflowBlocked("source_window_wrong_view")
        events = [event for view in payload["character_views"] for event in view["events"]]
    covered = payload["covered_source_anchors"]
    if len(covered) != 1 or covered[0]["start_utf16"] != start:
        raise WorkflowBlocked("source_window_coverage_invalid", {"expected_start_utf16": start})
    commit = covered[0]["end_utf16"]
    if not start < commit <= end:
        raise WorkflowBlocked("source_window_coverage_invalid", {"window_end_utf16": end})
    if not events and not (pass_name == "character" and commit in empty_boundaries):
        raise WorkflowBlocked("source_window_no_complete_event",
                              {"view": pass_name, "start_utf16": start, "end_utf16": end})
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
    payload["remaining_source_anchors"] = [] if commit == end else [_anchor(source_ref, commit, end)]
    return value, commit


def combine_windows(windows, source_ref, source_length):
    """Merge validated windows; each pass must independently cover the source."""
    result = {"result_kind": "ready", "payload": {"source_ref": deepcopy(source_ref),
              "global_events": [], "character_views": [],
              "covered_source_anchors": [_anchor(source_ref, 0, source_length)],
              "remaining_source_anchors": []}, "questions": [],
              "evidence_refs": [deepcopy(source_ref)], "notes": []}
    for pass_name in ("global", "character"):
        cursor = 0
        for item in (item for item in windows if item["pass"] == pass_name):
            if item["start_utf16"] != cursor:
                raise WorkflowBlocked("source_coverage_incomplete", {"view": pass_name})
            cursor = item["commit_utf16"]
            payload = item["output"]["payload"]
            if pass_name == "global":
                result["payload"]["global_events"].extend(deepcopy(payload["global_events"]))
            else:
                for view in payload["character_views"]:
                    existing = next((candidate for candidate in result["payload"]["character_views"]
                                     if candidate["character_id"] == view["character_id"] or
                                     candidate["name"] == view["name"]), None)
                    if existing is None:
                        result["payload"]["character_views"].append(deepcopy(view))
                    elif existing["name"] == view["name"]:
                        existing["events"].extend(deepcopy(view["events"]))
                        existing["aliases"] = list(dict.fromkeys(existing["aliases"] + view["aliases"]))
                    else:
                        raise WorkflowBlocked("source_character_id_conflict", {"character_id": view["character_id"]})
        if cursor != source_length:
            raise WorkflowBlocked("source_coverage_incomplete", {"view": pass_name})
    character_ids = [view["character_id"] for view in result["payload"]["character_views"]]
    if len(character_ids) != len(set(character_ids)):
        raise WorkflowBlocked("source_window_duplicate_id")
    # Local window numbering is not a global identity. Assign stable sequence
    # numbers only after both independent coverage passes have completed.
    for index, event in enumerate(result["payload"]["global_events"], 1):
        event["event_id"] = f"GEV-{index:05d}"
        event["narrative_order"] = index
    index = 0
    for view in result["payload"]["character_views"]:
        for event in view["events"]:
            index += 1
            event["character_event_id"] = f"CEV-{index:05d}"
            event["narrative_order"] = index
    return result
