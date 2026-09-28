"""SDK tools through which the conversation coordinator owns stage dispatch.

The stage tool is deliberately a function tool around the existing child
Runner.  A child Task, Run, and fenced Session must be allocated when the
manager actually calls it, not when the coordinator Agent is constructed.
"""
from __future__ import annotations

import asyncio
import json

from agents import function_tool

from .workflow import STAGE_OUTPUTS, STAGES, WorkflowBlocked, all_records, body, ref


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _markdown_download_url(project, fixed_ref):
    if not fixed_ref:
        return None
    return (f"/api/branch-agent/v1/projects/{project}/artifacts/{fixed_ref['record_id']}"
            f"/versions/{fixed_ref['version']}/download.md")


def _markdown_download_urls(project, fixed_refs):
    return {kind: _markdown_download_url(project, fixed_ref)
            for kind, fixed_ref in fixed_refs.items()}


def manager_state(engine, project, conversation):
    """Compact factual state for routing; the model never invents record IDs."""
    tasks = all_records(engine.store, project, "task", conversation_id=conversation)
    roots = [task for task in tasks if engine._task_data(task).get("manager_controlled")]
    roots.sort(key=lambda row: row["created_at"])
    root = next((row for row in reversed(roots) if row["state"] in ("queued", "running", "waiting_user", "paused")),
                roots[-1] if roots else None)
    children = [task for task in tasks if root and task["parent_task_id"] == root["id"]
                and not engine._task_data(task).get("invalid_stage_dispatch")]
    pending = []
    for candidate in ([root] if root else []) + children:
        pending.extend({"id": item["id"], "kind": item["kind"], "description": item["description"],
                        "stage": engine._task_data(candidate).get("stage"),
                        "chapter_id": engine._task_data(candidate).get("chapter_id")}
                       for item in engine._task_data(candidate).get("pending_user_items", [])
                       if item["state"] == "open")
    try:
        source = ref(engine.workflow.resolve(project, "source_text"))
    except WorkflowBlocked:
        source = None
    return {"source_available": source is not None,
            "workflow": None if root is None else {
                "id": root["id"], "state": root["state"],
                "stages": engine._task_data(root).get("stages", []),
                "chapters": engine._task_data(root).get("chapter_ids", []),
                "recovery_stage": engine._task_data(root).get("recovery_stage"),
                "chapter_plan_confirmed": bool(engine._task_data(root).get("confirmed_chapter_plan_ref")),
                "children": [{"stage": engine._task_data(child).get("stage"),
                              "chapter_id": engine._task_data(child).get("chapter_id"),
                              "task_id": child["id"], "state": child["state"],
                              "confirmation_candidate": bool(child["state"] == "waiting_user"
                                  and engine._task_data(child).get("stage") in range(2, 11)
                                  and (engine._task_data(child).get("result_ref") or engine._task_data(child).get("result_refs"))
                                  and not any(item["state"] == "open" for item in engine._task_data(child).get("pending_user_items", []))),
                              "artifact_ref": engine._task_data(child).get("result_ref"),
                              "artifact_refs": engine._task_data(child).get("result_refs", {})}
                             for child in children]},
            "pending": pending}


def build_manager_tools(engine, coordinator_task, coordinator_run, source_message,
                        presented, token, continuation: bool = False):
    """Bind one manager turn to its real user message and fenced coordinator Run."""
    project, conversation = source_message["project_id"], source_message["conversation_id"]
    selected = {"root_id": None}
    stage_lock = asyncio.Lock()

    def root():
        if selected["root_id"]:
            row = engine.store.get(selected["root_id"], project_id=project)
            if row and row["state"] in ("queued", "running", "waiting_user"):
                return row
        rows = [task for task in all_records(engine.store, project, "task", conversation_id=conversation)
                if engine._task_data(task).get("manager_controlled")
                and task["state"] in ("queued", "running", "waiting_user")]
        rows.sort(key=lambda row: row["created_at"])
        if not rows:
            raise WorkflowBlocked("manager_workflow_missing")
        selected["root_id"] = rows[-1]["id"]
        return rows[-1]

    def complete_workflow(workflow):
        """Verify every authorized stage before closing the manager-owned task."""
        data = engine._task_data(workflow)
        children = [task for task in all_records(engine.store, project, "task", parent_task_id=workflow["id"])
                    if not engine._task_data(task).get("superseded_by_task_id")
                    and not engine._task_data(task).get("invalid_stage_dispatch")]
        if any(item["state"] == "open" for task in [workflow] + children
               for item in engine._task_data(task).get("pending_user_items", [])):
            raise WorkflowBlocked("confirmation_required")
        if any(task["state"] != "succeeded" for task in children):
            raise WorkflowBlocked("manager_workflow_incomplete")
        chapters = data.get("chapter_ids", [])
        if any(stage in (9, 10, 11) for stage in data.get("stages", [])) and not chapters:
            raise WorkflowBlocked("chapter_scope_required")
        for stage in data.get("stages", []):
            for chapter in (chapters if stage in (9, 10) else [None]):
                if stage == 1:
                    views = [engine.workflow.resolve(project, kind)
                             for kind in STAGE_OUTPUTS[1]]
                    engine.workflow.require_approved_step1_inputs(project, [
                        {"kind": kind, "record": version}
                        for kind, version in zip(STAGE_OUTPUTS[1], views)])
                completed = any(engine._task_data(task).get("stage") == stage
                                and (stage == 11 or engine._task_data(task).get("chapter_id") == chapter)
                                and task["state"] == "succeeded" for task in children)
                if completed:
                    continue
                if stage in (3, 4, 11) or data.get("fresh_start"):
                    raise WorkflowBlocked("manager_workflow_incomplete", {"stage": stage, "chapter_id": chapter})
                try:
                    versions = [engine.workflow.resolve(project, kind, chapter)
                                for kind in STAGE_OUTPUTS.get(stage, (STAGES[stage],))]
                    if stage == 6:
                        events = body(engine.store, versions[0])["payload"]["events"]
                        if not events or any(not event.get("narrative_function") for event in events):
                            raise WorkflowBlocked("manager_workflow_incomplete")
                except WorkflowBlocked:
                    raise WorkflowBlocked("manager_workflow_incomplete", {"stage": stage, "chapter_id": chapter}) from None
        if workflow["state"] != "succeeded":
            engine._transition(workflow, "succeeded")
        return {"status": "completed", "workflow_id": workflow["id"]}

    @function_tool(failure_error_function=None)
    async def begin_adaptation(request: str, source_is_current_message: bool = False,
                               stage: int | None = None, chapter_id: str | None = None) -> str:
        """Start one authorized adaptation; paste-source detection saves the exact user text."""
        if continuation:
            raise WorkflowBlocked("manager_continuation_cannot_start")
        if not request.strip() or stage is not None and (stage < 1 or stage > 11):
            raise WorkflowBlocked("invalid_manager_request")
        with engine.store.transaction():
            engine.store.advisory_lock(f"{project}:materials")
            engine.store.advisory_lock(f"{project}:conversation:{conversation}")
            active = [task for task in all_records(engine.store, project, "task", conversation_id=conversation)
                      if engine._task_data(task).get("is_workflow")
                      and task["state"] in ("queued", "running", "waiting_user")]
            if active:
                existing = active[-1]
                existing_data = engine._task_data(existing)
                if not existing_data.get("manager_controlled") or existing["requested_by_message_id"] != source_message["id"]:
                    # This is an ordinary routing conflict, not a failed SDK tool call.
                    # The receipt lets the coordinator close or resume the existing
                    # workflow without granting the new request any stage authority.
                    return _json({"status": "workflow_active", "reason": "manager_workflow_active",
                                  "workflow_id": existing["id"], "task_id": existing["id"],
                                  "state": existing["state"],
                                  "scope": {"stages": existing_data.get("stages", []),
                                            "chapter_ids": existing_data.get("chapter_ids", [])},
                                  "manager_controlled": bool(existing_data.get("manager_controlled"))})
                selected["root_id"] = existing["id"]
                return _json({"status": "already_started", "workflow_id": existing["id"],
                              "stages": existing_data.get("stages", [])})
            if source_is_current_message:
                engine._import_source_message(source_message)
            command = {"intent": "generate", "stage": stage, "chapter_id": chapter_id,
                       "target_ref": None, "request": request,
                       "source_message_ids": [source_message["id"]],
                       "requested_confirmation_paths": []}
            receipt = engine._apply_request(command, source_message, presented,
                                            coordinator_task, coordinator_run)
            target = engine.store.get(receipt["task_id"], project_id=project)
            if not engine._task_data(target).get("is_workflow"):
                raise WorkflowBlocked("manager_workflow_missing")
            data = engine._task_data(target)
            data["manager_controlled"] = True
            engine._save_task_data(target, data)
            selected["root_id"] = target["id"]
            return _json({"status": "started", "workflow_id": target["id"],
                          "stages": data["stages"], "source_available": bool(data.get("source_ref"))})

    @function_tool(failure_error_function=None)
    async def run_stage(stage: int, chapter_id: str | None = None) -> str:
        """Run one eligible specialist Agent; return saved references and fixed Markdown download links."""
        if stage < 1 or stage > 11:
            raise WorkflowBlocked("invalid_manager_stage")
        # A provider may ignore parallel_tool_calls=False. Serialize dispatch and
        # execution so another run_stage call sees the first child's final state.
        async with stage_lock:
            return await run_stage_serial(stage, chapter_id)

    async def run_stage_serial(stage: int, chapter_id: str | None) -> str:
        with engine.store.transaction():
            engine.store.advisory_lock(f"{project}:conversation:{conversation}")
            workflow = root()
            data = engine._task_data(workflow)
            if stage not in data.get("stages", []):
                raise WorkflowBlocked("outside_authorized_scope")
            if stage in (9, 10) and (not chapter_id or chapter_id not in data.get("chapter_ids", [])):
                raise WorkflowBlocked("chapter_scope_required")
            previous_children = all_records(engine.store, project, "task", parent_task_id=workflow["id"])
            children = [task for task in previous_children if not engine._task_data(task).get("superseded_by_task_id")
                        and not engine._task_data(task).get("invalid_stage_dispatch")]
            pending = [{"id": item["id"], "task_id": task["id"], "stage": engine._task_data(task).get("stage")}
                       for task in [workflow] + children
                       for item in engine._task_data(task).get("pending_user_items", []) if item["state"] == "open"]
            if pending:
                return _json({"status": "needs_user_input", "pending": pending})
            candidates = [task for task in children if task["state"] == "waiting_user"
                          and engine._task_data(task).get("stage") in range(2, 11)
                          and (engine._task_data(task).get("result_ref") or engine._task_data(task).get("result_refs"))]
            if candidates:
                candidate = candidates[-1]
                candidate_data = engine._task_data(candidate)
                return _json({"status": "candidate_ready", "stage": candidate_data["stage"],
                              "chapter_id": candidate_data.get("chapter_id"), "task_id": candidate["id"],
                              "artifact_ref": candidate_data.get("result_ref"),
                              "artifact_refs": candidate_data.get("result_refs", {}),
                              "markdown_download_url": _markdown_download_url(project, candidate_data.get("result_ref")),
                              "markdown_download_urls": _markdown_download_urls(project, candidate_data.get("result_refs", {}))})
            matching = [task for task in children if engine._task_data(task).get("stage") == stage
                        and engine._task_data(task).get("chapter_id") == chapter_id]
            matching.sort(key=lambda task: task["created_at"])
            waiting = [task for task in matching if task["state"] == "queued"]
            child = waiting[-1] if waiting else matching[-1] if matching else None
            if child is None:
                try:
                    engine.workflow.materials(project, stage, chapter_id)
                except WorkflowBlocked as error:
                    return _json({"status": "prerequisite_pending", "stage": stage,
                                  "chapter_id": chapter_id, "reason": error.reason,
                                  "details": error.details})
                invalid_prior = any(engine._task_data(task).get("invalid_stage_dispatch")
                                    and engine._task_data(task).get("stage") == stage
                                    and engine._task_data(task).get("chapter_id") == chapter_id
                                    for task in previous_children)
                child = engine._dispatch(workflow, stage, chapter_id, regenerate=invalid_prior)
            child_data = engine._task_data(child)
            child_data["parent_owned"] = True
            engine._save_task_data(child, child_data)
            if data.get("recovery_stage") == stage:
                data.pop("recovery_stage", None)
                engine._save_task_data(workflow, data)
            if workflow["state"] != "running":
                engine._transition(workflow, "running")
        if child["state"] in ("queued", "running"):
            await engine._execute(child, token)
        with engine.store.transaction():
            child = engine.store.get(child["id"], project_id=project)
            if child["state"] not in ("queued", "running"):
                child_data = engine._task_data(child)
                child_data.pop("parent_owned", None)
                engine._save_task_data(child, child_data)
            workflow = root()
            if child["state"] == "waiting_user" and workflow["state"] != "waiting_user":
                engine._transition(workflow, "waiting_user")
            elif child["state"] in ("paused", "failed", "stopped") and workflow["state"] != "paused":
                engine._transition(workflow, "paused", "child_blocked")
            data = engine._task_data(child)
            open_items = [item["id"] for task in (workflow, child)
                          for item in engine._task_data(task).get("pending_user_items", [])
                          if item["state"] == "open"]
            candidate_ready = bool(child["state"] == "waiting_user" and stage in range(2, 11)
                and (data.get("result_ref") or data.get("result_refs"))
                and not open_items)
            status = ("needs_user_input" if open_items else "candidate_ready" if candidate_ready else
                      "prerequisite_pending" if child["state"] == "waiting_user" else child["state"])
            if stage == 11 and status == "succeeded" and all_records(
                    engine.store, project, "runtime_event", event_name="project.delivered", task_id=child["id"]):
                complete_workflow(workflow)
                status = "completed"
            return _json({"status": status,
                          "stage": stage, "chapter_id": chapter_id,
                          "task_id": child["id"], "artifact_ref": data.get("result_ref"),
                          "artifact_refs": data.get("result_refs", {}),
                          "markdown_download_url": _markdown_download_url(project, data.get("result_ref")),
                          "markdown_download_urls": _markdown_download_urls(project, data.get("result_refs", {})),
                          "pending_item_ids": open_items,
                          "reason": "stage_waiting_without_pending_item" if status == "prerequisite_pending" else None,
                          "pause_reason": child.get("pause_reason")})

    @function_tool(failure_error_function=None)
    async def confirm_pending(pending_item_id: str) -> str:
        """Confirm every fixed version in one displayed card after this user's explicit approval."""
        if continuation:
            raise WorkflowBlocked("manager_continuation_cannot_confirm")
        with engine.store.transaction():
            engine.store.advisory_lock(f"{project}:materials")
            engine.store.advisory_lock(f"{project}:conversation:{conversation}")
            workflow = root()
            candidates = [workflow] + all_records(engine.store, project, "task", parent_task_id=workflow["id"])
            found = [(task, item) for task in candidates for item in engine._task_data(task).get("pending_user_items", [])
                     if item["id"] == pending_item_id and item["state"] == "open" and item["kind"] == "confirmation"]
            if len(found) != 1:
                raise WorkflowBlocked("confirmation_target_ambiguous")
            _, item = found[0]
            if not item["targets"]:
                raise WorkflowBlocked("confirmation_target_ambiguous")
            receipts = []
            for target in item["targets"]:
                request = {"intent": "confirm", "stage": None, "chapter_id": None,
                           "target_ref": target["subject"], "request": "确认已展示的固定版本",
                           "source_message_ids": [source_message["id"]],
                           "requested_confirmation_paths": [part["json_pointer"] for part in target["selections"]]}
                receipts.append(engine._apply_request(request, source_message, presented,
                                                      coordinator_task, coordinator_run))
            return _json({"status": "confirmed", "confirmed_count": len(receipts), "receipts": receipts})

    @function_tool(failure_error_function=None)
    async def answer_pending(pending_item_id: str) -> str:
        """Use this user's complete message as the answer to one displayed question."""
        if continuation:
            raise WorkflowBlocked("manager_continuation_cannot_answer")
        with engine.store.transaction():
            engine.store.advisory_lock(f"{project}:conversation:{conversation}")
            workflow = root()
            found = [(task, item) for task in all_records(engine.store, project, "task", parent_task_id=workflow["id"])
                     for item in engine._task_data(task).get("pending_user_items", [])
                     if item["id"] == pending_item_id and item["state"] == "open"
                     and item["kind"] in ("question", "decision")]
            if len(found) != 1:
                raise WorkflowBlocked("continuation_target_ambiguous")
            task, item = found[0]
            shown = [engine.store.get(mid, project_id=project) for mid in item["presented_message_ids"]]
            if not shown or not all(row and row["sequence"] < source_message["sequence"] for row in shown):
                raise WorkflowBlocked("question_not_presented")
            answer = body(engine.store, source_message).strip()
            if not answer:
                raise WorkflowBlocked("answer_required")
            data = engine._task_data(task)
            data["request"] = data.get("request", "") + f"\n问题 {item.get('question_id') or item['id']}：{item['description']}\n用户明确答复（{source_message['id']}）：{answer}"
            item.update(state="resolved", answer_message_ids=[source_message["id"]])
            data["parent_owned"] = True
            engine._save_task_data(task, data)
            if not any(p["state"] == "open" for p in data["pending_user_items"]):
                engine._transition(task, "queued", source=source_message["id"])
                if workflow["state"] == "waiting_user":
                    engine._transition(workflow, "running")
            return _json({"status": "answered", "stage": data["stage"],
                          "remaining": sum(p["state"] == "open" for p in data["pending_user_items"])})

    @function_tool(failure_error_function=None)
    async def revise_pending(pending_item_id: str) -> str:
        """Create a revision task from the user's explicit change request to one shown draft."""
        if continuation:
            raise WorkflowBlocked("manager_continuation_cannot_revise")
        with engine.store.transaction():
            engine.store.advisory_lock(f"{project}:materials")
            engine.store.advisory_lock(f"{project}:conversation:{conversation}")
            workflow = root()
            found = [(task, item) for task in [workflow] + all_records(engine.store, project, "task", parent_task_id=workflow["id"])
                     for item in engine._task_data(task).get("pending_user_items", [])
                     if item["id"] == pending_item_id and item["state"] == "open" and item["kind"] == "confirmation"]
            if len(found) != 1:
                raise WorkflowBlocked("revision_target_ambiguous")
            task, item = found[0]
            shown = [engine.store.get(mid, project_id=project) for mid in item["presented_message_ids"]]
            if not shown or not all(row and row["sequence"] < source_message["sequence"] for row in shown):
                raise WorkflowBlocked("revision_target_not_presented")
            instruction = body(engine.store, source_message).strip()
            if not instruction:
                raise WorkflowBlocked("revision_instruction_required")
            from .actions import ActionService
            replacement = ActionService(engine)._request_changes(project, conversation, task, item,
                                                                  instruction, source_message,
                                                                  schedule_manager_resume=False)
            return _json(replacement)

    @function_tool(failure_error_function=None)
    async def propose_chapters(chapter_ids: list[str], explanation: str) -> str:
        """Present an ordered chapter plan for user confirmation before Step9."""
        if not chapter_ids or not explanation.strip() or any(not item.strip() for item in chapter_ids):
            raise WorkflowBlocked("chapter_plan_invalid")
        with engine.store.transaction():
            engine.store.advisory_lock(f"{project}:conversation:{conversation}")
            workflow = root()
            data = engine._task_data(workflow)
            if data.get("confirmed_chapter_plan_ref"):
                raise WorkflowBlocked("chapter_plan_already_confirmed")
            if data.get("chapter_plan"):
                return _json({"status": "already_proposed", "chapter_ids": data["chapter_plan"]["chapter_ids"]})
            if any(item["state"] == "open" for task in [workflow] + all_records(engine.store, project, "task", parent_task_id=workflow["id"])
                   for item in engine._task_data(task).get("pending_user_items", [])):
                raise WorkflowBlocked("confirmation_required")
            engine.workflow.materials(project, 9, chapter_ids[0])
            requests = [{"intent": "generate", "stage": 9, "chapter_id": chapter_id,
                         "target_ref": None, "request": f"设计章节 {chapter_id}",
                         "source_message_ids": [source_message["id"]],
                         "requested_confirmation_paths": []} for chapter_id in chapter_ids]
            engine._propose_chapters(workflow, requests, source_message, explanation, coordinator_run)
            engine._transition(workflow, "waiting_user")
            return _json({"status": "waiting_user", "chapter_ids": chapter_ids})

    @function_tool(failure_error_function=None)
    async def finish_workflow() -> str:
        """Close an authorized workflow only after every required stage really finished."""
        with engine.store.transaction():
            engine.store.advisory_lock(f"{project}:conversation:{conversation}")
            return _json(complete_workflow(root()))

    @function_tool(failure_error_function=None)
    async def get_workflow_state() -> str:
        """Read current stage status and pending user decisions after a tool call."""
        return _json(manager_state(engine, project, conversation))

    return [begin_adaptation, run_stage, confirm_pending, answer_pending, revise_pending,
            propose_chapters, finish_workflow, get_workflow_state]


def manager_instructions(config, state):
    from .prompts import instructions, harness_prompts
    harness = harness_prompts(config)
    structured = config.get("output", {}).get("structured", {}).get("coordinator", True)
    format_rule = harness['manager_structured' if structured else 'manager_plain']
    return '\n\n'.join(filter(None, (instructions('coordinator', config), harness['manager'],
                                     format_rule, '当前项目事实状态：' + _json(state))))
