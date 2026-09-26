"""Run the two original-source event views as independent Step 1 producers."""
from __future__ import annotations

import asyncio
from copy import deepcopy
import json

from .context import BudgetExceeded, build_materials, input_budget, prepare_runtime_materials, prune_optional_materials, tokens, utf16_length
from .presentation import stage_result_text
from .prompts import harness_prompts, instructions, step1_agent, step1_run_appendix
from .source_windows import combine_view_windows, source_window, validate_window
from .workflow import WorkflowBlocked, all_records, body, locate_source_anchors, ref, update


VIEW_KINDS = {"global": "source_global_events", "character": "source_character_events"}
ANALYSIS_KINDS = {"global": "source_global_analysis"}
VIEW_LABELS = {"global": "作品事件", "character": "主要人物事件"}


class _WindowPreviewComplete(Exception):
    """Carry a token estimate out of a transaction whose projections must roll back."""

    def __init__(self, count):
        self.count = count


async def _budgeted_window(engine, child, run, session, values, materials, source_text,
                           cursor, view, previous_analysis, previous_characters, request):
    """Fit the real Step1 envelope, including its index and prior analysis.

    Preview projections are rolled back. The model service rebuilds them once
    for the chosen window, so sizing never leaves phantom source-index records.
    """
    from .configuration import MODELS
    from .model_service import PersistentSession, ReadTools
    from .schemas import SchemaCatalog

    model = values["model"]["name"]
    configured_cap = values["context"]["step1_source"]["window_tokens"]
    budget = input_budget(values)
    headroom = min(8192, max(256, budget // 20), max(1, budget // 4))
    target = budget - headroom
    catalog = SchemaCatalog()
    schema_id = catalog.schema_for(f"step1.{view}", values)
    provider = MODELS[model].get("provider", "openai")
    output_type = catalog.output_type(schema_id, values.get("schemas"), strict=provider != "deepseek")
    output_schema = output_type.json_schema()
    tool_defs = [{"type": "function", "name": tool.name, "description": tool.description,
                  "parameters": tool.params_json_schema}
                 for tool in ReadTools(engine.store, child["project_id"], values).functions()]
    history = await PersistentSession(engine.store, session, child, run).get_items()
    message = request + f"\n当前视图：{VIEW_LABELS[view]}"
    if previous_analysis:
        message += ("\n上一已检查原文前缀的累计分析（须结合本窗口更新，不得丢失仍有效的事实）：\n"
                    + previous_analysis)
    if previous_characters:
        message += "\n已阅读前缀中的人物身份与最近事件（沿用人物 ID；不要重复输出已完成事件）：\n" + previous_characters

    limit = configured_cap
    while True:
        window = source_window(source_text, cursor, limit, model)
        window["view"] = view
        prompt = instructions("step1", values) + "\n\n" + step1_run_appendix(
            view, "window", values, cursor, window["end_utf16"])
        prompt += "\n\n" + harness_prompts(values)["no_ask_user"]
        try:
            with engine.store.transaction():
                prepared = prepare_runtime_materials("step1", child, run, session, values,
                                                     materials, engine.store, step1_window=window)
                packed, _, selections = build_materials("step1", prepared, values,
                                                         engine.store, child["project_id"])
                envelope = {"request": message, "task_id": child["id"],
                            "source_message_id": child["requested_by_message_id"],
                            "_harness_materials": [], "task_scope": child["scope"]}
                fixed = tokens({"instructions": prompt,
                    "input": history + [{"role": "user", "content": json.dumps(envelope, ensure_ascii=False)}],
                    "tools": tool_defs, "output_schema": output_schema}, model)
                packed, selections = prune_optional_materials(
                    packed, selections, max(0, target - fixed), values)
                envelope["_harness_materials"] = packed
                count = tokens({"instructions": prompt,
                    "input": history + [{"role": "user", "content": json.dumps(envelope, ensure_ascii=False)}],
                    "tools": tool_defs, "output_schema": output_schema}, model)
                raise _WindowPreviewComplete(count)
        except _WindowPreviewComplete as preview:
            count = preview.count
        if count <= target:
            return window
        if limit <= 1:
            raise BudgetExceeded(details={"input_tokens": count, "input_budget": target,
                                          "window_tokens": configured_cap,
                                          "reason": "Step1 fixed context cannot fit even one source token"})
        # Source text and its per-line index both shrink with the window.
        # Keep a little extra room for tokenizer and SDK envelope variation.
        limit = max(1, min(limit - 1, limit - max(1, (count - target) * 5 // 4)))


def _analysis(output, view):
    value = output.get("analysis") if isinstance(output, dict) else None
    if not isinstance(value, str) or not value.strip():
        raise WorkflowBlocked("source_analysis_incomplete", {"view": view})
    return value.strip()


def _view_only(output):
    result = deepcopy(output)
    result.pop("analysis", None)
    return result


def _character_continuity(engine, project, windows):
    """Carry compact identity and boundary context across separate character Sessions."""
    known = {}
    for window in windows:
        output = engine._projection(project, "source_window_output", window["run_id"])["output"]
        for character in output["payload"]["character_views"]:
            item = known.setdefault(character["character_id"], {
                "character_id": character["character_id"], "name": character["name"],
                "aliases": [], "recent_events": [],
            })
            item["aliases"] = list(dict.fromkeys(item["aliases"] + character["aliases"]))
            item["recent_events"] = (item["recent_events"] + [
                {"title": event["title"][:100], "summary": event["summary"][:180]}
                for event in character["events"]])[-2:]
    return json.dumps(list(known.values()), ensure_ascii=False) if known else None


def _validated_full(output, source_text, source_ref, view):
    if view == "global":
        _analysis(output, view)
    if output.get("result_kind") != "ready" or output.get("payload") is None or output.get("questions"):
        raise WorkflowBlocked("source_view_incomplete", {"view": view})
    result = locate_source_anchors(output, source_text, source_ref, full_coverage=True)
    payload = result["payload"]
    if payload["source_ref"] != source_ref:
        raise WorkflowBlocked("source_reference_mismatch", {"view": view})
    wrong = "character_views" if view == "global" else "global_events"
    if payload.get(wrong):
        raise WorkflowBlocked("source_window_wrong_view", {"view": view})
    events = payload["global_events"] if view == "global" else [
        event for character in payload["character_views"] for event in character["events"]]
    if view == "global" and not events:
        raise WorkflowBlocked("source_window_no_complete_event", {"view": view})
    if view == "global" and any(not event["source_anchors"] for event in events):
        raise WorkflowBlocked("source_anchor_invalid", {"view": view})
    return result


def _save_view(engine, child, run, output, analysis, kind):
    project = child["project_id"]
    if not engine._dependencies_valid(run):
        raise WorkflowBlocked("dependency_changed", {"view": engine._task_data(child)["step1_view"]})
    view = engine._task_data(child)["step1_view"]
    version = engine.workflow.save(project, kind, output, stage=1, run=run,
                                   inputs=run["input_refs"], effective=True)
    refs = {kind: ref(version)}
    if view == "global":
        analysis_version = engine.workflow.save(project, ANALYSIS_KINDS[view], analysis, stage=1, run=run,
                                                inputs=[*run["input_refs"], ref(version)], effective=True)
        refs[ANALYSIS_KINDS[view]] = ref(analysis_version)
    data = engine._task_data(child)
    data["result_ref"] = ref(version)
    data["result_refs"] = refs
    data.pop("resume_saved_result", None)
    engine._save_task_data(child, data)
    engine._event(project, "source.view_completed",
                  {"view": view, "artifact_ref": ref(version),
                   "analysis_ref": refs.get(ANALYSIS_KINDS.get(view))},
                  conversation=child["conversation_id"], task=child["id"], run=run["id"])
    if run["state"] == "running":
        engine._checkpoint(child, run, "advance_work")
        engine._close_run(child, run, "succeeded")
    engine._transition(engine.store.get(child["id"], project_id=project), "succeeded")
    return version


async def run_view(engine, child, token, source_ref, source_text, windowed):
    """Advance one view through its own Run, Session, and UTF-16 cursor."""
    project, cid = child["project_id"], child["conversation_id"]
    source_length = utf16_length(source_text)
    view = engine._task_data(child)["step1_view"]
    run = None
    while True:
        try:
            with engine.store.transaction():
                engine.store.advisory_lock(f"{project}:materials")
                engine.store.advisory_lock(f"{project}:conversation:{cid}")
                child = engine.store.get(child["id"], project_id=project)
                if child["state"] in ("succeeded", "paused", "stopped", "failed", "waiting_user"):
                    return
                engine._assert_authorized_source(child)
                engine._budget_check(child)
                data = engine._task_data(child)
                state = data["source_window_state"]
                if state["source_ref"] != source_ref or state["mode"] != ("window" if windowed else "full"):
                    raise WorkflowBlocked("dependency_changed", {"view": view})
                cursor = state["cursor"]
                if cursor == source_length and state["windows"]:
                    last = engine.store.get(state["windows"][-1]["run_id"], project_id=project)
                    windows = [{**item, "output": engine._projection(
                        project, "source_window_output", item["run_id"])["output"]}
                        for item in state["windows"]]
                    output = combine_view_windows(windows, view, source_ref, source_length)
                    _save_view(engine, child, last, output,
                               _analysis(windows[-1]["output"], view) if view == "global" else None,
                               VIEW_KINDS[view])
                    return
                previous_analysis = None
                previous_characters = None
                if windowed and state["windows"] and view == "global":
                    previous = engine._projection(project, "source_window_output", state["windows"][-1]["run_id"])
                    previous_analysis = _analysis(previous["output"], view)
                if windowed and state["windows"] and view == "character":
                    previous_characters = _character_continuity(engine, project, state["windows"])
                materials = engine.workflow.materials(project, 1)
                resume = data.get("resume_saved_result")
                if resume and (not isinstance(resume.get("output"), dict) or
                               engine._output_rejected(child, resume["source_run_id"])):
                    data.pop("resume_saved_result", None)
                    resume = None
                prior_id = data.get("current_run_id") or child.get("current_run_id")
                committed = {item["run_id"] for item in state["windows"]}
                if prior_id and prior_id not in committed and not data.get("result_ref"):
                    prior = engine.store.get(prior_id, project_id=project)
                    if prior and prior["state"] not in ("succeeded", "failed", "stopped", "paused", "interrupted", "waiting_user"):
                        if prior["lease_expires_at"] and prior["lease_expires_at"] > engine.store.now():
                            return
                        calls = all_records(engine.store, project, "model_call", run_id=prior_id)
                        if any(call["state"] in ("pending", "running", "unknown") for call in calls):
                            raise WorkflowBlocked("operation_uncertain", {"view": view})
                        update(engine.store, prior, state="interrupted", finished_at=engine.store.now(),
                               lease_owner=None, lease_expires_at=None)
                        session = engine.store.get(prior["session_id"], project_id=project)
                        update(engine.store, session, lease_owner=None, lease_expires_at=None,
                               fencing_token=session["fencing_token"] + 1)
                        child = update(engine.store, child, current_run_id=None)
                    if not resume and not data.get("manual_continuation") and prior and not engine._output_rejected(child, prior_id):
                        saved = engine._saved_output(child, prior_id)
                        if isinstance(saved, dict):
                            resume = {"source_run_id": prior_id, "output": saved}
                if not data.get("config_version_id"):
                    current_config = engine.config_service.resolve(project, "step1")
                    config_version = engine.config_service.resolve(
                        project, "step1", agent_key=step1_agent(view, current_config["values"]))
                    data["config_version_id"] = config_version["id"]
                engine._save_task_data(child, data)
                child, run, session, values = engine._start_run(
                    child, 1, materials, recovery=bool(resume), fresh_allowance=not resume,
                    session_key_override=f"step1:{child['parent_task_id']}:{view}:" +
                    (str(cursor) if windowed else "full"))
                if values.get("output", {}).get("structured", {}).get(f"step1.{view}", True) is False:
                    raise WorkflowBlocked("source_view_output_type_required", {"view": view})
                data = engine._task_data(child)
                data["model_dispatched"] = not bool(resume)
                engine._save_task_data(child, data)
            request = data.get("request", "分析原作")
            window = (await _budgeted_window(engine, child, run, session, values,
                       materials, source_text, cursor, view, previous_analysis, previous_characters, request)
                      if windowed and not resume else
                      source_window(source_text, cursor,
                          values["context"]["step1_source"]["window_tokens"], values["model"]["name"])
                      if windowed else None)
            if window is not None:
                window["view"] = view
            prompt = instructions("step1", values)
            prompt += "\n\n" + step1_run_appendix(
                view, "window" if windowed else "full", values,
                cursor if windowed else None,
                window["end_utf16"] if windowed else None)
            result = resume["output"] if resume else await engine._invoke(
                1, child, run, session, values, materials,
                request + f"\n当前视图：{VIEW_LABELS[view]}" +
                ("\n上一已检查原文前缀的累计分析（须结合本窗口更新，不得丢失仍有效的事实）：\n" +
                 previous_analysis if previous_analysis else "") +
                ("\n已阅读前缀中的人物身份与最近事件（沿用人物 ID；不要重复输出已完成事件）：\n" +
                 previous_characters if previous_characters else ""), token,
                instructions_override=prompt, step1_window=window, step1_view=view)
            with engine.store.transaction():
                engine.store.advisory_lock(f"{project}:conversation:{cid}")
                engine._assert_run(child, run, token)
                engine._save_projection(project, "run_result", run["id"],
                    {"output": result, **({"source_run_id": resume["source_run_id"]} if resume else {})})
                engine._checkpoint(child, run, "process_model_result")
            await engine._consume_steers(child, run, token)
            with engine.store.transaction():
                engine.store.advisory_lock(f"{project}:materials")
                engine.store.advisory_lock(f"{project}:conversation:{cid}")
                child = engine._assert_run(child, run, token)
                bound = engine._bind_program_provenance(1, run, result, materials)
                if windowed:
                    validated, commit = validate_window(bound, source_text, source_ref, view,
                                                         cursor, window["end_utf16"])
                    if view == "global":
                        _analysis(validated, view)
                    engine._save_projection(project, "source_window_output", run["id"],
                                            {"output": validated, "source_ref": source_ref})
                    data = engine._task_data(child)
                    state = data["source_window_state"]
                    state["windows"].append({"pass": view, "start_utf16": cursor,
                                             "commit_utf16": commit, "run_id": run["id"]})
                    state["cursor"] = commit
                    data.pop("resume_saved_result", None)
                    engine._save_task_data(child, data)
                    engine._event(project, "source.window_completed",
                        {"view": view, "start_utf16": cursor, "end_utf16": window["end_utf16"],
                         "commit_utf16": commit, "source_ref": source_ref},
                        conversation=cid, task=child["id"], run=run["id"])
                    if commit < source_length:
                        engine._checkpoint(child, run, "advance_work")
                        engine._close_run(child, run, "succeeded")
                        run = None
                        continue
                    windows = [{**item, "output": engine._projection(
                        project, "source_window_output", item["run_id"])["output"]}
                        for item in state["windows"]]
                    final = combine_view_windows(windows, view, source_ref, source_length)
                    analysis = _analysis(windows[-1]["output"], view) if view == "global" else None
                else:
                    final = _validated_full(bound, source_text, source_ref, view)
                    analysis = _analysis(final, view) if view == "global" else None
                    if view == "global":
                        final = _view_only(final)
                _save_view(engine, child, run, final, analysis, VIEW_KINDS[view])
                return
        except asyncio.CancelledError:
            if engine._closing:
                return
            engine._fail_execution(child, run, "user_stop")
            return
        except Exception as error:
            reason = getattr(error, "reason", None) or getattr(error, "code", None) or "configuration_error"
            repairable = (type(error).__name__ == "ValidationError" or reason in
                ("ModelBehaviorError", "source_anchor_invalid", "source_reference_mismatch",
                 "source_coverage_incomplete", "source_window_coverage_invalid",
                 "source_window_boundary_invalid", "source_window_event_outside_commit",
                 "source_window_wrong_view", "source_window_incomplete", "source_view_incomplete",
                 "source_analysis_incomplete", "source_window_empty_interval_unverified",
                 "output_schema_invalid"))
            if repairable and run:
                engine._repair_execution(child, run, str(error))
                child = engine.store.get(child["id"], project_id=project)
                if child["state"] == "running":
                    run = None
                    continue
                return
            explanation = {
                "source_window_no_complete_event": "当前窗口没有可确认的完整事件或空区间；请调大窗口 token 数后继续",
                "source_window_budget": "窗口 token 数太小，无法读取原文",
                "input_budget_exceeded": "原文和 instructions 超过模型输入预算；请缩小窗口或调整模型",
                "output_limit_exceeded": "模型输出达到上限，本窗口未记为完成",
                "source_view_output_type_required": "Step 1 两个事件视图都需要启用结构化 output_type",
            }.get(reason, str(error))
            engine._fail_execution(child, run, reason, explanation, getattr(error, "details", None))
            return


async def drive_step1_views(engine, task, token, source_version, source_text):
    """Coordinate two parallel producers; never combine their saved artifacts."""
    from .context import input_budget, tokens
    project, cid = task["project_id"], task["conversation_id"]
    source_ref = ref(source_version)
    try:
        with engine.store.transaction():
            engine.store.advisory_lock(f"{project}:materials")
            engine.store.advisory_lock(f"{project}:conversation:{cid}")
            task = engine.store.get(task["id"], project_id=project)
            engine._assert_authorized_source(task)
            engine._budget_check(task)
            data = engine._task_data(task)
            if data.get("step1_source_ref") and data["step1_source_ref"] != source_ref:
                raise WorkflowBlocked("dependency_changed", {"kind": "source_text"})
            data["step1_source_ref"] = source_ref
            children = data.get("step1_children", {})
            if not children:
                message = engine.store.get(task["requested_by_message_id"], project_id=project)
                base = engine.store.get(data["config_version_id"], project_id=project)["values"]
                for view in VIEW_KINDS:
                    config = engine.config_service.resolve(project, "step1", agent_key=step1_agent(view, base))
                    values = config["values"]
                    model = values["model"]["name"]
                    source_tokens = tokens(source_text, model)
                    trigger = values["context"]["step1_source"]["trigger_tokens"]
                    # Each Agent has its own model and output reserve. A source
                    # below the configured trigger can still exceed this
                    # branch's actual request budget.
                    safe_full_budget = max(0, input_budget(values) - min(8192, input_budget(values) // 10))
                    mode = "window" if source_tokens > trigger or source_tokens > safe_full_budget else "full"
                    child = engine._new_task(project, cid, message, "generate", stage=1, parent=task,
                        request=data.get("request", "切分原作"), parent_owned=True, step1_view=view,
                        work_key=f"{task['id']}:step1:{view}")
                    child_data = engine._task_data(child)
                    child_data["config_version_id"] = config["id"]
                    child_data["source_window_state"] = {"source_ref": source_ref, "mode": mode,
                                                         "cursor": 0, "windows": []}
                    engine._save_task_data(child, child_data)
                    children[view] = child["id"]
                data["step1_children"] = children
            data["step1_modes"] = {view: engine._task_data(engine.store.get(child_id, project_id=project))
                                     ["source_window_state"]["mode"] for view, child_id in children.items()}
            engine._save_task_data(task, data)
            if task["state"] != "running":
                engine._transition(task, "running")
        children = [engine.store.get(engine._task_data(task)["step1_children"][view], project_id=project)
                    for view in VIEW_KINDS]
        outcomes = await asyncio.gather(*(
            run_view(engine, child, token, source_ref, source_text,
                     engine._task_data(child)["source_window_state"]["mode"] == "window")
            for child in children),
            return_exceptions=True)
        for outcome in outcomes:
            if isinstance(outcome, BaseException):
                raise outcome
        with engine.store.transaction():
            engine.store.advisory_lock(f"{project}:materials")
            engine.store.advisory_lock(f"{project}:conversation:{cid}")
            task = engine.store.get(task["id"], project_id=project)
            if task["state"] in ("stopping", "stopped", "failed"):
                return
            children = {view: engine.store.get(engine._task_data(task)["step1_children"][view], project_id=project)
                        for view in VIEW_KINDS}
            if any(child["state"] != "succeeded" for child in children.values()):
                engine._transition(task, "paused", "child_blocked")
                return
            refs = {kind: fixed_ref for child in children.values()
                    for kind, fixed_ref in engine._task_data(child)["result_refs"].items()}
            for kind in VIEW_KINDS.values():
                fixed_ref = refs[kind]
                version = engine.workflow.fixed_version(project, fixed_ref)
                if body(engine.store, version)["payload"]["source_ref"] != source_ref:
                    raise WorkflowBlocked("dependency_changed", {"kind": kind})
            for kind in ANALYSIS_KINDS.values():
                version = engine.workflow.fixed_version(project, refs[kind])
                if source_ref not in version["source_refs"]:
                    raise WorkflowBlocked("dependency_changed", {"kind": kind})
            data = engine._task_data(task)
            data["result_refs"] = refs
            engine._save_task_data(task, data)
            for kind in ("source_global_events", "source_global_analysis",
                         "source_character_events"):
                fixed_ref = refs[kind]
                version = engine.workflow.fixed_version(project, fixed_ref)
                presented = stage_result_text(1, body(engine.store, version),
                    version=version["version"], artifact_kind=kind)
                message = engine._message(project, cid, presented, task=task["id"])
                engine._event(project, "artifact.presented",
                    {"artifact_ref": fixed_ref, "message_id": message["id"], "selections": [{"item_id": None, "json_pointer": ""}]},
                    conversation=cid, task=task["id"])
            engine._transition(task, "succeeded")
    except asyncio.CancelledError:
        if engine._closing:
            return
        engine._fail_execution(task, None, "user_stop")
    except Exception as error:
        reason = getattr(error, "reason", None) or getattr(error, "code", None) or "configuration_error"
        engine._fail_execution(task, None, reason, str(error), getattr(error, "details", None))
