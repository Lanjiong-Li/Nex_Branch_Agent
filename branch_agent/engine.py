"""Persistent, fenced conversation scheduler for the Branch Agent workflow."""
from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import inspect
import json
import logging
from uuid import uuid4

from .records import new_record, scope, usage, budget, canonical_bytes
from .workflow import (Workflow, WorkflowBlocked, STAGES, STAGE_OUTPUTS, WHOLE, all_records,
                       body, ref, update, session_key, locate_source_anchors)
from .prompts import stage_agent, agent_instructions, harness_prompts
from .graph import (assemble_project, merge_chapter, quality_checks, GraphError,
                    GraphValidationError, validate_output, project_profile)
from .graph_contract import load_graph_contract
from .presentation import stage_result_text
from .output_repair import assess as assess_output, apply_format_repair, candidate_hash
from .schemas import OutputValidationError

LOG = logging.getLogger(__name__)
CLOSED_RUNS = {"succeeded", "waiting_user", "paused", "failed", "stopped", "interrupted"}
# Test rollout: temporarily bypass billing gates, while keeping accounting and
# the gated implementation available for a later production rollout.
COST_GATES_ENABLED = False


def _after(value, seconds):
    return (datetime.fromisoformat(value.replace("Z", "+00:00")) + timedelta(seconds=seconds)).isoformat().replace("+00:00", "Z")


def _cfg(config, key, default):
    if key in config:
        return config[key]
    value = config
    for part in key.split("."):
        if not isinstance(value, dict) or part not in value:
            return default
        value = value[part]
    return value


class Engine:
    def __init__(self, store, model_service, config_service, *, cost_gates_enabled=COST_GATES_ENABLED):
        self.store, self.model_service, self.config_service = store, model_service, config_service
        self.cost_gates_enabled = cost_gates_enabled
        self.workflow = Workflow(store)
        self.owner = str(uuid4())
        self._worker = None
        self._running = {}
        self._calls = {}
        self._heartbeat_events = {}
        self._forced_stops = {}
        self._deleting_projects = set()
        self._wake = asyncio.Event()
        self._closing = False

    def _key(self, project, kind, identity):
        return f"{project}:{kind}:{identity}"

    def _projection(self, project, kind, identity, **defaults):
        return self.store.projection_get(self._key(project, kind, identity)) or {"project_id": project, **defaults}

    def _save_projection(self, project, kind, identity, value):
        value["project_id"] = project
        return self.store.projection_put(self._key(project, kind, identity), value)

    def _control(self, project, conversation):
        return self._projection(project, "conversation_control", conversation, holds=[], row_version=1,
                                lease_owner=None, lease_expires_at=None, fencing_token=0)

    def _effective_holds(self, project, gate):
        """Ignore old billing holds without rewriting their stored evidence."""
        if self.cost_gates_enabled:
            return gate['holds']
        active = []
        for hold in gate['holds']:
            if hold['reason_code'] == 'usage_uncertain':
                continue
            if hold['reason_code'] == 'budget_limit':
                basis = self.store.get(hold['basis_event_id'], project_id=project)
                if basis and basis.get('payload', {}).get('reason') == 'cost_limit':
                    continue
            active.append(hold)
        return active

    def _task_data(self, task):
        return self._projection(task["project_id"], "task_runtime", task["id"])

    def _save_task_data(self, task, data):
        return self._save_projection(task["project_id"], "task_runtime", task["id"], data)

    def _output_lineage(self, task, run_id):
        """Follow exact cached-response origins, never another Task's output."""
        lineage = []
        while run_id:
            candidate = self.store.get(run_id, project_id=task["project_id"])
            if not candidate or candidate["task_id"] != task["id"] or run_id in lineage:
                raise WorkflowBlocked("checkpoint_invalid", {"reason": "invalid cached output origin"})
            lineage.append(run_id)
            run_id = self._projection(task["project_id"], "run_result", run_id).get("source_run_id")
        return lineage

    def _output_rejected(self, task, run_id):
        rejected = set(self._task_data(task).get("rejected_output_run_ids", []))
        for origin in self._output_lineage(task, run_id):
            record = self.store.get(origin, project_id=task["project_id"])
            # Closed pre-upgrade Runs also prove that their response was rejected.
            if origin in rejected or (record.get("error") or {}).get("code") in ("output_validation_failed", "repair_exhausted"):
                return True
        return False

    def _saved_output(self, task, run_id):
        if not run_id or self._output_rejected(task, run_id):
            return None
        return self._projection(task["project_id"], "run_result", run_id).get("output")

    def _event(self, project, name, payload, *, conversation=None, task=None, run=None, source=None):
        # This lock serializes project event ordering across all conversations/workers.
        self.store.advisory_lock(f"{project}:event_sequence")
        sequence = self._projection(project, "sequence", "events", value=0)
        sequence["value"] += 1
        self._save_projection(project, "sequence", "events", sequence)
        return self.store.put(new_record("runtime_event", project, sequence=sequence["value"],
            conversation_id=conversation, task_id=task, run_id=run, event_name=name,
            subject_ref=None, actor_kind="program", actor_account_id=None,
            payload=payload, causation_id=source))

    def _message(self, project, conversation, text, role="assistant", task=None, run=None,
                 visibility="conversation"):
        self.store.advisory_lock("history:" + conversation)
        conv = self.store.get(conversation, project_id=project)
        if not conv or conv["record_type"] != "conversation":
            raise WorkflowBlocked("conversation_not_found")
        sequence = conv["last_message_seq"] + 1
        update(self.store, conv, last_message_seq=sequence)
        message = self.store.put(new_record("history_record", project, conversation_id=conversation,
            sequence=sequence, session_id=None, task_id=task, run_id=run, role=role,
            visibility=visibility, content={"storage": "inline_text", "text": text},
            attachment_blob_ids=[], source_message_id=None, operation_id=None, provider_item_id=None))
        self._event(project, "message.created", {"message_id": message["id"]}, conversation=conversation,
                    task=task, run=run, source=message["id"])
        return message

    def submit_message(self, project_id, conversation_id, text, mode="queue", target_run_id=None, idempotency_key=None):
        if mode not in ("queue", "steer") or not text.strip():
            raise WorkflowBlocked("invalid_message")
        request_hash = hashlib.sha256(json.dumps([conversation_id, text, mode, target_run_id], ensure_ascii=False).encode()).hexdigest()
        with self.store.transaction():
            self.store.advisory_lock(f"{project_id}:conversation:{conversation_id}")
            if idempotency_key:
                cached = self._projection(project_id, "message_idempotency", idempotency_key)
                if cached.get("request_hash"):
                    if cached["request_hash"] != request_hash:
                        raise WorkflowBlocked("idempotency_conflict")
                    return self.store.get(cached["message_id"], project_id=project_id)
            if mode == "steer" and not target_run_id:
                raise WorkflowBlocked("target_run_required")
            message = self._message(project_id, conversation_id, text, role="user")
            blocked = None
            if mode == "steer":
                run = self.store.get(target_run_id, project_id=project_id)
                task = self.store.get(run["task_id"], project_id=project_id) if run else None
                if not run or run["state"] != "running" or not task or task["conversation_id"] != conversation_id:
                    blocked = "target_not_running"
            queued = self.store.put(new_record("queued_request", project_id, conversation_id=conversation_id,
                source_message_id=message["id"], mode=mode, target_run_id=target_run_id,
                sequence=message["sequence"], scope=scope(description=text[:200]),
                state="blocked" if blocked else "pending", adopted_run_id=None,
                adopted_context_snapshot_id=None, created_task_id=None, resolved_at=None, blocked_reason=blocked))
            presented = self._projection(project_id, "presentations", conversation_id, targets=[])
            self._save_projection(project_id, "queue_context", queued["id"],
                                  {"presented": deepcopy(presented["targets"]), "request_hash": request_hash})
            if idempotency_key:
                self._save_projection(project_id, "message_idempotency", idempotency_key,
                                      {"request_hash": request_hash, "message_id": message["id"], "queue_id": queued["id"]})
        self._wake.set()
        return message

    def import_source(self, project_id, conversation_id, text, name, *, start_step1=False,
                      step1_block_reason=None):
        if not text:
            raise WorkflowBlocked("source_empty")
        with self.store.transaction():
            self.store.advisory_lock(f"{project_id}:materials")
            if start_step1 or step1_block_reason:
                self.store.advisory_lock(f"{project_id}:conversation:{conversation_id}")
                active = [task for task in all_records(self.store, project_id, "task")
                          if self._task_data(task).get("is_workflow")
                          and task["state"] in ("queued", "running", "waiting_user")]
                if active:
                    raise WorkflowBlocked("source_import_workflow_active", {"task_id": active[-1]["id"]})
            message = self._message(project_id, conversation_id, f"已导入原作：{name}（{len(text)} 字符）", role="user")
            version = self.workflow.save(project_id, "source_text", text, origin="import", inputs=[ref(message)], effective=True)
            self._event(project_id, "source.imported", {"artifact_ref": ref(version), "name": name}, conversation=conversation_id, source=message["id"])
            step1_task = None
            if start_step1:
                request = {"intent": "generate", "stage": 1, "chapter_id": None,
                           "target_ref": None, "request": "分析刚导入的完整原作，执行 Step1。",
                           "source_message_ids": [message["id"]], "requested_confirmation_paths": []}
                receipt = self._apply_request(request, message, [], None, None)
                step1_task = self.store.get(receipt["task_id"], project_id=project_id)
                data = self._task_data(step1_task)
                if not data.get("is_workflow") or data.get("source_ref") != ref(version):
                    raise WorkflowBlocked("source_reference_mismatch", {"expected_source_ref": ref(version)})
                data["fresh_start"] = True
                self._save_task_data(step1_task, data)
                self._event(project_id, "source.step1_queued",
                            {"artifact_ref": ref(version), "task_id": step1_task["id"]},
                            conversation=conversation_id, task=step1_task["id"], source=message["id"])
                self._event(project_id, "chat.activity",
                            {"text": "原作已保存，Step1 已排队启动。", "kind": "source",
                             "status": "queued", "stage": "step1"},
                            conversation=conversation_id, task=step1_task["id"], source=message["id"])
            elif step1_block_reason == "budget_insufficient":
                self._event(project_id, "chat.activity",
                            {"text": "原作已保存，Step1 未启动：当前模型预算不足。", "kind": "source",
                             "status": "blocked", "stage": "step1"},
                            conversation=conversation_id, source=message["id"])
        if step1_task:
            self._wake.set()
        return {"source": version, "message": message, "step1_task": step1_task}

    def _import_source_message(self, message):
        """Promote one exact user message to the effective source, once."""
        project_id = message["project_id"]
        saved = self._projection(project_id, "message_source", message["id"])
        if saved.get("artifact_ref"):
            return self.workflow.fixed_version(project_id, saved["artifact_ref"])
        text = body(self.store, message)
        if not isinstance(text, str) or not text.strip():
            raise WorkflowBlocked("source_empty")
        version = self.workflow.save(project_id, "source_text", text, origin="import",
                                     inputs=[ref(message)], effective=True)
        source_ref = ref(version)
        self._save_projection(project_id, "message_source", message["id"], {"artifact_ref": source_ref})
        self._event(project_id, "source.detected_from_message", {"artifact_ref": source_ref},
                    conversation=message["conversation_id"], source=message["id"])
        return version

    def status(self, project_id):
        deliveries = [e for e in all_records(self.store, project_id, "runtime_event") if e["event_name"] == "project.delivered"]
        delivered_ref = max(deliveries, key=lambda e: e["sequence"])["payload"]["artifact_ref"] if deliveries else None
        return {"latest_delivery": ({"artifact_ref": delivered_ref, "artifact_id": delivered_ref["record_id"], "version": int(delivered_ref["version"])} if delivered_ref else None),
                "runtime_policy": {"cost_gates_enabled": self.cost_gates_enabled},
                "tasks": all_records(self.store, project_id, "task"),
                "runs": all_records(self.store, project_id, "run"),
                "queue": all_records(self.store, project_id, "queued_request"),
                "artifacts": all_records(self.store, project_id, "artifact"),
                "artifact_states": all_records(self.store, project_id, "artifact_state"),
                "conversation_controls": [self._control(project_id, c["id"]) for c in all_records(self.store, project_id, "conversation")],
                "pending_user_items": [item for t in all_records(self.store, project_id, "task")
                                       for item in self._task_data(t).get("pending_user_items", []) if item["state"] == "open"]}

    def _transition(self, task, state, reason=None, source=None):
        old = task["state"]
        changed = update(self.store, task, state=state, pause_reason=reason,
                         current_run_id=task["current_run_id"] if state in ("running", "stopping") else None)
        event = self._event(task["project_id"], "task.transitioned", {"object_id": task["id"],
            "from_state": old, "to_state": state, "reason": reason, "source_message_id": source,
            "checkpoint_id": changed["latest_checkpoint_id"]}, conversation=task["conversation_id"], task=task["id"], source=source)
        if state in ("paused", "stopping", "stopped", "failed"):
            self._hold(changed, reason or ("user_stop" if state in ("stopping", "stopped") else "task_failed"), event["id"])
        return changed

    def _hold(self, task, reason, event_id):
        if not self.cost_gates_enabled and reason in ('cost_limit', 'usage_uncertain'):
            return
        mapped = {"cost_limit": "budget_limit", "active_time_limit": "budget_limit", "turn_limit": "budget_limit",
                  "retry_exhausted": "retry_limit", "repair_exhausted": "repair_limit"}.get(reason, reason)
        allowed = {"user_stop", "budget_limit", "retry_limit", "repair_limit", "configuration_error",
                   "operation_uncertain", "usage_uncertain", "dependency_changed", "recovery_exhausted",
                   "checkpoint_invalid", "child_blocked", "task_failed"}
        mapped = mapped if mapped in allowed else "configuration_error"
        gate = self._control(task["project_id"], task["conversation_id"])
        gate["holds"] = [h for h in gate["holds"] if h["task_id"] != task["id"]] + [{"task_id": task["id"], "reason_code": mapped, "basis_event_id": event_id}]
        gate["row_version"] += 1
        self._save_projection(task["project_id"], "conversation_control", task["conversation_id"], gate)

    def resume_queue(self, project_id, conversation_id=None):
        conversations = [self.store.get(conversation_id, project_id=project_id)] if conversation_id else all_records(self.store, project_id, "conversation")
        with self.store.transaction():
            for conv in conversations:
                self.store.advisory_lock(f"{project_id}:conversation:{conv['id']}")
                gate = self._control(project_id, conv["id"])
                old = gate["holds"]
                gate.update(holds=[], row_version=gate["row_version"] + 1)
                self._save_projection(project_id, "conversation_control", conv["id"], gate)
                self._event(project_id, "queue.gate_changed", {"holds": [], "released_holds": old, "explicit": True}, conversation=conv["id"])
                for q in all_records(self.store, project_id, "queued_request", conversation_id=conv["id"]):
                    if q["state"] == "blocked" and q["blocked_reason"] == "queue_hold":
                        update(self.store, q, state="pending", blocked_reason=None)
        self._wake.set()
        return {"resumed": [c["id"] for c in conversations]}

    def _single_queue_release_valid(self, queued):
        data = self._projection(queued['project_id'], 'queue_context', queued['id'])
        permit = data.get('single_release')
        if not permit or permit.get('queue_id') != queued['id']:
            return False
        gate = self._control(queued['project_id'], queued['conversation_id'])
        if permit.get('basis_event_ids') != sorted(h['basis_event_id'] for h in gate['holds']):
            return False
        from .actions import ActionService
        service = ActionService(self)
        tasks = service._tasks(queued['project_id'], queued['conversation_id'])
        calls = service._calls(queued['project_id'], tasks)
        remaining = [t for t in tasks if t['id'] != data.get('coordinator_task_id')]
        return not service._remote_block(remaining, calls) and not (self.cost_gates_enabled and any(service._unknown_cost(c) for c in calls))

    def control_task(self, project_id, task_id, action, additional_seconds=None, additional_cost=None):
        with self.store.transaction():
            task = self.store.get(task_id, project_id=project_id)
            if not task or task["record_type"] != "task":
                raise WorkflowBlocked("task_not_found")
            self.store.advisory_lock(f"{project_id}:conversation:{task['conversation_id']}")
            descendants = {task_id}
            tasks = all_records(self.store, project_id, "task")
            while True:
                expanded = descendants | {t["id"] for t in tasks if t["parent_task_id"] in descendants}
                if expanded == descendants:
                    break
                descendants = expanded
            if action in ("stop", "interrupt"):
                stop_command_id = str(uuid4())
                for candidate in tasks:
                    if candidate["id"] not in descendants or candidate["state"] in ("succeeded", "failed", "stopped"):
                        continue
                    active = candidate["current_run_id"]
                    state = "stopping" if active else "stopped"
                    event = self._event(project_id, "control.stop_requested", {"task_id": candidate["id"], "explicit": True,
                        "stop_command_id": stop_command_id}, conversation=task["conversation_id"], task=candidate["id"])
                    data = self._task_data(candidate)
                    data["stop_command_id"] = stop_command_id
                    data["stop_previous_state"] = candidate["state"]
                    fixed = self.store.get(self.store.get(active, project_id=project_id)["config_version_id"], project_id=project_id)["values"] if active else self.config_service.values(project_id, "coordinator")[0]
                    data["stop"] = {"requested": True, "requested_event_id": event["id"],
                                    "deadline_at": _after(self.store.now(), fixed["runtime"]["stop_grace_seconds"]), "task_ids": sorted(descendants)}
                    self._save_task_data(candidate, data)
                    self._transition(candidate, state, "user_stop")
                    if active in self._calls:
                        self._calls[active].cancel()
            elif action in ("continue", "resume"):
                if self._task_data(task).get('superseded_by_task_id'):
                    raise WorkflowBlocked('task_superseded')
                if task["state"] not in ("paused", "stopped"):
                    raise WorkflowBlocked("invalid_state")
                if task["pause_reason"] in ("operation_uncertain", "checkpoint_invalid") or (self.cost_gates_enabled and task["pause_reason"] == "usage_uncertain"):
                    raise WorkflowBlocked(task["pause_reason"])
                if (additional_seconds is not None and additional_seconds < 0) or (additional_cost is not None and Decimal(str(additional_cost)) < 0):
                    raise WorkflowBlocked("invalid_budget_increment")
                budget_root = self.store.get(task["budget_root_task_id"], project_id=project_id)
                limits = deepcopy(budget_root["budget"])
                limits["max_active_seconds"] = (limits["max_active_seconds"] or 0) + (additional_seconds if additional_seconds is not None else 3600)
                if self.cost_gates_enabled:
                    amount = Decimal(limits["max_cost"]["amount"] if limits["max_cost"] else "0")
                    amount += Decimal(str(additional_cost if additional_cost is not None else 20))
                    limits["max_cost"] = {"amount": str(amount), "currency": "USD"}
                update(self.store, budget_root, budget=limits)
                task = self.store.get(task_id, project_id=project_id)
                data = self._task_data(task)
                continued_stop_command = data.get("stop_command_id") if data.get("stop", {}).get("requested") else None
                data["stop"] = {"requested": False, "requested_event_id": None, "deadline_at": None, "task_ids": []}
                data["manual_continuation"] = True
                if task["pause_reason"] == "repair_exhausted":
                    data["additional_repair_rounds"] = data.get("additional_repair_rounds", 0) + 2
                data.pop("remaining_turns", None)
                data.pop("next_retry_at", None)
                previous_run = data.get("current_run_id")
                saved = self._saved_output(task, previous_run)
                if data.get("source_window_state"):
                    # A deliberate continuation may adopt newly published
                    # window/model settings. Completed windows retain their
                    # original Run snapshots and absolute UTF-16 boundaries.
                    data.pop("config_version_id", None)
                    data.pop("resume_saved_result", None)
                    saved = None
                if data.get("resume_saved_result") and self._output_rejected(task, data["resume_saved_result"]["source_run_id"]):
                    data.pop("resume_saved_result")
                saved_result_kind = saved.get("result_kind") if isinstance(saved, dict) else "ready" if isinstance(saved, str) and saved.strip() else None
                if saved_result_kind in ("ready", "needs_input"):
                    if data.get("coordinator") and not data.get("chapter_planning"):
                        queue_data = self._projection(project_id, "queue_context", data["queue_id"])
                        queue_data["recovered_output"] = saved
                        self._save_projection(project_id, "queue_context", data["queue_id"], queue_data)
                    else:
                        data["resume_saved_result"] = {"source_run_id": previous_run, "output": saved}
                self._save_task_data(task, data)
                self._transition(task, "queued")
                resumed = {task_id}
                for descendant in tasks:
                    if descendant["id"] not in descendants - {task_id}:
                        continue
                    child_data = self._task_data(descendant)
                    prior_run = child_data.get("current_run_id")
                    saved_child = self._saved_output(descendant, prior_run)
                    if child_data.get("resume_saved_result") and self._output_rejected(descendant, child_data["resume_saved_result"]["source_run_id"]):
                        child_data.pop("resume_saved_result")
                    resumable_pause = descendant["state"] == "paused" and (
                        descendant["pause_reason"] in ("cost_limit", "active_time_limit", "turn_limit", "child_blocked", "repair_exhausted")
                        or (child_data.get("step1_view") and descendant["pause_reason"] not in ("operation_uncertain", "checkpoint_invalid", "dependency_changed"))
                        or (not self.cost_gates_enabled and descendant["pause_reason"] == 'usage_uncertain'))
                    # A parent-owned batch may have a fully persisted model result
                    # awaiting corrected evidence validation. Explicit continuation
                    # can recheck it without dispatching another model request.
                    recheck_batch = (descendant["state"] == "paused" and descendant["parent_task_id"] == task_id
                        and child_data.get("batch_owned") and saved_child and (
                            (saved_child.get("result_kind") == "ready" and descendant["pause_reason"] in
                             ("evidence_pointer_missing", "evidence_pointer_not_concrete", "evidence_item_ambiguous_or_missing"))
                            or (saved_child.get("result_kind") == "needs_input" and descendant["pause_reason"] == "batch_needs_input")))
                    if descendant["state"] != "stopped" and not resumable_pause and not recheck_batch:
                        continue
                    previous_state = child_data.get("stop_previous_state", "queued")
                    child_data["stop"] = {"requested": False, "requested_event_id": None, "deadline_at": None, "task_ids": []}
                    child_data["manual_continuation"] = True
                    if descendant["pause_reason"] == "repair_exhausted":
                        child_data["additional_repair_rounds"] = child_data.get("additional_repair_rounds", 0) + 2
                    if child_data.get("step1_view") and child_data.get("source_window_state"):
                        # Resuming a source-view branch may adopt the latest
                        # model and window settings while retaining its
                        # already committed UTF-16 prefix.
                        child_data.pop("config_version_id", None)
                        child_data.pop("resume_saved_result", None)
                        saved_child = None
                    if (saved_child and not child_data.get("coordinator")
                            and (child_data.get("stage") != 2 or isinstance(saved_child, str))):
                        child_data["resume_saved_result"] = {"source_run_id": prior_run, "output": saved_child}
                    self._save_task_data(descendant, child_data)
                    self._transition(self.store.get(descendant["id"], project_id=project_id), "waiting_user" if previous_state == "waiting_user" else "queued")
                    resumed.add(descendant["id"])
                ancestor_id = task["parent_task_id"]
                while ancestor_id:
                    ancestor = self.store.get(ancestor_id, project_id=project_id)
                    if ancestor["state"] == "paused" and ancestor["pause_reason"] == "child_blocked":
                        self._transition(ancestor, "queued")
                        resumed.add(ancestor_id)
                    ancestor_id = ancestor["parent_task_id"]
                gate = self._control(project_id, task["conversation_id"])
                released_summary_holds = [h for h in gate["holds"] if h["task_id"] not in resumed
                    and self._same_stopped_failed_summary(project_id, h, descendants - {task_id}, continued_stop_command)]
                gate["holds"] = [h for h in gate["holds"] if h["task_id"] not in resumed and h not in released_summary_holds]
                if released_summary_holds:
                    gate["row_version"] += 1
                    self._event(project_id, "queue.gate_changed", {"holds": gate["holds"],
                        "released_holds": released_summary_holds, "reason": "parent_continued_same_stop_command",
                        "stop_command_id": continued_stop_command, "resumed_task_id": task_id},
                        conversation=task["conversation_id"], task=task_id)
                self._save_projection(project_id, "conversation_control", task["conversation_id"], gate)
                for queued in all_records(self.store, project_id, "queued_request", conversation_id=task["conversation_id"]):
                    if queued["state"] == "blocked" and queued["blocked_reason"] == "queue_hold" and self._projection(project_id, "queue_context", queued["id"]).get("coordinator_task_id") in resumed:
                        update(self.store, queued, state="pending", blocked_reason=None)
                self._event(project_id, "control.continued", {"task_id": task_id, "budget": limits}, conversation=task["conversation_id"], task=task_id)
            else:
                raise WorkflowBlocked("unknown_control_action")
        self._wake.set()
        if task["conversation_id"] in self._heartbeat_events:
            self._heartbeat_events[task["conversation_id"]].set()
        return self.store.get(task_id, project_id=project_id)

    def _same_stopped_failed_summary(self, project, hold, descendants, command_id):
        if not command_id or hold["reason_code"] != "user_stop" or hold["task_id"] not in descendants:
            return False
        child = self.store.get(hold["task_id"], project_id=project)
        data = self._task_data(child)
        if (child["state"] != "failed" or child["pause_reason"] != "summary_failed"
            or not data.get("parent_owned") or data.get("stage") != "aux.summary"
            or data.get("stop_command_id") != command_id or not data.get("stop", {}).get("requested")):
            return False
        requested_id = data["stop"].get("requested_event_id")
        requested = self.store.get(requested_id, project_id=project) if requested_id else None
        basis = self.store.get(hold["basis_event_id"], project_id=project)
        if (not requested or requested["event_name"] != "control.stop_requested" or requested["task_id"] != child["id"]
            or requested["payload"].get("stop_command_id") != command_id
            or not basis or basis["task_id"] != child["id"] or basis["event_name"] != "task.transitioned"
            or basis["payload"].get("reason") != "user_stop" or basis["sequence"] < requested["sequence"]):
            return False
        return not any(c["state"] in ("pending", "running", "unknown")
                       for c in all_records(self.store, project, "model_call", task_id=child["id"]))

    async def start(self):
        if self._worker is None:
            self._closing = False
            self._worker = asyncio.create_task(self._loop(), name="branch-agent-worker")

    async def stop(self):
        self._closing = True
        self._wake.set()
        if self._worker:
            await self._worker
            self._worker = None
        for task in list(self._running.values()):
            task.cancel()
        if self._running:
            await asyncio.gather(*self._running.values(), return_exceptions=True)
        self._running.clear()

    async def delete_project(self, project_id, account_id):
        self._deleting_projects.add(project_id)
        self._wake.set()
        try:
            running=[]
            for conversation_id,task in list(self._running.items()):
                conversation=self.store.get(conversation_id,project_id=project_id)
                if conversation:
                    task.cancel();running.append(task);self._running.pop(conversation_id,None)
            for run_id,call in list(self._calls.items()):
                if self.store.get(run_id,project_id=project_id): call.cancel()
            if running: await asyncio.gather(*running,return_exceptions=True)
            return self.store.delete_project(project_id,account_id)
        finally:
            self._deleting_projects.discard(project_id)

    async def _loop(self):
        while not self._closing:
            try:
                for key, running in list(self._running.items()):
                    if running.done():
                        self._running.pop(key)
                        if not running.cancelled() and running.exception():
                            LOG.error("conversation tick failed: %s", running.exception())
                conversations = self.store.scan("conversation", limit=10000, filters={"state": "active"})
                active_projects={p["id"] for p in self.store.scan("project",limit=10000,filters={"state":"active"})}
                for conv in conversations:
                    if conv["project_id"] not in active_projects or conv["project_id"] in self._deleting_projects:
                        continue
                    key = conv["id"]
                    if key not in self._running:
                        self._running[key] = asyncio.create_task(self._tick(conv), name=f"conversation:{key}")
            except Exception:
                LOG.exception("scheduler scan failed")
            self._wake.clear()
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=0.5)
            except asyncio.TimeoutError:
                pass

    async def tick(self, project_id, conversation_id):
        """One deterministic scheduling step, also used by integration tests."""
        return await self._tick(self.store.get(conversation_id, project_id=project_id))

    async def _tick(self, conversation):
        if not conversation or conversation["project_id"] in self._deleting_projects:
            return
        project, cid = conversation["project_id"], conversation["id"]
        project_record = self.store.get(project, project_id=project)
        if not project_record or project_record["state"] != "active":
            return
        # Imported-source-only conversations have no work to recover or dispatch.
        # Avoid resolving the full configuration and taking a lease on every idle poll.
        if not self.store.list(project,"task",limit=1,filters={"conversation_id":cid}) and not self.store.list(project,"queued_request",limit=1,filters={"conversation_id":cid,"state":"pending"}):
            return
        with self.store.transaction():
            self.store.advisory_lock(f"{project}:conversation:{cid}")
            gate = self._control(project, cid)
            now = self.store.now()
            if gate["lease_owner"] and gate["lease_expires_at"] and gate["lease_expires_at"] > now:
                return
            runtime = self.config_service.values(project, "coordinator")[0]["runtime"]
            gate.update(lease_owner=self.owner, lease_expires_at=_after(now, runtime["lease_seconds"]), fencing_token=gate["fencing_token"] + 1, runtime=runtime)
            token = gate["fencing_token"]
            self._save_projection(project, "conversation_control", cid, gate)
        self._heartbeat_events[cid] = asyncio.Event()
        heartbeat = asyncio.create_task(self._heartbeat(project, cid, token))
        try:
            await self._recover(project, cid)
            with self.store.transaction():
                self.store.advisory_lock(f"{project}:conversation:{cid}")
                tasks = all_records(self.store, project, "task", conversation_id=cid)
                for candidate in tasks:
                    if candidate["state"] in ("queued", "running") and not self._task_data(candidate).get("work_key") and not self._task_data(candidate).get("parent_owned"):
                        self._transition(candidate, "paused", "checkpoint_invalid")
                tasks = all_records(self.store, project, "task", conversation_id=cid)
                queued = sorted([t for t in tasks if (t["state"] == "queued" or (t["state"] == "running" and t["current_run_id"] is None)) and not self._task_data(t).get("is_workflow") and not self._task_data(t).get("parent_owned") and not self._task_data(t).get("batch_owned") and (not self._task_data(t).get("coordinator") or self._task_data(t).get("chapter_planning"))], key=lambda t: t["created_at"])
                executable = next((t for t in queued if self._ancestors_allow(t)
                                   and (not self._task_data(t).get("next_retry_at") or self._task_data(t)["next_retry_at"] <= self.store.now())), None)
                if executable:
                    selected = ("task", executable)
                else:
                    # Root workflows waiting for child completion may advance without a model call.
                    for root in [t for t in tasks if self._task_data(t).get("is_workflow") and t["state"] in ("queued", "running", "waiting_user")]:
                        if not self._task_data(root).get("manager_controlled"):
                            self._advance_root(root)
                    requests = sorted(all_records(self.store, project, "queued_request", conversation_id=cid), key=lambda q: q["sequence"])
                    gate = self._control(project, cid)
                    hold_times = [self.store.get(h["basis_event_id"], project_id=project)["created_at"] for h in self._effective_holds(project, gate)]
                    if hold_times:
                        for request in requests:
                            if request["state"] == "pending" and request["mode"] == "queue" and request["created_at"] <= max(hold_times) and not self._single_queue_release_valid(request):
                                update(self.store, request, state="blocked", blocked_reason="queue_hold")
                        requests = sorted(all_records(self.store, project, "queued_request", conversation_id=cid), key=lambda q: q["sequence"])
                    pending = next((q for q in requests if q["state"] == "pending" and q["mode"] == "queue"
                                    and self._projection(project, "queue_context", q["id"]).get("request_hash")), None)
                    selected = ("queue", pending) if pending else None
            if selected:
                if selected[0] == "task":
                    await self._execute(selected[1], token)
                else:
                    await self._coordinate(selected[1], token)
        finally:
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)
            self._heartbeat_events.pop(cid, None)
            with self.store.transaction():
                self.store.advisory_lock(f"{project}:conversation:{cid}")
                gate = self._control(project, cid)
                if gate["lease_owner"] == self.owner and gate["fencing_token"] == token:
                    gate.update(lease_owner=None, lease_expires_at=None)
                    self._save_projection(project, "conversation_control", cid, gate)

    async def _heartbeat(self, project, conversation, token):
        while True:
            gate = self._control(project, conversation)
            active = [r for r in all_records(self.store, project, "run", lease_owner=self.owner)
                      if r["state"] in ("running", "stopping") and self.store.get(r["task_id"], project_id=project)["conversation_id"] == conversation]
            settings = {r["id"]: self.store.get(r["config_version_id"], project_id=project)["values"]["runtime"] for r in active}
            delay = min([gate.get("runtime", {}).get("heartbeat_seconds", 15)] + [v["heartbeat_seconds"] for v in settings.values()])
            wake = self._heartbeat_events[conversation]
            wake.clear()
            try:
                await asyncio.wait_for(wake.wait(), timeout=delay)
            except asyncio.TimeoutError:
                pass
            with self.store.transaction():
                self.store.advisory_lock(f"{project}:conversation:{conversation}")
                gate = self._control(project, conversation)
                if gate["lease_owner"] != self.owner or gate["fencing_token"] != token:
                    return
                now = self.store.now()
                gate["lease_expires_at"] = _after(now, gate.get("runtime", {}).get("lease_seconds", 60))
                self._save_projection(project, "conversation_control", conversation, gate)
                for run in all_records(self.store, project, "run", lease_owner=self.owner):
                    task = self.store.get(run["task_id"], project_id=project)
                    if task["conversation_id"] != conversation or run["state"] not in ("running", "stopping"):
                        continue
                    control = self._task_data(task).get("stop", {})
                    session = self.store.get(run["session_id"], project_id=project)
                    if control.get("requested") and control.get("deadline_at") and control["deadline_at"] <= now:
                        for call in all_records(self.store, project, "model_call", run_id=run["id"]):
                            if call["state"] in ("pending", "running"):
                                update(self.store, call, state="unknown")
                        update(self.store, run, state="stopped", finished_at=now, lease_owner=None, lease_expires_at=None, fencing_token=run["fencing_token"] + 1)
                        update(self.store, session, lease_owner=None, lease_expires_at=None, fencing_token=session["fencing_token"] + 1)
                        self._transition(task, "stopped", "user_stop")
                        self._event(project, "control.stop_fenced", {"run_id": run["id"], "remote_outcome": "unknown"}, conversation=conversation, task=task["id"], run=run["id"])
                        if run["id"] in self._calls: self._calls[run["id"]].cancel()
                        forced = self._forced_stops.get(run["id"])
                        if forced is not None and not forced.done(): forced.set_result("user_stop")
                        continue
                    runtime = self.store.get(run["config_version_id"], project_id=project)["values"]["runtime"]
                    expires = _after(now, runtime["lease_seconds"])
                    update(self.store, run, lease_expires_at=expires)
                    if session["lease_owner"] == self.owner:
                        update(self.store, session, lease_expires_at=expires)

    def _ancestors_allow(self, task):
        current = task
        while current:
            data = self._task_data(current)
            if data.get('superseded_by_task_id') or data.get("stop", {}).get("requested") or current["state"] in ("paused", "stopping", "stopped", "failed"):
                return False
            current = self.store.get(current["parent_task_id"], project_id=task["project_id"]) if current["parent_task_id"] else None
        return True

    def _new_task(self, project, conversation, message, intent, stage=None, chapter=None, parent=None, **data):
        task_id = str(uuid4())
        values, _ = self.config_service.values(project, f"step{stage}" if stage else "coordinator")
        limits = values.get("task", {})
        if not self.cost_gates_enabled:
            limits = {**limits, 'max_cost': None, 'disabled_limits': list(set(limits.get('disabled_limits', [])) | {'cost'})}
        task = self.store.put(new_record("task", project, id=task_id, conversation_id=conversation,
            parent_task_id=parent["id"] if parent else None,
            budget_root_task_id=parent["budget_root_task_id"] if parent else task_id,
            requested_by_message_id=message["id"], intent=intent,
            scope=scope(stage=stage, chapter_id=chapter, description=data.get("request", intent)[:500]),
            state="queued", pause_reason=None, budget=budget(**limits), usage=usage(),
            repair_rounds_used=0, current_run_id=None, latest_checkpoint_id=None))
        data.update(stage=stage, chapter_id=chapter, workflow_root_task_id=parent["id"] if parent else task_id,
                    work_key=data.get("work_key", f"{task_id}:{stage}:{chapter or '-'}"),
                    pending_user_items=[], recovery_attempts_used=0,
                    stop={"requested": False, "requested_event_id": None, "deadline_at": None, "task_ids": []})
        self._save_task_data(task, data)
        self._event(project, "task.transitioned", {"object_id": task_id, "from_state": None, "to_state": "queued", "reason": None, "source_message_id": message["id"], "checkpoint_id": None}, conversation=conversation, task=task_id, source=message["id"])
        return task

    def _dispatch(self, root, stage, chapter=None, request=None, regenerate=False):
        self._assert_authorized_source(root)
        data = self._task_data(root)
        generation = data.get("generation", 0)
        if regenerate:
            generation += 1
            data["generation"] = generation
            self._save_task_data(root, data)
        key = f"{root['id']}:{stage}:{chapter or '-'}:{generation}"
        dispatch = self._projection(root["project_id"], "work_dispatch", key)
        if dispatch.get("task_id"):
            return self.store.get(dispatch["task_id"], project_id=root["project_id"])
        message = self.store.get(root["requested_by_message_id"], project_id=root["project_id"])
        task = self._new_task(root["project_id"], root["conversation_id"], message,
            "modify" if regenerate else "generate", stage, chapter, parent=root,
            request=request or data.get("request", "继续已授权改编"), work_key=key)
        self._save_projection(root["project_id"], "work_dispatch", key, {"task_id": task["id"]})
        return task

    def _advance_root(self, root):
        root = self.store.get(root["id"], project_id=root["project_id"])
        if not self._ancestors_allow(root):
            return
        try:
            self._assert_authorized_source(root)
        except WorkflowBlocked:
            self._transition(root, 'paused', 'dependency_changed')
            return
        data = self._task_data(root)
        children = [t for t in all_records(self.store, root["project_id"], "task", parent_task_id=root["id"])
                    if not self._task_data(t).get('superseded_by_task_id')]
        for audit in children:
            audit_data = self._task_data(audit)
            repair_ids = audit_data.get("repair_task_ids", [])
            if audit["state"] == "waiting_user" and repair_ids and all(self.store.get(i, project_id=root["project_id"])["state"] == "succeeded" for i in repair_ids):
                audit_data["repair_task_ids"] = []
                self._save_task_data(audit, audit_data)
                self._transition(audit, "queued")
        children = [t for t in all_records(self.store, root["project_id"], "task", parent_task_id=root["id"])
                    if not self._task_data(t).get('superseded_by_task_id')]
        blocked = next((t for t in children if t["state"] in ("paused", "stopped", "stopping", "failed")), None)
        if blocked:
            self._transition(root, "paused", "child_blocked")
            return
        active = next((t for t in children if t["state"] in ("queued", "running", "waiting_user")), None)
        if active:
            state = "waiting_user" if active["state"] == "waiting_user" else "running"
            if root["state"] != state:
                self._transition(root, state)
            return
        stages = data.get("stages", list(range(1, 12)))
        for stage in stages:
            if stage in (9, 10):
                continue
            if stage == 11:
                break
            # Step5 and Step6 intentionally share the same artifact kind.  A
            # resolved game_event_view therefore cannot prove that Step6 has
            # run; require a successful stage-6 child before advancing.
            if stage == 6 and not any(t["scope"].get("stage") == 6 and t["state"] == "succeeded"
                                     for t in children):
                self._dispatch(root, stage)
                return
            if (data.get('fresh_start') or stage in (3, 4)) and not any(
                    t['scope']['stage'] == stage and t['state'] == 'succeeded' for t in children):
                self._dispatch(root, stage)
                return
            try:
                for output_kind in STAGE_OUTPUTS.get(stage, (STAGES[stage],)):
                    self.workflow.resolve(root["project_id"], output_kind)
            except WorkflowBlocked:
                self._dispatch(root, stage)
                return
        chapter_ids = data.get("chapter_ids", [])
        if any(s in stages for s in (9, 10, 11)) and not chapter_ids:
            if not data.get("chapter_plan_requested"):
                original = self.store.get(data.get('chapter_plan_source_message_id') or root["requested_by_message_id"], project_id=root["project_id"])
                self._new_task(root["project_id"], root["conversation_id"], original, "query", parent=root,
                               coordinator=True, chapter_planning=True, request="依据已确认事件、结局路线和玩家画像提出完整有序章节划分，等待用户确认。\n" + data.get('request', ''))
                data["chapter_plan_requested"] = True
                self._save_task_data(root, data)
            if root["state"] != "waiting_user":
                self._transition(root, "waiting_user")
            return
        pairs = [(s, c) for c in chapter_ids for s in (9, 10) if s in stages]
        if data.get("chapter_order_mode") == "design_first":
            pairs = [(s, c) for s in (9, 10) if s in stages for c in chapter_ids]
        for stage, chapter in pairs:
            if data.get('fresh_start') and not any(t['scope']['stage'] == stage and chapter in t['scope']['chapter_ids'] and t['state'] == 'succeeded' for t in children):
                self._dispatch(root, stage, chapter)
                return
            try:
                self.workflow.resolve(root["project_id"], STAGES[stage], chapter)
            except WorkflowBlocked:
                self._dispatch(root, stage, chapter)
                return
        if 11 in stages and not any(t["scope"]["stage"] == 11 and t["state"] == "succeeded" for t in children):
            self._dispatch(root, 11)
            return
        if root["state"] != "succeeded":
            self._transition(root, "succeeded")

    def _wait_item(self, task, kind, message, description, targets=None, question_id=None):
        return {"id": str(uuid4()), "task_id": task["id"], "kind": kind, "question_id": question_id,
                "description": description, "presented_message_ids": [message["id"]], "targets": targets or [],
                "state": "open", "answer_message_ids": [], "replaced_by_id": None}

    def _assert_authorized_source(self, task):
        current = task
        while current:
            data = self._task_data(current)
            if data.get('source_ref'):
                source = self.workflow.resolve(task['project_id'], 'source_text')
                if data['source_ref'] != ref(source):
                    raise WorkflowBlocked('dependency_changed', {'authorized_source_ref': data['source_ref'], 'current_source_ref': ref(source)})
            current = self.store.get(current['parent_task_id'], project_id=task['project_id']) if current['parent_task_id'] else None

    def _session(self, task, stage, values=None, key_override=None):
        sharing = (values or {}).get("context", {}).get("session_sharing", {})
        key = key_override or (f"aux:{task['id']}:{stage}" if isinstance(stage, str) and stage.startswith("aux.") else session_key(
            stage, self._task_data(task).get("chapter_id"), sharing))
        sessions = all_records(self.store, task["project_id"], "work_session", conversation_id=task["conversation_id"], session_key=key)
        if sessions:
            return max(sessions, key=lambda s: s["generation"])
        return self.store.put(new_record("work_session", task["project_id"], conversation_id=task["conversation_id"],
            session_key=key, scope=task["scope"], state="active", generation=1, latest_summary_ref=None,
            last_item_seq=0, lease_owner=None, lease_expires_at=None, fencing_token=0))

    def _start_run(self, task, stage, materials, *, recovery=False, session_key_override=None,
                   fresh_allowance=False):
        data = self._task_data(task)
        if fresh_allowance:
            data.pop("remaining_turns", None)
            data.pop("allowance", None)
            data.pop("root_run_id", None)
            data.pop("recovery_run_ids", None)
        config = self.store.get(data["config_version_id"], project_id=task["project_id"]) if data.get("config_version_id") else self.config_service.resolve(task["project_id"], stage if isinstance(stage, str) else f"step{stage}")
        values = config["values"]
        if (isinstance(stage, int) and not recovery and materials
                and not data.get("batch_coverage_ready")):
            selected = values.get("context", {}).get("stage_inputs", {}).get(f"step{stage}")
            if selected is not None:
                configured = self.workflow.materials(task["project_id"], stage, data.get("chapter_id"), selected)
                # Keep non-business runtime projections explicitly prepared by
                # the caller; business artifacts come from the selected list.
                extras = [material for material in materials if material.get("builtin") or material.get("program_only")]
                identities = {(m.get("ref", {}).get("record_id"), m.get("ref", {}).get("version")) for m in configured}
                configured.extend(m for m in extras if (m.get("ref", {}).get("record_id"),
                                                        m.get("ref", {}).get("version")) not in identities)
                materials[:] = configured
        session = self._session(task, stage, values, key_override=session_key_override)
        now = self.store.now()
        if session["lease_owner"] and session["lease_expires_at"] and session["lease_expires_at"] > now:
            raise WorkflowBlocked("session_leased")
        token = session["fencing_token"] + 1
        session = update(self.store, session, lease_owner=self.owner, lease_expires_at=_after(now, _cfg(values, "runtime.lease_seconds", 60)), fencing_token=token)
        turns = data.get("remaining_turns", _cfg(values, "run.max_turns", 10))
        if turns <= 0 and not recovery:
            raise WorkflowBlocked("turn_limit")
        run = self.store.put(new_record("run", task["project_id"], task_id=task["id"],
            agent_key="runtime_recovery" if recovery else stage_agent(
                stage if isinstance(stage,str) else f"step{stage}",values),
            session_id=session["id"], state="running", config_version_id=config["id"],
            # Step6 reads the Step5 game_event_view but writes a new version
            # of that same artifact.  Do not make the new version depend on
            # the predecessor version, or replacing it invalidates itself.
            input_refs=[m["ref"] for m in materials if m.get("ref") and not m.get("program_only")
                        and not (stage in (6, "step6") and m.get("schema_id") == "game_event_view")],
            started_at=now, finished_at=None,
            max_turns=0 if recovery else turns, model_turns_used=0, latest_checkpoint_id=None,
            error=None, lease_owner=self.owner, lease_expires_at=session["lease_expires_at"], fencing_token=token,
            execution_kind="recovery" if recovery else "runner"))
        task = update(self.store, task, state="running", current_run_id=run["id"], pause_reason=None)
        data.update(config_version_id=config["id"], current_run_id=run["id"])
        event = self._event(task["project_id"], "run.transitioned", {"object_id": run["id"], "from_state": "created", "to_state": "running", "reason": None, "checkpoint_id": None}, conversation=task["conversation_id"], task=task["id"], run=run["id"])
        if not data.get("allowance") or data.pop("manual_continuation", False) or (not recovery and "remaining_turns" not in data):
            data["allowance"] = {"allowance_id": str(uuid4()), "limit": max(turns, 1), "used": 0, "reserved_operation_ids": [], "source_event_id": event["id"]}
            data["root_run_id"] = run["id"]
            data["recovery_run_ids"] = []
        data.setdefault("root_run_id", run["id"])
        self._save_task_data(task, data)
        self._checkpoint(task, run, "dispatch_agent")
        if task["conversation_id"] in self._heartbeat_events:
            self._heartbeat_events[task["conversation_id"]].set()
        return task, run, session, values

    def _checkpoint(self, task, run, action):
        task = self.store.get(task["id"], project_id=task["project_id"])
        run = self.store.get(run["id"], project_id=task["project_id"])
        data = self._task_data(task)
        chain = set(data.get("recovery_run_ids", [])) | {run["id"]}
        calls = [c for c in all_records(self.store, task["project_id"], "model_call", task_id=task["id"]) if c["run_id"] in chain]
        operation_ids = {c["operation_id"] for c in calls}
        if len(operation_ids) > data["allowance"]["limit"]:
            raise WorkflowBlocked("checkpoint_invalid", {"reason": "turn allowance exceeded"})
        data["allowance"]["used"] = len(operation_ids)
        data["allowance"]["reserved_operation_ids"] = sorted({c["operation_id"] for c in calls if c["state"] in ("pending", "running", "unknown")})
        self._save_task_data(task, data)
        gate = self._control(task["project_id"], task["conversation_id"])
        session = self.store.get(run["session_id"], project_id=task["project_id"])
        state = {"schema_version": "1.0.0", "workflow_root_task_id": data.get("workflow_root_task_id", task["id"]),
            "work_key": data["work_key"], "next_action": action,
            "continuation": {"root_run_id": data["root_run_id"], "previous_run_id": data.get("previous_run_id"),
                "recovery_attempts_used": data.get("recovery_attempts_used", 0), "max_auto_recoveries": 3,
                "last_recovery_event_id": data.get("last_recovery_event_id"), "next_retry_at": data.get("next_retry_at")},
            "turn_allowance": data["allowance"], "retry_limits": [], "repair_round_limit": data.get("repair_round_limit", 2),
            "pending_control_request_ids": [q["id"] for q in all_records(self.store, task["project_id"], "queued_request", target_run_id=run["id"]) if q["state"] == "pending"],
            "pending_user_items": data.get("pending_user_items", []),
            "queue_gate": {"conversation_id": task["conversation_id"], "row_version": gate["row_version"], "holds": gate["holds"]},
            "stop": data["stop"], "operation_reconciliation": data.get("operation_reconciliation", []),
            "last_applied_event_seq": gate.get("last_applied_event_seq", 0)}
        cp = self.store.put(new_record("checkpoint", task["project_id"], task_id=task["id"], run_id=run["id"],
            cursor={"kind": "await_user" if action == "await_user" else "commit_artifact" if action in ("process_model_result", "commit_artifact", "deliver") else "dispatch_agent",
                    "stage": task["scope"]["stage"], "chapter_id": data.get("chapter_id"), "batch_key": None,
                    "handler_version": "runtime.v1", "state": state},
            session_id=session["id"], session_generation=session["generation"], config_version_id=run["config_version_id"],
            input_refs=run["input_refs"], adopted_message_ids=[task["requested_by_message_id"]], completed_operation_ids=[],
            unresolved_operation_ids=[], usage=task["usage"], repair_rounds_used=task["repair_rounds_used"], pause_reason=task["pause_reason"]))
        update(self.store, task, latest_checkpoint_id=cp["id"])
        update(self.store, run, latest_checkpoint_id=cp["id"])
        self._event(task["project_id"], "checkpoint.saved", {"checkpoint_id": cp["id"], "next_action": action}, conversation=task["conversation_id"], task=task["id"], run=run["id"])
        return cp

    def _close_run(self, task, run, state, error=None):
        current = self.store.get(run["id"], project_id=task["project_id"])
        if current["state"] in CLOSED_RUNS:
            return
        update(self.store, current, state=state, finished_at=self.store.now(), lease_owner=None, lease_expires_at=None, error=error)
        session = self.store.get(run["session_id"], project_id=task["project_id"])
        if session["fencing_token"] == run["fencing_token"] and session["lease_owner"] == self.owner:
            update(self.store, session, lease_owner=None, lease_expires_at=None)
        task = self.store.get(task["id"], project_id=task["project_id"])
        if task["current_run_id"] == run["id"]:
            update(self.store, task, current_run_id=None)
        self._event(task["project_id"], "run.transitioned", {"object_id": run["id"], "from_state": current["state"], "to_state": state, "reason": error["code"] if error else None, "checkpoint_id": task["latest_checkpoint_id"]}, conversation=task["conversation_id"], task=task["id"], run=run["id"])
        for request in all_records(self.store, task["project_id"], "queued_request", target_run_id=run["id"]):
            if request["state"] == "pending":
                update(self.store, request, state="blocked", blocked_reason="target_not_running")

    def _assert_run(self, task, run, conversation_token):
        gate = self._control(task["project_id"], task["conversation_id"])
        current = self.store.get(run["id"], project_id=task["project_id"])
        if gate["lease_owner"] != self.owner or gate["fencing_token"] != conversation_token or gate["lease_expires_at"] <= self.store.now():
            raise WorkflowBlocked("lease_lost")
        if current["lease_owner"] != self.owner or current["fencing_token"] != run["fencing_token"]:
            raise WorkflowBlocked("lease_lost")
        current_task = self.store.get(task["id"], project_id=task["project_id"])
        if not self._ancestors_allow(current_task):
            raise WorkflowBlocked("user_stop" if current_task["state"] in ("stopping", "stopped") else current_task["pause_reason"] or "task_blocked")
        return current_task

    def _budget_check(self, task):
        root = self.store.get(task["budget_root_task_id"], project_id=task["project_id"])
        members = all_records(self.store, task["project_id"], "task", budget_root_task_id=root["id"])
        members_by_id = {member["id"]: member for member in members}
        calls = [c for c in all_records(self.store, task["project_id"], "model_call") if c["task_id"] in members_by_id]
        cost = Decimal("0")
        task_data = self._task_data(task)
        current_view = task_data.get("step1_view")
        view_owner = task
        if (task_data.get('parent_owned') and task_data.get('stage') == 'aux.format_repair'
                and current_view in ('global', 'character')):
            parent = members_by_id.get(task['parent_task_id'])
            if parent and self._task_data(parent).get('step1_view') == current_view:
                view_owner = parent
        peer_parent_id = view_owner['parent_task_id']

        def live_other_view(view_task, run_id):
            if (current_view not in ("global", "character") or not view_task or not run_id
                    or view_task["parent_task_id"] != peer_parent_id
                    or view_task["state"] != "running" or view_task["current_run_id"] != run_id):
                return False
            other_view = self._task_data(view_task).get("step1_view")
            if other_view not in ("global", "character") or other_view == current_view:
                return False
            view_run = self.store.get(run_id, project_id=task["project_id"])
            return bool(view_run and view_run["task_id"] == view_task["id"]
                        and view_run["state"] == "running"
                        and view_run["lease_owner"] == self.owner
                        and view_run["lease_expires_at"]
                        and view_run["lease_expires_at"] > self.store.now())

        for call in calls:
            if call.get("state") in ("pending", "running", "unknown"):
                # Step 1's other view can have a model call in flight, including
                # its parent-owned context summary. A summary run has no lease;
                # its live parent view run owns the lease instead. Unknown calls
                # and stale or unrelated work must still block recovery.
                call_task = members_by_id[call["task_id"]]
                parallel_step1 = False
                if call["state"] in ("pending", "running"):
                    parallel_step1 = live_other_view(call_task, call["run_id"])
                    call_data = self._task_data(call_task)
                    if (not parallel_step1 and call_task["intent"] == "summarize"
                            and call_task["state"] == "running"
                            and call_task["current_run_id"] == call["run_id"]
                            and call_data.get("stage") in ("aux.summary", "aux.format_repair")
                            and call_data.get("parent_owned") is True):
                        parent = members_by_id.get(call_task["parent_task_id"])
                        summary_run = self.store.get(call["run_id"], project_id=task["project_id"])
                        parallel_step1 = bool(
                            parent and live_other_view(parent, parent["current_run_id"])
                            and summary_run and summary_run["task_id"] == call_task["id"]
                            and summary_run["state"] == "running"
                            and (call_data.get('stage') == 'aux.summary' or
                                 (summary_run['lease_owner'] == self.owner
                                  and summary_run['lease_expires_at']
                                  and summary_run['lease_expires_at'] > self.store.now())))
                if not parallel_step1:
                    raise WorkflowBlocked("operation_uncertain")
                continue
            measured = call.get("usage", {})
            amount = measured.get("reported_cost") or measured.get("estimated_cost")
            # A received incomplete/failed response can still consume tokens.
            # Keep unknown usage distinct from an unknown remote outcome.
            failure_details = (call.get("error") or {}).get("details", {})
            received_terminal = (call.get("state") == "failed" and failure_details.get("known_outcome") is True
                                 and failure_details.get("terminal_status") in ("incomplete", "failed", "completed"))
            if self.cost_gates_enabled and amount is None and (call.get("state") == "succeeded" or received_terminal) and "cost" not in root["budget"]["disabled_limits"]:
                raise WorkflowBlocked("usage_uncertain")
            if amount:
                cost += Decimal(amount["amount"])
        if self.cost_gates_enabled and "cost" not in root["budget"]["disabled_limits"] and root["budget"]["max_cost"] and cost >= Decimal(root["budget"]["max_cost"]["amount"]):
            raise WorkflowBlocked("cost_limit")
        active = sum(self._task_data(t).get("active_ms", 0) for t in members)
        if "active_seconds" not in root["budget"]["disabled_limits"] and root["budget"]["max_active_seconds"] and active >= root["budget"]["max_active_seconds"] * 1000:
            raise WorkflowBlocked("active_time_limit")

    def _refresh_usage(self, task):
        project = task["project_id"]
        root = self.store.get(task["budget_root_task_id"], project_id=project)
        tasks = all_records(self.store, project, "task", budget_root_task_id=root["id"])
        calls = all_records(self.store, project, "model_call")
        for target in {task["id"]: task, root["id"]: root}.values():
            ids = {t["id"] for t in tasks} if target["id"] == root["id"] else {target["id"]}
            selected = [c for c in calls if c["task_id"] in ids]
            measured = usage()
            for field in ("input_tokens", "output_tokens", "cached_input_tokens", "reasoning_tokens"):
                values = [c["usage"][field] for c in selected]
                measured[field] = sum(values) if all(v is not None for v in values) else None
            amounts = [c["usage"]["reported_cost"] or c["usage"]["estimated_cost"] for c in selected]
            if all(a is not None and a["currency"] == "USD" for a in amounts):
                measured["estimated_cost"] = {"amount": str(sum((Decimal(a["amount"]) for a in amounts), Decimal("0"))), "currency": "USD"}
            measured["active_ms"] = sum(self._task_data(t).get("active_ms", 0) for t in tasks if t["id"] in ids)
            versions = {c["usage"]["pricing_version"] for c in selected if c["usage"]["pricing_version"]}
            measured["pricing_version"] = ",".join(sorted(versions)) if versions else None
            live = self.store.get(target["id"], project_id=project)
            update(self.store, live, usage=measured)

    def _dependencies_valid(self, run):
        task = self.store.get(run["task_id"], project_id=run["project_id"]) if run.get("task_id") else None
        data = self._task_data(task) if task else {}
        if data.get("coordinator") and not data.get("chapter_planning"):
            # Queries and confirmations must see drafts/history. A read is not promotion;
            # Workflow.confirm independently verifies exact target and dependency state.
            return all(self.store.resolve_ref(source, run["project_id"]) for source in run["input_refs"])
        for source in run["input_refs"]:
            if not source.get("version"):
                continue
            try:
                version = self.workflow.fixed_version(run["project_id"], source)
            except WorkflowBlocked:
                continue  # Config and message refs are not artifact versions.
            state = self.workflow.state(version)
            if state and state["dependency_status"] != "valid":
                return False
            artifact = self.store.get(version["artifact_id"], project_id=run["project_id"])
            current_number = artifact["current_effective_version"]
            candidate_input = artifact["artifact_kind"] == "nexo_graph" and version["origin"] == "program"
            if current_number and current_number != version["version"] and not candidate_input:
                current = next(v for v in self.workflow.versions(run["project_id"], artifact["id"]) if v["version"] == current_number)
                if current["content_sha256"] != version["content_sha256"]:
                    return False
        return True

    def _auxiliary_active_ms(self, task):
        return sum(self._task_data(child).get("active_ms", 0)
                   for child in all_records(self.store, task["project_id"], "task", budget_root_task_id=task["budget_root_task_id"])
                   if self._task_data(child).get("parent_owned"))

    def _fixed_run_materials(self, run):
        materials = []
        event_builtins = {"graph.checked": "runtime.graph_checks", "graph.write_context_created": "runtime.graph_write_context",
                          "review.requested": "runtime.review_context", "runtime.status_prepared": "runtime.status",
                          "graph.contract_frozen": "runtime.graph_contract"}
        for source in run["input_refs"]:
            fixed = self.store.resolve_ref(source, run["project_id"])
            if fixed["record_type"] == "artifact_version":
                kind = self.store.get(fixed["artifact_id"], project_id=run["project_id"])["artifact_kind"]
                materials.append({"ref": source, "kind": kind, "schema_id": kind, "record": fixed,
                                  "content": body(self.store, fixed), "state": self.workflow.state(fixed), "required": True})
            elif fixed["record_type"] == "runtime_event":
                builtin = event_builtins.get(fixed["event_name"], "runtime.recovery")
                materials.append({"ref": source, "schema_id": builtin, "builtin": builtin, "content": fixed["payload"],
                                  "projection_pointer": "/payload", "required": True})
            else:
                materials.append({"ref": source, "schema_id": fixed["record_type"], "content": fixed, "required": False})
        return materials

    async def _invoke(self, stage, task, run, session, config, materials, message, token,
                      *, extra_tools=None, instructions_override=None, step1_window=None,
                      step1_view=None):
        async def control():
            with self.store.transaction():
                self.store.advisory_lock(f"{task['project_id']}:conversation:{task['conversation_id']}")
                self._assert_run(task, run, token)
                self._assert_authorized_source(task)
                self._budget_check(task)
                if not self._dependencies_valid(run):
                    raise WorkflowBlocked("dependency_changed")
                targets = {run["id"], *self._task_data(task).get("recovery_run_ids", [])}
                queued = [q for q in all_records(self.store, task["project_id"], "queued_request")
                          if q["state"] == "pending" and q["target_run_id"] in targets]
                return {"stop": False, "steer_messages": [self.store.get(q["source_message_id"], project_id=task["project_id"]) for q in queued], "reason": None}
        kwargs = {}
        if extra_tools is not None:
            kwargs["extra_tools"] = extra_tools
        if instructions_override is not None:
            kwargs["instructions_override"] = instructions_override
        if step1_window is not None:
            kwargs["step1_window"] = step1_window
        if step1_view is not None:
            kwargs["step1_view"] = step1_view
        if stage == "coordinator" and getattr(self.model_service, "supports_manager", False):
            def present_event_source(markdown, metadata):
                # This is a user-facing projection of a fixed source range.  It
                # belongs in conversation history, never in the Agent Session.
                with self.store.transaction():
                    self.store.advisory_lock(f"{task['project_id']}:conversation:{task['conversation_id']}")
                    self._assert_run(task, run, token)
                    shown = self._message(task["project_id"], task["conversation_id"], markdown,
                                          task=task["id"], run=run["id"])
                    self._event(task["project_id"], "event_source.presented",
                                {**metadata, "message_id": shown["id"]},
                                conversation=task["conversation_id"], task=task["id"], run=run["id"])
                    return {"message_id": shown["id"]}
            kwargs["event_source_presenter"] = present_event_source
        # Only the production model service opts into durable public streaming.
        # Test and extension model services keep their existing run contract.
        if "live_events" in inspect.signature(self.model_service.run).parameters:
            kwargs["live_events"] = True
        model = asyncio.create_task(self.model_service.run(stage if isinstance(stage, str) else f"step{stage}", task, run, session, config, materials, message, control=control, **kwargs))
        self._calls[run["id"]] = model
        forced = asyncio.get_running_loop().create_future()
        self._forced_stops[run["id"]] = forced
        started = asyncio.get_running_loop().time()
        auxiliary_before = self._auxiliary_active_ms(task)
        try:
            root = self.store.get(task["budget_root_task_id"], project_id=task["project_id"])
            members = all_records(self.store, task["project_id"], "task", budget_root_task_id=root["id"])
            used = sum(self._task_data(t).get("active_ms", 0) for t in members) / 1000
            remaining = None if "active_seconds" in root["budget"]["disabled_limits"] else (root["budget"]["max_active_seconds"] or 3600) - used
            if remaining is not None and remaining <= 0:
                raise WorkflowBlocked("active_time_limit")
            done, _ = await asyncio.wait({model, forced}, timeout=remaining, return_when=asyncio.FIRST_COMPLETED)
            if forced in done:
                raise WorkflowBlocked("user_stop")
            if model in done:
                try:
                    return model.result()
                except Exception as error:
                    issue = (getattr(error, 'details', None) or {}).get('output_failure')
                    if stage != 'aux.format_repair' and issue and issue.get('category') == 'format':
                        return await self._format_repair(task, run, config, issue, token, error)
                    raise
            raise WorkflowBlocked("active_time_limit")
        finally:
            self._calls.pop(run["id"], None)
            self._forced_stops.pop(run["id"], None)
            if not forced.done(): forced.cancel()
            if not model.done():
                model.cancel()
                # Late responses may still arrive; audit fencing rejects business writes.
                model.add_done_callback(lambda finished: None if finished.cancelled() else finished.exception())
            with self.store.transaction():
                data = self._task_data(task)
                elapsed_ms = int((asyncio.get_running_loop().time() - started) * 1000)
                auxiliary_ms = max(0, self._auxiliary_active_ms(task) - auxiliary_before)
                data["active_ms"] = data.get("active_ms", 0) + max(0, elapsed_ms - auxiliary_ms)
                self._save_task_data(task, data)
                self._refresh_usage(task)

    async def _format_repair(self, task, parent_run, config, issue, token, original_error):
        """Run a format-only Agent in its own Task/Session under the parent budget."""
        from .context import unwrap
        from .model_service import ModelRunError
        project = task['project_id']
        limit = _cfg(config, 'repair.format_max_rounds', 1)
        already_used = self._task_data(task).get('format_repair_rounds_used', 0)
        if not issue.get('history_record_id') or not issue.get('schema_id'):
            issue['category'] = 'content'
            raise original_error
        archive = self.store.get(issue['history_record_id'], project_id=project)
        from .output_repair import response_text
        raw = (response_text(unwrap(archive['content'], self.store, project))
               if archive and archive['record_type'] == 'history_record'
               and archive.get('run_id') == parent_run['id'] else None)
        if raw is None or already_used >= limit:
            issue['category'] = 'content'
            raise original_error
        schema_id = issue['schema_id']
        finding = assess_output(raw, schema_id, self.workflow.catalog, config.get('schemas'))
        if finding['category'] != 'format':
            issue.update(category=finding['category'], problems=finding['problems'])
            raise original_error
        if len(finding['problems']) > 20:
            issue['category'] = 'content'
            issue['format_repair_error'] = '校验问题超过单次定向格式修复范围'
            raise original_error
        schema = (config.get('schemas') or self.workflow.catalog.schemas)[schema_id]
        request = json.dumps({'task': 'format_only', 'source_run_id': parent_run['id'],
            'source_archive_id': archive['id'], 'candidate_sha256': issue.get('candidate_sha256'),
            'raw_output': raw, 'schema_id': schema_id, 'schema': schema,
            'validation_errors': finding['problems'],
            'response_protocol': ('hash_bound_json_pointer_patch' if finding['candidate'] is not None
                                  else 'complete_json_syntax_fix')}, ensure_ascii=False)
        failure = None
        for round_number in range(already_used, limit):
            child = child_run = None
            try:
                with self.store.transaction():
                    self.store.advisory_lock(f'{project}:conversation:{task["conversation_id"]}')
                    message = self.store.get(task['requested_by_message_id'], project_id=project)
                    child = self._new_task(project, task['conversation_id'], message, 'summarize',
                                           parent=task, request='修复固定模型候选的格式')
                    data = self._task_data(child)
                    data['stage'] = 'aux.format_repair'
                    data['parent_owned'] = True
                    if self._task_data(task).get('step1_view'):
                        data['step1_view'] = self._task_data(task)['step1_view']
                    data['config_version_id'] = self._auxiliary_config(project, parent_run['config_version_id'],
                                                                        'aux.format_repair')['id']
                    self._save_task_data(child, data)
                    parent_data = self._task_data(task)
                    parent_data['format_repair_rounds_used'] = round_number + 1
                    self._save_task_data(task, parent_data)
                    child, child_run, child_session, child_config = self._start_run(
                        child, 'aux.format_repair', [], fresh_allowance=True,
                        session_key_override=f'format:{parent_run["id"]}:{round_number}:{child["id"]}')
                reply = await self._invoke('aux.format_repair', child, child_run, child_session,
                                           child_config, [], request, token)
                fixed = apply_format_repair(raw, finding, reply)
                fixed = self.workflow.catalog.validate(schema_id, fixed, config.get('schemas'))
                with self.store.transaction():
                    self._save_projection(project, 'run_result', child_run['id'], {'output': reply})
                    self._close_run(child, child_run, 'succeeded')
                    self._transition(self.store.get(child['id'], project_id=project), 'succeeded')
                    self._event(project, 'format_repair.applied',
                        {'parent_run_id': parent_run['id'], 'repair_run_id': child_run['id'],
                         'source_archive_id': archive['id'], 'candidate_sha256': candidate_hash(fixed),
                         'round': round_number + 1}, conversation=task['conversation_id'],
                        task=task['id'], run=parent_run['id'])
                return fixed
            except (asyncio.CancelledError, WorkflowBlocked) as error:
                if child and child_run:
                    reason = getattr(error, 'reason', None) or 'user_stop'
                    state = 'paused' if reason == 'operation_uncertain' else 'stopped'
                    with self.store.transaction():
                        self._close_run(child, child_run, state,
                            {'code': reason, 'message': str(error), 'retryable': False, 'details': {}})
                        update(self.store, self.store.get(child['id'], project_id=project),
                               state=state, pause_reason=reason, current_run_id=None)
                raise
            except Exception as error:
                failure = str(error)
                uncertain = getattr(error, 'code', None) == 'operation_uncertain'
                if child and child_run:
                    with self.store.transaction():
                        state = 'paused' if uncertain else 'failed'
                        self._close_run(child, child_run, state,
                                        {'code': 'operation_uncertain' if uncertain else 'format_repair_failed',
                                         'message': failure,
                                         'retryable': False, 'details': {}})
                        current = self.store.get(child['id'], project_id=project)
                        update(self.store, current, state=state,
                               pause_reason='operation_uncertain' if uncertain else 'format_repair_failed',
                               current_run_id=None)
                if uncertain:
                    raise
                continue
        with self.store.transaction():
            self._event(project, 'format_repair.failed',
                {'parent_run_id': parent_run['id'], 'source_archive_id': archive['id'],
                 'reason': failure or 'format_repair_exhausted'}, conversation=task['conversation_id'],
                task=task['id'], run=parent_run['id'])
        # The original stage Agent, with a fresh Session, must decide any
        # unresolved or semantic changes. Never accept the repair Agent's text.
        issue['category'] = 'content'
        issue['format_repair_error'] = failure or 'format_repair_exhausted'
        raise original_error

    async def _execute(self, task, token):
        project, cid = task["project_id"], task["conversation_id"]
        try:
            self._assert_authorized_source(task)
        except WorkflowBlocked as error:
            self._fail_execution(task, None, error.reason, details=error.details)
            return
        data = self._task_data(task)
        if data.get("chapter_planning"):
            await self._plan_chapters(task, token)
            return
        if not data.get("stage"):
            self._fail_execution(task, None, "checkpoint_invalid")
            return
        stage, chapter = data["stage"], data.get("chapter_id")
        if stage == 1:
            try:
                source_version = self.workflow.resolve(project, "source_text")
                source_text = body(self.store, source_version)
                config_version = (self.store.get(data["config_version_id"], project_id=project)
                                  if data.get("config_version_id") else self.config_service.resolve(project, "step1"))
                if not data.get("config_version_id"):
                    with self.store.transaction():
                        data["config_version_id"] = config_version["id"]
                        self._save_task_data(task, data)
            except Exception as error:
                reason = getattr(error, "reason", None) or getattr(error, "code", None) or "configuration_error"
                self._fail_execution(task, None, reason, str(error), getattr(error, "details", None))
                return
            await self._drive_step1_views(task, token, source_version, source_text)
            return
        if data.get("batch_manifest_ref") and not data.get("batch_coverage_ready"):
            await self._drive_batch(task, token)
            return
        run = None
        try:
            with self.store.transaction():
                self.store.advisory_lock(f"{project}:materials")
                self.store.advisory_lock(f"{project}:conversation:{cid}")
                self._assert_authorized_source(task)
                self._budget_check(task)
                resume = data.get("resume_saved_result")
                if resume and self._output_rejected(task, resume["source_run_id"]):
                    data.pop("resume_saved_result")
                    self._save_task_data(task, data)
                    resume = None
                if resume:
                    previous_run = self.store.get(resume["source_run_id"], project_id=project)
                    if not self._dependencies_valid(previous_run):
                        raise WorkflowBlocked("dependency_changed")
                    materials = self._fixed_run_materials(previous_run)
                    if stage == 10:
                        # Legacy runs may have frozen the plan and Graph
                        # baseline as model inputs. Resume with only the
                        # fixed design and original under the current contract.
                        materials = [m for m in materials if m.get("schema_id") in ("chapter_design", "source_text")]
                    if stage == 11:
                        materials = [m for m in materials if m.get("schema_id") == "nexo_graph"]
                else:
                    if stage == 11:
                        candidate = self._assemble(task)
                        fixed = self.store.get(self._task_data(task)["config_version_id"], project_id=project)
                        if not fixed["values"]["prompts"].get("validation_enabled",
                                bool(agent_instructions("step11",fixed["values"]).strip())):
                            self._deliver_script_validated(task, candidate, fixed)
                            return
                        materials = self._review_materials(task)
                    else:
                        materials = self.workflow.materials(project, stage, chapter)
                        if stage == 10:
                            materials.extend(self._graph_write_materials(task))
                if data.get("batch_coverage_ready") and not resume:
                    from .context_batching import aggregation_materials
                    fixed = self.store.get(data["config_version_id"], project_id=project)
                    prospective = {"task_id": task["id"], "config_version_id": fixed["id"]}
                    materials = aggregation_materials(self.store, task, prospective, fixed["values"], materials, data["batch_manifest_ref"])
                    manifest_version = self.workflow.fixed_version(project, data["batch_manifest_ref"])
                    materials.append({"ref": data["batch_manifest_ref"], "schema_id": "batch_manifest", "content": body(self.store, manifest_version), "required": False})
                task, run, session, config = self._start_run(
                    task, stage, materials, recovery=bool(resume),
                    session_key_override=data.get('repair_session_key'),
                    fresh_allowance=bool(data.get('repair_session_key') and not resume))
                data = self._task_data(task)
                data["model_dispatched"] = True
                self._save_task_data(task, data)
            request_text = data.get("request", "执行当前阶段")
            if data.get('repair_brief'):
                request_text += ('\nHarness 校验修复单（候选按需通过 read_record 固定引用读取；'
                                 '不要继承失败 Run 的历史）：\n' +
                                 json.dumps(data['repair_brief'], ensure_ascii=False))
            if stage == 11:
                request_text += "\n只校验本次提供的最终 Nexo Graph。checked_scope与unchecked_scope都是仅含裸章节ID的字符串数组，不要填说明句或拼接多个ID。checked_scope只列实际完整检查的章节ID；未完整检查的章节列入unchecked_scope。程序会绑定来源和criteria_ref，并将graph_checks固定为空数组。"
            result = resume["output"] if resume else await self._invoke(stage, task, run, session, config, materials, request_text, token)
            with self.store.transaction():
                self.store.advisory_lock(f"{project}:conversation:{cid}")
                # Persist the exact model result before business effects for crash recovery.
                persisted = self._projection(project, "run_result", run["id"])
                persisted["output"] = result
                if resume:
                    persisted["source_run_id"] = resume["source_run_id"]
                self._save_projection(project, "run_result", run["id"], persisted)
                self._assert_run(task, run, token)
                self._checkpoint(task, run, "process_model_result")
            await self._consume_steers(task, run, token)
            with self.store.transaction():
                self.store.advisory_lock(f"{project}:materials")
                self.store.advisory_lock(f"{project}:conversation:{cid}")
                task = self._assert_run(task, run, token)
                self._apply_stage(task, run, result, materials)
        except asyncio.CancelledError:
            if self._closing:
                # Graceful service shutdown is not a user stop. Recovery reconciles it.
                return
            self._fail_execution(task, run, "user_stop")
        except Exception as error:
            reason = getattr(error, "reason", None) or getattr(error, "code", None) or ("active_time_limit" if isinstance(error, asyncio.TimeoutError) else "configuration_error")
            repairable = isinstance(error, GraphError) or type(error).__name__ == "ValidationError" or reason in ("ModelBehaviorError", "source_anchor_invalid", "source_anchor_ambiguous", "source_reference_mismatch", "source_coverage_incomplete", "incomplete_ready_result", "output_schema_invalid", "evidence_pointer_not_concrete", "evidence_pointer_missing", "evidence_item_ambiguous_or_missing", "evidence_reference_invalid", "review_scope_format_invalid", "ask_user_tool_required")
            if isinstance(error, GraphValidationError) and run:
                self._message(project, cid, str(error), task=task["id"], run=run["id"])
            if reason == "input_budget_exceeded" and stage in range(2, 9) and run and not self._task_data(task).get("batch_manifest_ref"):
                self._begin_batches(task, run, materials)
            elif repairable and run:
                category=self._repair_category(error, reason)
                feedback=str(error)
                if category=='legacy' and getattr(error, 'details', None):
                    feedback += ': ' + json.dumps(error.details, ensure_ascii=False)
                self._repair_execution(task, run, feedback,
                    category=category,
                    diagnostics=getattr(error, 'details', None))
            else:
                message = (f"旧版 {error.details.get('kind', '阶段')} 产物缺少独立原文索引，需从该阶段重新生成并确认"
                           if reason == "source_index_migration_required" else str(error))
                self._fail_execution(task, run, reason, message, getattr(error, "details", None))

    async def _drive_step1_views(self, task, token, source_version, source_text):
        from .step1_execution import drive_step1_views
        await drive_step1_views(self, task, token, source_version, source_text)

    def _auxiliary_config(self, project, parent_config_id, stage):
        fixed = self.store.get(parent_config_id, project_id=project)
        values = deepcopy(fixed["values"].get("auxiliary_configs", {}).get(stage, fixed["values"]))
        values.pop("auxiliary_configs", None)
        self.store.advisory_lock(f"config:snapshot:{project}:{stage}")
        next_version = self.config_service.next_version(project, "run_snapshot", stage)
        return self.store.put(new_record("config_version", project, config_key="harness", scope_kind="run_snapshot",
            scope_key=stage, state="snapshot", version=next_version,
            values=values, sha256=hashlib.sha256(canonical_bytes(values)).hexdigest(), resolved_from_ids=[parent_config_id]))

    def _begin_batches(self, task, run, materials):
        from .context_batching import plan_batches, persist_manifest
        with self.store.transaction():
            self.store.advisory_lock(f"{task['project_id']}:conversation:{task['conversation_id']}")
            data = self._task_data(task)
            config = self.store.get(run["config_version_id"], project_id=task["project_id"])["values"]
            inputs = [{"source_ref": m["ref"], "content": m["content"]} for m in materials
                      if m.get("ref", {}).get("version") and not m.get("builtin")]
            manifest = plan_batches(f"step{data['stage']}", task["id"], run["config_version_id"], inputs, config)
            saved = persist_manifest(self.store, task, run, manifest, inputs, config)
            data.update(batch_manifest_ref=ref(saved), batch_inputs=inputs, batch_source_run_id=run["id"],
                        batch_children={}, batch_coverage_ready=False, model_dispatched=False)
            self._save_task_data(task, data)
            self._checkpoint(task, run, "advance_work")
            self._close_run(task, run, "failed", {"code": "input_budget_exceeded", "message": "已转入真实局部分析批次", "retryable": False, "details": {}})
            self._event(task["project_id"], "context.batches_scheduled", {"manifest_ref": ref(saved), "batches": len(manifest["batches"])},
                        conversation=task["conversation_id"], task=task["id"])

    async def _drive_batch(self, task, token):
        from .context_batching import prepare_batch, complete_batch, batch_coverage
        project = task["project_id"]
        child = run = None
        try:
            with self.store.transaction():
                self.store.advisory_lock(f"{project}:conversation:{task['conversation_id']}")
                data = self._task_data(task)
                source_run = self.store.get(data["batch_source_run_id"], project_id=project)
                if not self._dependencies_valid(source_run):
                    raise WorkflowBlocked("dependency_changed")
                manifest = body(self.store, self.workflow.fixed_version(project, data["batch_manifest_ref"]))
                ledger = batch_coverage(self.store, project, data["batch_manifest_ref"])
                if ledger["complete"]:
                    data["batch_coverage_ready"] = True
                    self._save_task_data(task, data)
                    return
                batch = next(b for b in manifest["batches"] if not next(x for x in ledger["batches"] if x["batch_id"] == b["batch_id"])["complete"])
                key = batch["batch_id"]
                processed_in_batch = sorted(set(batch["owned_unit_ids"]) & set(ledger["processed_unit_ids"]))
                child_key = key + ":" + hashlib.sha256(canonical_bytes(processed_in_batch)).hexdigest()[:16]
                child = self.store.get(data["batch_children"].get(child_key), project_id=project) if data["batch_children"].get(child_key) else None
                if child is None:
                    message = self.store.get(task["requested_by_message_id"], project_id=project)
                    child = self._new_task(project, task["conversation_id"], message, "query", stage=data["stage"], parent=task,
                        batch_owned=True, batch_id=key, request="读取固定批次并输出局部证据分析，不生成整阶段完成结论。")
                    cdata = self._task_data(child)
                    cdata["config_version_id"] = self._auxiliary_config(project, source_run["config_version_id"], "aux.subtask")["id"]
                    self._save_task_data(child, cdata)
                    data["batch_children"][child_key] = child["id"]
                    self._save_task_data(task, data)
                if child["state"] == "waiting_user":
                    data["waiting_batch_task_id"] = child["id"]
                    self._save_task_data(task, data)
                    self._transition(self.store.get(task["id"], project_id=project), "waiting_user")
                    return
                if child["state"] in ("paused", "stopped", "stopping"):
                    raise WorkflowBlocked("child_blocked", {"task_id": child["id"]})
                cdata = self._task_data(child)
                previous = self.store.get(cdata["current_run_id"], project_id=project) if cdata.get("current_run_id") else None
                rejected_cached = bool(previous and self._output_rejected(child, previous["id"]))
                saved = self._saved_output(child, previous["id"]) if previous else None
                saved_source_run_id = previous["id"] if saved is not None else None
                resume = cdata.get("resume_saved_result")
                if resume and self._output_rejected(child, resume["source_run_id"]):
                    rejected_cached = True
                    cdata.pop("resume_saved_result")
                    self._save_task_data(child, cdata)
                    resume = None
                if saved is None and resume:
                    candidate_run = self.store.get(resume["source_run_id"], project_id=project)
                    candidate = self._projection(project, "run_result", resume["source_run_id"]).get("output")
                    if (not candidate_run or candidate_run["task_id"] != child["id"]
                        or candidate_run["config_version_id"] != cdata["config_version_id"]
                        or candidate is None or candidate != resume["output"]):
                        raise WorkflowBlocked("checkpoint_invalid", {"reason": "invalid saved batch response"})
                    saved, saved_source_run_id = candidate, candidate_run["id"]
                if previous and previous["id"] in cdata.get("answered_needs_input_run_ids", []):
                    saved = None
                if previous and previous["state"] not in CLOSED_RUNS:
                    # Only the owning parent may recover this auxiliary operation.
                    if previous["lease_expires_at"] and previous["lease_expires_at"] > self.store.now():
                        return
                    calls = all_records(self.store, project, "model_call", run_id=previous["id"])
                    if saved is None and previous["execution_kind"] == "recovery" and not calls and not rejected_cached:
                        raise WorkflowBlocked("checkpoint_invalid", {"reason": "recovery response missing; no provider request was dispatched"})
                    if any(c["state"] in ("pending", "running", "unknown") for c in calls) or (saved is None and cdata.get("model_dispatched") and not rejected_cached):
                        raise WorkflowBlocked("operation_uncertain")
                    update(self.store, previous, state="interrupted", finished_at=self.store.now(), lease_owner=None, lease_expires_at=None)
                    session = self.store.get(previous["session_id"], project_id=project)
                    update(self.store, session, lease_owner=None, lease_expires_at=None, fencing_token=session["fencing_token"] + 1)
                    child = update(self.store, child, current_run_id=None)
                config = self.store.get(source_run["config_version_id"], project_id=project)["values"]
                prepared = prepare_batch(manifest, key, data["batch_inputs"], config,
                    manifest_ref=data["batch_manifest_ref"], processed_unit_ids=ledger["processed_unit_ids"], continuity_refs=ledger["result_refs"][-2:], store=self.store, project_id=project)
                if not prepared["owned"] and not prepared["complete"]:
                    raise WorkflowBlocked("batch_unit_exceeds_remaining_budget")
                batch_event = self._event(project, "context.batch_prepared", prepared["runtime_batch_state"], conversation=task["conversation_id"], task=child["id"])
                materials = [{"schema_id": "runtime.batch_state", "builtin": "runtime.batch_state", "required": True,
                              "ref": ref(batch_event), "content": prepared["runtime_batch_state"], "projection_pointer": "/payload"}]
                materials.extend(prepared.get("continuity_materials", []))
                # Fixed originals stay as provenance; aux.subtask reads only owned/neighbor pieces.
                materials.extend({**m, "required": False} for m in self._fixed_run_materials(source_run) if not m.get("builtin"))
                self._budget_check(child)
                child, run, session, config = self._start_run(child, "aux.subtask", materials,
                    recovery=saved is not None,
                    session_key_override=cdata.get('repair_session_key'),
                    fresh_allowance=bool(cdata.get('repair_session_key') and saved is None))
                cdata = self._task_data(child)
                cdata["model_dispatched"] = saved is None
                if saved is not None:
                    # Publish a recoverable Run and its exact response atomically.
                    # A restart must never infer a provider call from this replay.
                    self._save_projection(project, "run_result", run["id"], {"output": saved, "source_run_id": saved_source_run_id})
                    cdata["resume_saved_result"] = {"source_run_id": saved_source_run_id, "output": saved}
                self._save_task_data(child, cdata)
            prompt = (f"为Step{data['stage']}执行局部分析。读取runtime.batch_state所有owned_source_ranges，neighbor_source_ranges仅作邻接证据。"
                      "用subtask_result记录发现、真实证据引用、建议和限制；不要声称已看未分配范围。"
                      "若材料冲突或不能完整处理当前范围，返回needs_input，不以ready掩盖缺口。\n" + data.get("request", "")
                      + "\n批次任务与用户补充：\n" + cdata.get("request", "")
                      + ("\nHarness 校验修复单：\n" + json.dumps(cdata['repair_brief'], ensure_ascii=False)
                         if cdata.get('repair_brief') else ""))
            result = saved if saved is not None else await self._invoke("aux.subtask", child, run, session, config, materials, prompt, token)
            with self.store.transaction():
                self.store.advisory_lock(f"{project}:conversation:{task['conversation_id']}")
                child = self._assert_run(child, run, token)
                persisted = self._projection(project, "run_result", run["id"])
                persisted["output"] = result
                self._save_projection(project, "run_result", run["id"], persisted)
            await self._consume_steers(child, run, token)
            with self.store.transaction():
                self.store.advisory_lock(f"{project}:materials")
                self.store.advisory_lock(f"{project}:conversation:{task['conversation_id']}")
                child = self._assert_run(child, run, token)
                self._budget_check(child)
                if not self._dependencies_valid(source_run) or not self._dependencies_valid(run):
                    raise WorkflowBlocked("dependency_changed")
                self.workflow.catalog.validate("subtask_result", result, config.get("schemas"))
                if result["result_kind"] == "needs_input":
                    self._await_user_questions(child, run, result["questions"])
                    data["waiting_batch_task_id"] = child["id"]
                    self._save_task_data(task, data)
                    self._transition(self.store.get(task["id"], project_id=project), "waiting_user")
                    return
                artifact = self.store.put(new_record("artifact", project, artifact_kind="subtask_result", scope=child["scope"]))
                output = self.workflow.save(project, "subtask_result", result, run=run, inputs=run["input_refs"], effective=True, artifact_id=artifact["id"])
                complete_batch(self.store, project, data["batch_manifest_ref"], key, child["id"], [ref(output)],
                               [u["unit_id"] for u in prepared["owned"]])
                self._checkpoint(child, run, "advance_work")
                self._close_run(child, run, "succeeded")
                self._transition(self.store.get(child["id"], project_id=project), "succeeded")
        except asyncio.CancelledError:
            if not self._closing:
                if child and run: self._fail_execution(child, run, "user_stop")
                self._fail_execution(task, None, "user_stop")
        except Exception as error:
            reason = getattr(error, "reason", None) or getattr(error, "code", "configuration_error")
            diagnostics = getattr(error, 'details', None) or {}
            if isinstance(error, OutputValidationError):
                reason = 'output_schema_invalid'
                diagnostics = {'validation_errors': error.problems}
            repairable = (reason in ('evidence_reference_invalid', 'output_schema_invalid')
                          or reason == 'ModelBehaviorError')
            if child and run and repairable:
                diagnostics = {**diagnostics, 'code': reason}
                category = self._repair_category(error, reason)
                feedback = str(error)
                if category == 'legacy' and diagnostics:
                    feedback += ': ' + json.dumps(diagnostics, ensure_ascii=False)
                self._repair_execution(child, run, feedback, category=category,
                                       diagnostics=diagnostics)
                failed_child = self.store.get(child["id"], project_id=project)
                if failed_child["state"] == "paused":
                    self._fail_execution(task, None, failed_child["pause_reason"], feedback)
                return
            if child and run: self._fail_execution(child, run, reason, str(error), getattr(error, "details", None))
            self._fail_execution(task, None, reason, str(error), getattr(error, "details", None))

    @staticmethod
    def _repair_category(error, reason):
        if type(error).__name__ == 'ValidationError':
            return 'content'
        issue = (getattr(error, 'details', None) or {}).get('output_failure', {})
        if issue.get('category') in ('content', 'format'):
            return 'content'  # Failed format repair returns to the producer.
        if reason in ('source_anchor_invalid', 'source_anchor_ambiguous', 'source_reference_mismatch',
                      'source_coverage_incomplete', 'source_window_coverage_invalid',
                      'source_window_boundary_invalid', 'source_window_event_outside_commit',
                      'source_window_wrong_view', 'source_window_incomplete',
                      'source_view_incomplete', 'source_analysis_incomplete',
                      'source_window_empty_interval_unverified', 'output_schema_invalid',
                      'incomplete_ready_result', 'evidence_pointer_not_concrete',
                      'evidence_pointer_missing', 'evidence_item_ambiguous_or_missing'):
            return 'content'
        return 'legacy'

    def _repair_execution(self, task, run, error, *, category='legacy', diagnostics=None):
        with self.store.transaction():
            self.store.advisory_lock(f"{task['project_id']}:conversation:{task['conversation_id']}")
            current = self.store.get(task["id"], project_id=task["project_id"])
            live = self.store.get(run["id"], project_id=task["project_id"])
            if live["state"] in CLOSED_RUNS or live["lease_owner"] != self.owner:
                return
            config = self.store.get(run["config_version_id"], project_id=task["project_id"])["values"]
            data = self._task_data(current)
            # Mark before checking the limit: the final rejected attempt must not
            # become a recoverable response after an explicit budget extension.
            origins = self._output_lineage(current, run["id"])
            if data.get("resume_saved_result"):
                origins.extend(self._output_lineage(current, data["resume_saved_result"]["source_run_id"]))
            data["rejected_output_run_ids"] = list(dict.fromkeys(data.get("rejected_output_run_ids", []) + origins))
            data.pop("resume_saved_result", None)
            data["model_dispatched"] = False
            self._save_task_data(current, data)
            content = category == 'content'
            # A pre-upgrade task has no protocol marker and only the aggregate
            # counter. New-format tasks keep content and legacy repair limits
            # independent even if both happen in one Task.
            migrated = current['repair_rounds_used'] if 'repair_protocol_version' not in data else 0
            used = (data.get('content_repair_rounds_used', migrated if content else 0)
                    if content else data.get('legacy_repair_rounds_used', migrated))
            limit = (_cfg(config, 'repair.content_max_rounds', 5) if content else
                     _cfg(config, 'repair.max_rounds', 2)) + data.get('additional_repair_rounds', 0)
            if used >= limit:
                self._fail_execution(task, run, "repair_exhausted", error)
                return
            detail = diagnostics or {}
            self._close_run(current, live, "failed", {"code": "output_validation_failed",
                "message": error, "retryable": False,
                "details": {'category': category, 'validation': deepcopy(detail)}})
            current = self.store.get(task["id"], project_id=task["project_id"])
            current = update(self.store, current, repair_rounds_used=current["repair_rounds_used"] + 1)
            data = self._task_data(current)
            data['repair_protocol_version'] = 2
            prior = self._projection(task["project_id"], "run_result", run["id"]).get("output")
            if content:
                from .model_service import append_history
                data['content_repair_rounds_used'] = used + 1
                issue = detail.get('output_failure', {})
                candidate_ref = None
                if issue.get('history_record_id'):
                    candidate_ref = {'record_id': issue['history_record_id'],
                                     'json_pointer': '/output',
                                     'sha256': issue.get('candidate_sha256')}
                elif prior is not None:
                    archived = append_history(self.store, task['project_id'], task['conversation_id'],
                        prior, kind='model_output', task_id=task['id'], run_id=run['id'])
                    candidate_ref = {'record_id': archived['id'], 'sha256': candidate_hash(prior)}
                problems = issue.get('problems') or detail.get('validation_errors') or []
                if not isinstance(problems, list):
                    problems = [{'path': '', 'validator': 'stage_contract',
                                 'message': str(problems)[:500], 'category': 'content'}]
                if not problems:
                    problems = [{'path': '', 'validator': 'stage_contract',
                                 'message': error[:500], 'category': 'content'}]
                raw_details = {key: value for key, value in detail.items()
                               if key not in ('output_failure', 'validation_errors', 'validation_error')
                               and isinstance(value, (str, int, float, bool, type(None)))
                               and len(str(value)) <= 500}
                data['repair_brief'] = {
                    'error_code': getattr(error, 'code', None) or detail.get('code') or 'output_validation_failed',
                    'errors': problems[:30], 'error_count': len(problems),
                    'details': raw_details, 'candidate_ref': candidate_ref,
                    'source_input_refs': live['input_refs'][:30],
                    'source_input_ref_count': len(live['input_refs']),
                    'instruction': ('修复上述明确缺失或无效的字段；按需读取固定候选的对应路径。'
                                    '输出本阶段完整 Schema 对象，不得凭空补写原文或事实。'
                                    '无法定位或证据不足时基于固定输入重新生成。'
                                    + ('校验错误数量超过当前修复单范围，请复核整个固定候选。'
                                       if len(problems) > 30 else '')),
                    'round': used + 1, 'max_rounds': limit,
                }
                state = data.get('source_window_state')
                if state:
                    data['repair_brief']['source_window'] = {
                        'source_ref': state['source_ref'], 'start_utf16': state['cursor']}
                data['repair_session_key'] = f'repair:{task["id"]}:{run["id"]}:{used + 1}'
            else:
                data['legacy_repair_rounds_used'] = used + 1
                # Existing review/graph/protocol repair keeps its former limit.
                # It too uses a fresh Session and a bounded diagnostic rather
                # than appending an entire failed draft to the request.
                data['repair_brief'] = {'error_code': 'output_validation_failed',
                    'errors': [{'path': '', 'message': error[:500]}],
                    'source_input_refs': live['input_refs'], 'round': used + 1}
                data['repair_session_key'] = f'repair:{task["id"]}:{run["id"]}:{used + 1}'
            data.pop("remaining_turns", None)
            data.pop("resume_saved_result", None)
            data["model_dispatched"] = False
            self._save_task_data(current, data)
            # This is additional authorized work in the same running Task, not a
            # budget-pause recovery or a new user task.
            self._transition(current, "running")
            self._event(task["project_id"], "repair.scheduled",
                {"round": used + 1,
                 "category": category, "error": error[:2000],
                 "candidate_ref": data['repair_brief'].get('candidate_ref')},
                conversation=task["conversation_id"], task=task["id"])

    def _fail_execution(self, task, run, reason, message=None, details=None):
        reason = {"max_turns": "turn_limit", "MaxTurnsExceeded": "turn_limit"}.get(reason, reason)
        with self.store.transaction():
            self.store.advisory_lock(f"{task['project_id']}:conversation:{task['conversation_id']}")
            current = self.store.get(task["id"], project_id=task["project_id"])
            live = self.store.get(run["id"], project_id=task["project_id"]) if run else None
            if live:
                if live["lease_owner"] != self.owner or live["state"] in CLOSED_RUNS:
                    return
                if reason in ("dependency_changed", "user_stop", "cost_limit", "active_time_limit", "turn_limit"):
                    saved = self._saved_output(current, run["id"])
                    data = self._task_data(current)
                    saved_ready = isinstance(saved, dict) and saved.get("result_kind") == "ready"
                    if saved_ready and not data.get("source_window_state") and not data.get("step1_view") and data.get("stage") in STAGES and data.get("stage") != 6 and not data.get("batch_owned") \
                            and not data.get("result_ref") and not data.get("result_refs"):
                        candidate = (self._bind_program_provenance(2, live, saved, self._fixed_run_materials(live))
                                     if data["stage"] == 2 else saved)
                        version = self.workflow.save(task["project_id"], STAGES[data["stage"]], candidate,
                            stage=data["stage"], chapter_id=data.get("chapter_id"), run=live, inputs=live["input_refs"],
                            historical_inputs=[data["baseline_ref"]] if data["stage"] == 10 and data.get("baseline_ref") else [])
                        if reason == "dependency_changed":
                            update(self.store, self.workflow.state(version), dependency_status="review_required")
                        data["result_ref"] = ref(version)
                        self._save_task_data(current, data)
                self._close_run(current, live, "stopped" if reason == "user_stop" else "paused",
                                {"code": reason, "message": message or reason, "retryable": False, "details": deepcopy(details or {})})
                current = self.store.get(task["id"], project_id=task["project_id"])
            waiting = reason in ("missing_material", "confirmation_required", "chapter_scope_required", "source_anchor_ambiguous")
            self._transition(current, "stopped" if reason == "user_stop" else "waiting_user" if waiting else "paused", None if waiting else reason)
            notice = self._message(task["project_id"], task["conversation_id"], f"任务需要处理：{message or reason}。已保留记录和进度。", task=task["id"])
            if waiting:
                data = self._task_data(current)
                data["pending_user_items"] = [self._wait_item(current, "question", notice, message or reason)]
                self._save_task_data(current, data)

    def _await_user_questions(self, task, run, questions):
        data = self._task_data(task)
        message = self._message(task["project_id"], task["conversation_id"], "\n".join(q["prompt"] for q in questions) or "当前阶段需要补充信息。", task=task["id"], run=run["id"])
        data["pending_user_items"] = [{**self._wait_item(task, "question", message, q["prompt"], question_id=q.get("question_id") or f"ask-user-{index + 1}"),
            **{key: deepcopy(q[key]) for key in ('suggested_answers', 'reason', 'target_field', 'blocking_scope') if key in q}} for index, q in enumerate(questions)]
        self._save_task_data(task, data)
        self._checkpoint(task, run, "await_user")
        self._close_run(task, run, "waiting_user")
        self._transition(self.store.get(task["id"], project_id=task["project_id"]), "waiting_user")

    def _request_agent_confirmation(self, coordinator, run, question):
        """Turn a manager ask_user call into one fixed-version confirmation card."""
        project, conversation = coordinator["project_id"], coordinator["conversation_id"]
        child = self.store.get(question["confirmation_task_id"], project_id=project)
        if not child or child["conversation_id"] != conversation or child["state"] != "waiting_user":
            raise WorkflowBlocked("confirmation_candidate_missing")
        parent = self.store.get(child["parent_task_id"], project_id=project) if child["parent_task_id"] else None
        if (not parent or parent["state"] not in ("queued", "running", "waiting_user")
                or not self._task_data(parent).get("manager_controlled")):
            raise WorkflowBlocked("confirmation_candidate_missing")
        data = self._task_data(child)
        if (data.get("stage") not in range(2, 11) or data.get("superseded_by_task_id")
                or any(item["state"] == "open" for item in data.get("pending_user_items", []))):
            raise WorkflowBlocked("confirmation_candidate_missing")
        refs = [data.get("result_ref")]
        if not refs or any(not isinstance(value, dict) for value in refs):
            raise WorkflowBlocked("confirmation_candidate_missing")
        targets = []
        for saved_ref in refs:
            version = self.workflow.fixed_version(project, saved_ref)
            state = self.workflow.state(version)
            artifact = self.store.get(version["artifact_id"], project_id=project)
            if (state["dependency_status"] != "valid" or state["confirmation_status"] == "confirmed"
                    or artifact["latest_version"] != version["version"]):
                raise WorkflowBlocked("confirmation_candidate_changed")
            targets.append({"subject": ref(version), "selections": [WHOLE]})
        message = self._message(project, conversation, question["prompt"], task=child["id"], run=run["id"])
        data["pending_user_items"] = [{**self._wait_item(child, "confirmation", message, question["prompt"], targets),
                                       "agent_requested": True}]
        self._save_task_data(child, data)
        self._event(project, "confirmation.requested_by_agent", {
            "coordinator_task_id": coordinator["id"], "candidate_task_id": child["id"],
            "artifact_refs": [target["subject"] for target in targets],
        }, conversation=conversation, task=child["id"], run=run["id"])

    @staticmethod
    def _frozen_material_ref(run, materials, kind):
        """Return one whole-artifact ref that is frozen in this exact Run."""
        matches = [material for material in materials if isinstance(material.get("ref"), dict)
                   and (material.get("kind") or material.get("builtin") or material.get("schema_id")) == kind]
        if len(matches) != 1:
            raise WorkflowBlocked("fixed_material_reference_missing", {
                "kind": kind, "match_count": len(matches),
            })
        material_ref = matches[0]["ref"]
        fields = ("record_id", "version", "item_id", "json_pointer")
        identity_fields = ("record_id", "version")
        frozen = next((candidate for candidate in run.get("input_refs", [])
                       if all(candidate.get(field) == material_ref.get(field) for field in identity_fields)), None)
        if frozen is None:
            raise WorkflowBlocked("fixed_material_reference_not_in_run", {
                "kind": kind, "reference": deepcopy(material_ref),
            })
        return {field: deepcopy(frozen.get(field)) for field in fields}

    @staticmethod
    def _reference_sources(run, materials):
        """Return only materials whose exact identities were frozen for this Run."""
        fields = ("record_id", "version", "item_id", "json_pointer")
        identity_fields = ("record_id", "version")
        sources = []
        for material in materials:
            material_ref = material.get("ref")
            if not isinstance(material_ref, dict):
                continue
            frozen = next((candidate for candidate in run.get("input_refs", [])
                           if all(candidate.get(field) == material_ref.get(field) for field in identity_fields)), None)
            if frozen is None:
                continue
            sources.append({
                "kind": material.get("kind") or material.get("builtin") or material.get("schema_id"),
                "ref": {field: deepcopy(frozen.get(field)) for field in fields},
                "content": material.get("content"),
            })
        return sources

    @staticmethod
    def _reference_kinds(output_kind, path):
        """Map an output field to the fixed material kinds that can own it."""
        leaf = next((part for part in reversed(path.split("/")) if part and not part.isdigit()), "")
        rules = {
            "source_knowledge_asset": {
                "source_global_events_ref": ("source_global_events",),
                "source_global_analysis_ref": ("source_global_analysis",),
                "source_character_events_ref": ("source_character_events",),
                "character_ref": ("source_character_events",),
                "other_character_ref": ("source_character_events",),
                "key_event_refs": ("source_global_events", "source_character_events"),
            },
            "adaptation_strategy": {
                "source_knowledge_asset_ref": ("source_knowledge_asset",),
                "character_ref": ("source_knowledge_asset",),
                "decision_refs": ("runtime.applicable_controls",),
                "default_strategy_ref": ("adaptation_strategy",),
            },
            "adaptation_plan": {
                "source_knowledge_asset_ref": ("source_knowledge_asset",),
                "strategy_ref": ("adaptation_strategy",),
                "character_ref": ("adaptation_strategy", "source_knowledge_asset"),
                "subject_ref": ("adaptation_strategy", "source_knowledge_asset"),
                "source_refs": ("adaptation_strategy", "source_knowledge_asset"),
            },
            "game_event_view": {
                "character_refs": ("source_character_events",),
                "source_ref": ("source_text",),
            },
            "player_profiles": {
                "game_event_view_ref": ("game_event_view",),
                "ending_routes_ref": ("ending_routes",),
                "related_refs": ("game_event_view", "ending_routes"),
            },
            "chapter_design": {
                "plan_ref": ("adaptation_plan",),
                "game_event_refs": ("game_event_view",),
                "source_ref": ("source_text",),
                "character_refs": ("adaptation_plan",),
                "location_refs": ("adaptation_plan",),
                "global_entity_refs": ("adaptation_plan",),
                "used_by_refs": ("ending_routes", "game_event_view", "player_profiles"),
            },
            "review_report": {
                "reviewed_artifact_refs": ("nexo_graph",),
                "location_ref": ("nexo_graph",),
            },
        }
        if output_kind == "review_report" and "/graph_checks/" in path and leaf == "evidence_refs":
            return ("runtime.graph_checks",)
        if output_kind == "review_report" and leaf == "evidence_refs":
            return ("nexo_graph",)
        return rules.get(output_kind, {}).get(leaf)

    @staticmethod
    def _find_material_item(content, identity):
        from .context import _identity
        matches = []

        def visit(node, pointer=""):
            if isinstance(node, dict):
                if _identity(node) == identity:
                    matches.append(pointer)
                for key, child in node.items():
                    escaped = str(key).replace("~", "~0").replace("/", "~1")
                    visit(child, pointer + "/" + escaped)
            elif isinstance(node, list):
                for index, child in enumerate(node):
                    visit(child, pointer + "/" + str(index))

        visit(content)
        return matches

    def _bind_reference_hint(self, hint, candidates, *, allow_whole):
        """Resolve semantic hints against fixed inputs; ignore model DB identities."""
        from .context import MaterialError, pointer_values

        if not candidates:
            return None
        identity = hint.get("item_id") if isinstance(hint, dict) else None
        pointer = hint.get("json_pointer") if isinstance(hint, dict) else None
        if identity:
            matches = []
            for source in candidates:
                for found in self._find_material_item(source["content"], identity):
                    if pointer is None or pointer == found or pointer.startswith(found + "/") or found.startswith(pointer.rstrip("/") + "/"):
                        matches.append((source, found))
            if len(matches) == 1:
                source, found = matches[0]
                return {**deepcopy(source["ref"]), "item_id": identity, "json_pointer": found}
        if pointer is not None and "*" not in pointer.split("/"):
            matches = []
            for source in candidates:
                try:
                    pointer_values(source["content"], pointer, required=True)
                    matches.append(source)
                except MaterialError:
                    pass
            if len(matches) == 1:
                return {**deepcopy(matches[0]["ref"]), "item_id": None, "json_pointer": pointer}
        return deepcopy(candidates[0]["ref"]) if allow_whole else None

    def _bind_program_provenance(self, stage, run, result, materials):
        """Create provenance from direct or transitively fixed inputs, never model DB IDs."""
        if stage not in STAGES or result.get("result_kind") != "ready":
            return result
        value = deepcopy(result)
        output_kind = STAGES[stage]
        sources = self._reference_sources(run, materials)

        # Some stages intentionally receive a compact parent artifact instead
        # of every referenced body.  Provenance still follows the parent's
        # immutable references, while those bodies remain outside model input.
        if stage == 5:
            views = next(source for source in sources if source["kind"] == "source_global_events")
            characters = next(source for source in sources if source["kind"] == "source_character_events")
            if views["content"]["payload"]["source_ref"] != characters["content"]["payload"]["source_ref"]:
                raise WorkflowBlocked("source_reference_mismatch", {"kind": "step1_views"})
            source_version = self.workflow.fixed_version(run["project_id"],
                views["content"]["payload"]["source_ref"])
            sources.append({"kind": "source_text", "ref": ref(source_version),
                            "content": body(self.store, source_version)})
        if stage == 9:
            plan_source = next(source for source in sources if source["kind"] == "adaptation_plan")
            indirect = (("game_events", "game_event_view"),
                        ("ending_routes", "ending_routes"),
                        ("player_profiles", "player_profiles"))
            missing = [(field, kind) for field, kind in indirect
                       if not any(source["kind"] == kind for source in sources)]
            if missing:
                plan_refs = plan_source["content"]["payload"]["stage_artifact_refs"]
                for field, kind in missing:
                    version = self.workflow.fixed_version(run["project_id"], plan_refs[field])
                    sources.append({"kind": kind, "ref": ref(version),
                                    "content": body(self.store, version)})

        if stage == 1:
            # Both Step1 branches reference the same frozen original. Bind
            # source identities before their independent coverage checks.
            source_ref = self._frozen_material_ref(run, materials, "source_text")

            def bind_source(node):
                if isinstance(node, dict):
                    return {key: deepcopy(source_ref) if key == "source_ref" else bind_source(child)
                            for key, child in node.items()}
                if isinstance(node, list):
                    return [bind_source(child) for child in node]
                return node

            value = bind_source(value)
            value["evidence_refs"] = [deepcopy(source_ref)]
            return value

        def candidates(path):
            kinds = self._reference_kinds(output_kind, path)
            return [source for source in sources if kinds is None or source["kind"] in kinds]

        semantic_lists = {"decision_refs", "character_refs", "location_refs", "game_event_refs",
                          "source_event_refs", "event_refs", "used_by_refs", "related_refs",
                          "artifact_refs", "global_entity_refs", "source_refs"}

        def visit(node, path=""):
            if isinstance(node, dict) and set(node) == {"record_id", "version", "item_id", "json_pointer"}:
                return self._bind_reference_hint(node, candidates(path), allow_whole=True)
            if isinstance(node, dict):
                out = {}
                for key, child in node.items():
                    child_path = path + "/" + str(key).replace("~", "~0").replace("/", "~1")
                    if key == "evidence_refs" and isinstance(child, list):
                        if child_path == "/evidence_refs":
                            bound = [deepcopy(source["ref"]) for source in candidates(child_path)]
                        else:
                            bound = [self._bind_reference_hint(item, candidates(child_path), allow_whole=True)
                                     for item in child if isinstance(item, dict)]
                            bound = [item for item in bound if item is not None]
                            if not bound:
                                bound = [deepcopy(source["ref"]) for source in candidates(child_path)]
                        out[key] = list({json.dumps(item, sort_keys=True): item for item in bound}.values())
                    elif key in semantic_lists and isinstance(child, list):
                        bound = [self._bind_reference_hint(item, candidates(child_path), allow_whole=False)
                                 for item in child if isinstance(item, dict)]
                        out[key] = list({json.dumps(item, sort_keys=True): item for item in bound if item is not None}.values())
                    else:
                        out[key] = visit(child, child_path)
                return out
            if isinstance(node, list):
                return [visit(child, path + "/" + str(index)) for index, child in enumerate(node)]
            return node

        value = visit(value)
        payload = value["payload"]
        fixed_fields = {
            2: (("source_global_events_ref", "source_global_events"),
                ("source_global_analysis_ref", "source_global_analysis"),
                ("source_character_events_ref", "source_character_events")),
            3: (("source_knowledge_asset_ref", "source_knowledge_asset"),),
            4: (("source_knowledge_asset_ref", "source_knowledge_asset"),
                ("strategy_ref", "adaptation_strategy")),
            8: (("game_event_view_ref", "game_event_view"), ("ending_routes_ref", "ending_routes")),
            9: (("plan_ref", "adaptation_plan"),),
        }
        for field, kind in fixed_fields.get(stage, ()):
            payload[field] = self._frozen_material_ref(run, materials, kind)
        if stage == 3:
            payload["player_identity"]["character_ref"] = None
            payload["player_identity"]["decision_refs"] = []
            baseline = next((source for source in materials if source.get("material_role") == "current_stage_baseline"
                             and source.get("schema_id") == "adaptation_strategy"), None)
            payload["strategy_basis"]["default_strategy_ref"] = deepcopy(baseline["ref"]) if baseline else None
        if stage == 4:
            payload["stage_artifact_refs"] = {
                "game_events": None,
                "event_functions": None,
                "ending_routes": None,
                "player_profiles": None,
            }
        if stage == 5:
            for event in payload["events"]:
                event["narrative_function"] = None
        if stage == 11:
            graph = next((source for source in sources if source["kind"] == "nexo_graph"), None)
            if graph:
                payload["reviewed_artifact_refs"] = [deepcopy(graph["ref"])]
            payload["criteria_ref"] = ref(self.store.get(run["config_version_id"], project_id=run["project_id"]))
            # Script checks are authoritative program records.  The optional
            # validation Agent only reports semantic findings.
            payload["graph_checks"] = []
        return value

    def _apply_stage(self, task, run, result, materials):
        data = self._task_data(task)
        stage = data["stage"]
        if isinstance(result, dict) and "__ask_user__" in result:
            self._await_user_questions(task, run, result["__ask_user__"])
            return
        fixed_config = self.store.get(run["config_version_id"], project_id=task["project_id"])
        structured = fixed_config["values"].get("output", {}).get("structured", {}).get(
            f"step{stage}", True)
        if not structured:
            if not isinstance(result, str) or not result.strip():
                raise WorkflowBlocked("unstructured_output_invalid")
            data["unstructured_result_run_id"] = run["id"]
            data.pop("resume_saved_result", None)
            self._save_task_data(task, data)
            self._message(task["project_id"], task["conversation_id"],
                result.strip() + "\n\n本阶段已关闭 output_type；原始文本已保存在运行记录中，为避免下游读取非结构化数据，全流程在此暂停。",
                task=task["id"], run=run["id"])
            self._checkpoint(task, run, "await_user")
            self._close_run(task, run, "succeeded")
            self._transition(self.store.get(task["id"], project_id=task["project_id"]),
                             "paused", "unstructured_output")
            return
        try:
            model_schema = self.workflow.catalog.schema_for(f"step{stage}", fixed_config["values"])
            self.workflow.catalog.validate(model_schema, result, fixed_config["values"].get("schemas"))
        except ValueError as error:
            raise WorkflowBlocked("output_schema_invalid", {
                "validation_error": str(error),
                **({"validation_errors": error.problems} if isinstance(error, OutputValidationError) else {})
            }) from error
        if result["result_kind"] == "needs_input":
            if getattr(self.model_service, "supports_ask_user", False):
                raise WorkflowBlocked("ask_user_tool_required")
            self._await_user_questions(task, run, result["questions"])
            return
        if result["payload"] is None or result["questions"]:
            raise WorkflowBlocked("incomplete_ready_result")
        display_result = result if stage == 6 else None
        if stage == 6:
            baseline_material = next(m for m in materials if m.get("schema_id") == "game_event_view")
            current = self.workflow.resolve(task["project_id"], "game_event_view")
            if ref(current) != baseline_material["ref"]:
                raise WorkflowBlocked("dependency_changed", {"kind": "game_event_view"})
            plan = self.workflow.resolve(task["project_id"], "adaptation_plan")
            if body(self.store, plan)["payload"]["stage_artifact_refs"]["game_events"] != baseline_material["ref"]:
                raise WorkflowBlocked("dependency_changed", {"kind": "adaptation_plan.stage_artifact_refs.game_events"})
            if model_schema == "game_event_view":
                # A run created before this change keeps its frozen output
                # contract. Convert that legacy full-view response into
                # the same patch while rejecting edits outside the field.
                old_payload = deepcopy(baseline_material["content"]["payload"])
                proposed_payload = deepcopy(result["payload"])
                old_events = old_payload["events"]
                proposed_events = proposed_payload["events"]
                old_ids = [event["game_event_id"] for event in old_events]
                proposed_ids = [event["game_event_id"] for event in proposed_events]
                if old_ids != proposed_ids:
                    raise WorkflowBlocked("output_schema_invalid", {"validation_error": "Step6 必须保留原有事件 ID 和顺序"})
                updates = [{"game_event_id": event["game_event_id"],
                            "narrative_function": event["narrative_function"]}
                           for event in proposed_events]
                for old_event, proposed_event in zip(old_events, proposed_events):
                    old_event.pop("narrative_function", None)
                    proposed_event.pop("narrative_function", None)
                if old_payload != proposed_payload:
                    raise WorkflowBlocked("output_schema_invalid", {"validation_error": "Step6 只能补充 narrative_function"})
            else:
                updates = result["payload"]["updates"]
            update_ids = [item["game_event_id"] for item in updates]
            original_ids = [event["game_event_id"] for event in baseline_material["content"]["payload"]["events"]]
            if len(update_ids) != len(set(update_ids)) or set(update_ids) != set(original_ids):
                raise WorkflowBlocked("output_schema_invalid", {"validation_error": "Step6 必须为现有每个事件各提供一次叙事功能，不得新增或遗漏事件"})
            if any(not item["narrative_function"].strip() for item in updates):
                raise WorkflowBlocked("output_schema_invalid", {"validation_error": "Step6 的叙事功能不能为空"})
            functions = {item["game_event_id"]: item["narrative_function"].strip() for item in updates}
            enriched = deepcopy(baseline_material["content"])
            for event in enriched["payload"]["events"]:
                event["narrative_function"] = functions[event["game_event_id"]]
            enriched["notes"] = result["notes"]
            result = enriched
            try:
                self.workflow.catalog.validate("game_event_view", result, fixed_config["values"].get("schemas"))
            except ValueError as error:
                raise WorkflowBlocked("output_schema_invalid", {"validation_error": str(error)}) from error
        if stage == 7:
            events_material = next(m for m in materials if m.get("schema_id") == "game_event_view")
            if ref(self.workflow.planned_game_events(task["project_id"])) != events_material["ref"]:
                raise WorkflowBlocked("dependency_changed", {"kind": "adaptation_plan.stage_artifact_refs.game_events"})
            event_ids = {event["game_event_id"] for event in events_material["content"]["payload"]["events"]}
            if not event_ids or any(not isinstance(event.get("narrative_function"), str) or
                                    not event["narrative_function"].strip()
                                    for event in events_material["content"]["payload"]["events"]):
                raise WorkflowBlocked("missing_material", {"kind": "game_events.narrative_function"})
            ending_ids = {ending["ending_id"] for ending in result["payload"]["endings"]}
            route_ids = {route["route_id"] for route in result["payload"]["routes"]}
            for route in result["payload"]["routes"]:
                if set(route["game_event_ids"]) - event_ids or set(route["ending_ids"]) - ending_ids:
                    raise WorkflowBlocked("output_schema_invalid", {"validation_error": "Step7 路线引用了不存在的游戏事件或结局 ID"})
            if any(set(item["used_by_item_ids"]) - (ending_ids | route_ids)
                   for item in result["payload"]["state_requirements"]):
                raise WorkflowBlocked("output_schema_invalid", {"validation_error": "Step7 状态需求引用了不存在的结局或路线 ID"})
        if stage == 8:
            pinned = {material["schema_id"]: material["ref"] for material in materials}
            if ref(self.workflow.planned_game_events(task["project_id"])) != pinned["game_event_view"]:
                raise WorkflowBlocked("dependency_changed", {"kind": "adaptation_plan.stage_artifact_refs.game_events"})
            if ref(self.workflow.planned_ending_routes(task["project_id"])) != pinned["ending_routes"]:
                raise WorkflowBlocked("dependency_changed", {"kind": "adaptation_plan.stage_artifact_refs.ending_routes"})
        if stage == 9:
            pinned = {material["schema_id"]: material["ref"] for material in materials}
            plan = self.workflow.resolve(task["project_id"], "adaptation_plan")
            if ref(plan) != pinned["adaptation_plan"]:
                raise WorkflowBlocked("dependency_changed", {"kind": "adaptation_plan"})
            # The three stage artifacts are transitively fixed by the
            # plan.  Resolve them for validation without widening the
            # Step9 model-input contract.
            self.workflow.planned_game_events(task["project_id"], plan=plan)
            self.workflow.planned_ending_routes(task["project_id"], plan=plan)
            self.workflow.planned_player_profiles(task["project_id"], plan=plan)
        # Step6 edits only narrative_function on the fixed Step5 artifact.
        # Its original source anchors were already bound by the Harness;
        # rebinding here would erase them because Step6 does not load the
        # original text as a separate input.
        if stage != 6:
            result = self._bind_program_provenance(stage, run, result, materials)
        if stage == 9:
            # A chapter is made of fixed game events. Derive its original text
            # ranges from their verified Step5 anchors, never from an LLM
            # supplied chapter boundary (which could silently include more of
            # the original than this chapter actually uses).
            plan_material = next(m for m in materials if m["schema_id"] == "adaptation_plan")
            plan = plan_material.get("record") or self.workflow.fixed_version(
                task["project_id"], plan_material["ref"])
            events_version = self.workflow.planned_game_events(task["project_id"], plan=plan)
            events_material = {"schema_id": "game_event_view", "ref": ref(events_version),
                               "content": body(self.store, events_version)}
            source_material = next(m for m in materials if m["schema_id"] == "source_text")
            available = {event["game_event_id"]: event for event in events_material["content"]["payload"]["events"]}
            picked = []
            for event_ref in result["payload"]["game_event_refs"]:
                event_id = event_ref.get("item_id")
                if (event_ref["record_id"] != events_material["ref"]["record_id"] or
                        event_ref["version"] != events_material["ref"]["version"] or event_id not in available):
                    raise WorkflowBlocked("game_event_reference_invalid", {"chapter_id": data.get("chapter_id")})
                picked.append(event_id)
            if not picked or len(set(picked)) != len(picked):
                raise WorkflowBlocked("game_event_reference_invalid", {"chapter_id": data.get("chapter_id")})
            spans = []
            for event_id in picked:
                for anchor in available[event_id]["source_anchors"]:
                    if anchor["source_ref"] != source_material["ref"]:
                        raise WorkflowBlocked("source_reference_mismatch", {"game_event_id": event_id})
                    start, end = anchor["start_utf16"], anchor["end_utf16"]
                    if type(start) is not int or type(end) is not int or start >= end:
                        raise WorkflowBlocked("source_index_migration_required", {"kind": "game_event_view"})
                    spans.append((start, end))
            merged = []
            for start, end in sorted(spans):
                if not merged or start > merged[-1][1]:
                    merged.append([start, end])
                else:
                    merged[-1][1] = max(merged[-1][1], end)
            result["payload"]["chapter_source_anchors"] = [
                {"source_ref": deepcopy(source_material["ref"]), "start_utf16": start, "end_utf16": end,
                 "exact_quote": None, "prefix": None, "suffix": None}
                for start, end in merged]
        if stage in (5, 9):
            if stage == 5:
                views_material = next(m for m in materials if m["schema_id"] == "source_global_events")
                views_version = views_material.get("record") or self.workflow.fixed_version(
                    task["project_id"], views_material["ref"])
                source_version = self.workflow.original_for(task["project_id"], views_version)
                source = {"ref": ref(source_version), "content": body(self.store, source_version)}
            else:
                source = next(m for m in materials if m["schema_id"] == "source_text")
            result = locate_source_anchors(result, source["content"], source["ref"], full_coverage=False)
        if stage == 5:
            for event in result["payload"]["events"]:
                if event["adaptation_kind"] != "new" and not event["source_anchors"]:
                    raise WorkflowBlocked("source_anchor_invalid", {"kind": "game_event_view.events", "game_event_id": event["game_event_id"]})
            if any(not item["source_anchors"] for item in result["payload"]["source_coverage"]):
                raise WorkflowBlocked("source_anchor_invalid", {"kind": "game_event_view.source_coverage"})
        if stage == 9:
            payload = result["payload"]
            if payload["chapter_id"] != data.get("chapter_id"):
                raise WorkflowBlocked("chapter_scope_changed")
        if stage == 10:
            payload = result["payload"]
            if payload["chapter"]["id"] != data.get("chapter_id"):
                raise GraphError("chapter identity differs from task")
            baseline = body(self.store, self.workflow.fixed_version(task["project_id"], data["baseline_ref"]))
            write_context = self.store.get(data["graph_write_ref"]["record_id"], project_id=task["project_id"])["payload"]
            metadata = self.store.resolve_ref(write_context["metadata_ref"], task["project_id"])["payload"]
            authorization = write_context["change_scope"]
            preview = merge_chapter(baseline, result, chapter_id=data["chapter_id"], chapter_order=metadata["chapter_ids"],
                entity_catalog=metadata["entity_catalog"], schemas=fixed_config["values"].get("schemas"),
                allowed_removed_ids=set(authorization["removed_node_ids"] + authorization["removed_edge_ids"]),
                allowed_shared_changes=set(authorization.get("shared_entity_ids", [])))
            result = deepcopy(result)
            result["payload"]["chapter"] = next(c for c in preview["chapters"] if c["id"] == data["chapter_id"])
            from .graph import _nested_ids
            old_ids, proposed_ids = _nested_ids(baseline), _nested_ids(result["payload"]["chapter"])
            self._event(task["project_id"], "graph.identities_resolved", {"baseline_ref": data["baseline_ref"],
                "chapter_id": data["chapter_id"], "retained_ids": sorted(proposed_ids & old_ids),
                "registered_new_ids": sorted(proposed_ids - old_ids), "removed_ids": sorted(set(authorization["removed_node_ids"] + authorization["removed_edge_ids"]))},
                conversation=task["conversation_id"], task=task["id"], run=run["id"])
            checks = quality_checks(preview, fixed_config["values"].get("schemas"))
            if any(check["status"] != "pass" for check in checks):
                raise GraphValidationError(checks)
        prior = self.workflow.fixed_version(task["project_id"], data["result_ref"]) if data.get("result_ref") else None
        if prior and body(self.store, prior) == result:
            version = prior
            if stage == 11:
                update(self.store, self.workflow.state(version), confirmation_status="not_required", effective_selections=[WHOLE])
                artifact = self.store.get(version["artifact_id"], project_id=task["project_id"])
                update(self.store, artifact, current_effective_version=version["version"])
        else:
            step6_baseline = baseline_material["ref"] if stage == 6 else None
            version = self.workflow.save(task["project_id"], STAGES[stage], result, stage=stage,
                chapter_id=data.get("chapter_id"), run=run,
                inputs=run["input_refs"] + ([step6_baseline] if step6_baseline else []),
                effective=stage == 11,
                historical_inputs=([step6_baseline] if step6_baseline else []) +
                    ([data["baseline_ref"]] if stage == 10 and data.get("baseline_ref") else []))
        data["result_ref"] = ref(version)
        data.pop("resume_saved_result", None)
        self._save_task_data(task, data)
        if not self._dependencies_valid(run):
            update(self.store, self.workflow.state(version), dependency_status="review_required")
            self._checkpoint(task, run, "commit_artifact")
            self._close_run(task, run, "paused")
            self._transition(self.store.get(task["id"], project_id=task["project_id"]), "paused", "dependency_changed")
            return
        if stage == 11:
            self._deliver_or_repair(task, run, version, result)
            return
        agent_confirmation = bool(getattr(self.model_service, "supports_manager", False)
                                  and data.get("parent_owned"))
        text = stage_result_text(stage, result,
                                 version=version["version"], confirmation=not agent_confirmation)
        message = self._message(task["project_id"], task["conversation_id"], text, task=task["id"], run=run["id"])
        targets = [{"subject": ref(version), "selections": [WHOLE]}]
        data = self._task_data(task)
        data["pending_user_items"] = ([] if agent_confirmation else
                                      [self._wait_item(task, "confirmation", message,
                                                       f"请确认 Step {stage} 已展示的固定版本，或提出修改。", targets)])
        self._save_task_data(task, data)
        presentations = self._projection(task["project_id"], "presentations", task["conversation_id"], targets=[])
        presentations["targets"].append({**targets[0], "message_id": message["id"], "task_id": task["id"]})
        self._save_projection(task["project_id"], "presentations", task["conversation_id"], presentations)
        self._event(task["project_id"], "artifact.presented", {"artifact_ref": ref(version), "message_id": message["id"], "selections": [WHOLE]}, conversation=task["conversation_id"], task=task["id"])
        self._checkpoint(task, run, "await_user")
        self._close_run(task, run, "succeeded")
        self._transition(self.store.get(task["id"], project_id=task["project_id"]), "waiting_user")

    def _coordinator_state(self, project, conversation, queued):
        queue_context = self._projection(project, "queue_context", queued["id"], presented=[])
        tasks = all_records(self.store, project, "task", conversation_id=conversation)
        directory, plans = [], []
        for task in tasks:
            entry = {"task_ref": ref(task), "state": task["state"], "intent": task["intent"],
                     "scope": {k: task["scope"][k] for k in ("stage", "chapter_ids", "branch_ids", "target_refs")}}
            if task["state"] not in ("succeeded", "failed", "stopped"):
                entry.update(parent_task_id=task["parent_task_id"], current_run_id=task["current_run_id"],
                             request_ref=ref(self.store.get(task["requested_by_message_id"], project_id=project)))
            if task["pause_reason"]:
                entry["pause_reason"] = task["pause_reason"]
            directory.append(entry)
            data = self._task_data(task)
            plan = data.get("chapter_plan")
            if plan:
                # Full proposal/request text remains in the exact Decision record.
                plans.append({"task_ref": ref(task), "decision_ref": {"record_id": plan["decision_id"],
                    "version": str(plan["version"]), "item_id": None, "json_pointer": None},
                    "confirmed_decision_ref": data.get("confirmed_chapter_plan_ref"),
                    "chapter_ids": plan["chapter_ids"], "source_ref": plan.get("source_ref")})
        gate = self._control(project, conversation)
        state = {"tasks": directory, "queue_gate": {"row_version": gate["row_version"], "holds": self._effective_holds(project, gate),
                                                   "last_applied_event_seq": gate.get("last_applied_event_seq", 0)},
                 "pending_user_items": [p for t in tasks for p in self._task_data(t).get("pending_user_items", []) if p["state"] == "open"],
                 "presented_at_submission": queue_context["presented"],
                 "chapter_plans": plans}
        return state

    def _coordinator_materials(self, project, conversation, queued):
        state = self._coordinator_state(project, conversation, queued)
        event = self._event(project, "runtime.status_prepared", state, conversation=conversation, source=queued["source_message_id"])
        materials = [{"builtin": "runtime.status", "schema_id": "runtime.status", "required": True,
                      "ref": ref(event), "content": state, "projection_pointer": "/payload"}]
        for artifact in all_records(self.store, project, "artifact"):
            if artifact["artifact_kind"] in ("source_text", "work_summary"):
                continue
            versions = self.workflow.versions(project, artifact["id"])
            selected = next((v for v in versions if v["version"] == artifact["latest_version"]), None)
            if selected:
                materials.append({"schema_id": artifact["artifact_kind"], "kind": artifact["artifact_kind"],
                    "content": body(self.store, selected), "ref": ref(selected), "required": False,
                    "state": self.workflow.state(selected)})
        return materials

    async def _plan_chapters(self, task, token):
        run = None
        try:
            with self.store.transaction():
                project = task["project_id"]
                materials = self.workflow.materials(project, 9)
                task, run, session, config = self._start_run(task, "coordinator", materials)
            prompt = ("已获得整剧改编授权，当前需确定章节范围。请依据已确认方案、事件和路线，自主提出完整有序章节列表、划分依据。"
                      "reply展示建议，task_requests按章节顺序给出stage=9、各章稳定chapter_id、intent=generate；"
                      f"每条source_message_ids只使用原授权消息{task['requested_by_message_id']}。这里只产生建议，等待用户确认后才创作。")
            prompt += '\n' + self._task_data(task).get('request', '')
            result = await self._invoke("coordinator", task, run, session, config, materials, prompt, token)
            with self.store.transaction():
                self.store.advisory_lock(f"{task['project_id']}:conversation:{task['conversation_id']}")
                self._assert_run(task, run, token)
                if isinstance(result, dict) and "__ask_user__" in result:
                    self._await_user_questions(task, run, result["__ask_user__"])
                    return
                validate_output("coordinator_response", result, config.get("schemas"))
                if result['result_kind'] == 'needs_input':
                    if getattr(self.model_service, "supports_ask_user", False):
                        raise WorkflowBlocked("ask_user_tool_required")
                    self._await_user_questions(task, run, result['questions'])
                    return
                if result["result_kind"] != "ready" or not result["payload"]:
                    raise WorkflowBlocked("chapter_plan_needs_input")
                requests = result["payload"]["task_requests"]
                if not requests or any(r["intent"] != "generate" or r["stage"] != 9 or not r["chapter_id"] for r in requests):
                    raise WorkflowBlocked("chapter_plan_invalid")
                data = self._task_data(task)
                root = self.store.get(data["workflow_root_task_id"], project_id=task["project_id"])
                original = self.store.get(task["requested_by_message_id"], project_id=task["project_id"])
                self._propose_chapters(root, requests, original, result["payload"]["reply"], run)
                self._checkpoint(task, run, "none")
                self._close_run(task, run, "succeeded")
                self._transition(self.store.get(task["id"], project_id=task["project_id"]), "succeeded")
        except asyncio.CancelledError:
            if not self._closing:
                self._fail_execution(task, run, "user_stop")
        except Exception as error:
            self._fail_execution(task, run, getattr(error, "reason", None) or getattr(error, "code", "configuration_error"), str(error), getattr(error, "details", None))

    async def _coordinate(self, queued, token, steered_task=None):
        project, cid = queued["project_id"], queued["conversation_id"]
        manager_mode = bool(getattr(self.model_service, "supports_manager", False)) and steered_task is None
        run = None
        with self.store.transaction():
            self.store.advisory_lock(f"{project}:conversation:{cid}")
            current = self.store.get(queued["id"], project_id=project)
            if current["state"] != "pending":
                return
            data = self._projection(project, "queue_context", queued["id"], presented=[])
            prior = self.store.get(data["coordinator_task_id"], project_id=project) if data.get("coordinator_task_id") else None
            if prior and prior["state"] in ("paused", "running", "stopping", "stopped"):
                return
            message = self.store.get(queued["source_message_id"], project_id=project)
            request_text = body(self.store, message)
            if data.get('clarification_context'):
                request_text += '\n前序原始请求与逐题答复（仅本轮用户消息可签发新操作）：\n' + data['clarification_context']
            if data.get('manager_resume'):
                request_text = '本轮用户操作已经由 Harness 应用。请读取最新项目状态，继续尚未完成的授权流程；不要重复执行这条消息中的确认、答复或修改。'
            continuation_parent = self.store.get(data['continuation_parent_task_id'], project_id=project) if data.get('continuation_parent_task_id') else None
            if continuation_parent and (continuation_parent['conversation_id'] != cid or not self._task_data(continuation_parent).get('coordinator')):
                raise WorkflowBlocked('checkpoint_invalid')
            task = prior if prior and prior["state"] == "queued" else self._new_task(project, cid, message, "query", request=request_text, coordinator=True, queue_id=queued["id"], parent=continuation_parent)
            if continuation_parent and data.get('continuation_config_version_id'):
                task_data = self._task_data(task)
                task_data['config_version_id'] = data['continuation_config_version_id']
                self._save_task_data(task, task_data)
            data["coordinator_task_id"] = task["id"]
            self._save_projection(project, "queue_context", queued["id"], data)
            materials = self._coordinator_materials(project, cid, queued)
            self._budget_check(task)
            task, run, session, config = self._start_run(task, "coordinator", materials, recovery=data.get("recovered_output") is not None)
        try:
            text = request_text
            manager_before = None
            try:
                current_source = self.workflow.resolve(project, "source_text")
                source_status = ("\n当前项目已有可用原作固定版本："
                                 + json.dumps(ref(current_source), ensure_ascii=False, sort_keys=True)
                                 + "。对开始／继续改编的请求直接使用它，不得再次要求用户提供原作全文或record_id/version。")
            except WorkflowBlocked:
                source_status = ("\n当前项目尚无可用原作。若当前真实用户消息全文本身就是完整线性小说或剧本，"
                                 "将source_message_kind设为complete_source_text并发起完整改编；Harness会逐字保存该消息。")
            if manager_mode:
                from .manager import build_manager_tools, manager_instructions, manager_state
                manager_before = manager_state(self, project, cid)
                tool_message = message
                if data.get("manager_resume"):
                    roots = [candidate for candidate in all_records(self.store, project, "task", conversation_id=cid)
                             if self._task_data(candidate).get("manager_controlled")
                             and candidate["state"] in ("queued", "running", "waiting_user")]
                    if roots:
                        tool_message = self.store.get(roots[-1]["requested_by_message_id"], project_id=project)
                extra_tools = build_manager_tools(self, task, run, tool_message, data["presented"], token,
                                                  continuation=bool(data.get("manager_resume")))
                effective_instructions = manager_instructions(config, manager_state(self, project, cid))
                instruction = f"本轮真实用户消息 ID：{message['id']}。仅按用户本轮明确要求操作。\n用户消息：\n{text}"
            else:
                extra_tools = None
                effective_instructions = None
                instruction = (f"当前真实用户消息ID：{message['id']}。仅此消息可以作为本轮确认依据。\n"
                               "任务请求不是已执行状态；确认必须指向提交时已展示的固定版本与JSON Pointer。"
                               "若用户希望系统划分章节，提出完整有序章节列表并给出多条stage9请求作为待确认建议。"
                               "不要把普通查询变成创作修改；resume/继续必须指定原Task或等待点。"
                               + source_status + "\n" + text)
            result = data.get("recovered_output")
            if result is None:
                result = await self._invoke("coordinator", task, run, session, config, materials, instruction, token,
                                            extra_tools=extra_tools, instructions_override=effective_instructions)
            tool_questions = result.get("__ask_user__") if isinstance(result, dict) else None
            manager_halt = result.get("__manager_halt__") if isinstance(result, dict) else None
            if manager_halt:
                interpreted = {"result_kind": "ready", "payload": {"reply": "",
                    "source_message_kind": "request", "task_requests": []},
                    "questions": [], "evidence_refs": [], "notes": []}
            elif tool_questions:
                interpreted = {"result_kind": "ready", "payload": {"reply": "",
                    "source_message_kind": "request", "task_requests": []},
                    "questions": [], "evidence_refs": [], "notes": []}
            elif manager_mode and isinstance(result, str):
                if not result.strip():
                    raise WorkflowBlocked("coordinator_output_empty")
                interpreted = {"result_kind": "ready", "payload": {"reply": result,
                    "source_message_kind": "request", "task_requests": []},
                    "questions": [], "evidence_refs": [], "notes": []}
            else:
                validate_output("coordinator_response", result, config.get("schemas"))
                interpreted = result
            with self.store.transaction():
                self.store.advisory_lock(f"{project}:materials")
                self.store.advisory_lock(f"{project}:conversation:{cid}")
                self._assert_run(task, run, token)
                self._save_projection(project, "run_result", run["id"], {"output": result})
                self._checkpoint(task, run, "process_model_result")
                if tool_questions:
                    open_items = [item for candidate in all_records(self.store, project, "task", conversation_id=cid)
                        if candidate["id"] != task["id"] and candidate["state"] == "waiting_user"
                        for item in self._task_data(candidate).get("pending_user_items", [])
                        if item["state"] == "open"]
                    confirmation_questions = [q for q in tool_questions if q.get("confirmation_task_id")]
                    if confirmation_questions:
                        if open_items:
                            self._event(project, "ask_user.suppressed", {"reason": "existing_pending_user_item",
                                "pending_item_ids": [item["id"] for item in open_items]}, conversation=cid, task=task["id"])
                        else:
                            if not manager_mode or len(tool_questions) != 1:
                                raise WorkflowBlocked("confirmation_candidate_ambiguous")
                            self._request_agent_confirmation(task, run, confirmation_questions[0])
                    elif not open_items:
                        self._await_user_questions(task, run, tool_questions)
                        update(self.store, self.store.get(queued['id'], project_id=project), state='blocked',
                               blocked_reason='intent_ambiguous', created_task_id=task['id'])
                        return
                    else:
                        self._event(project, "ask_user.suppressed", {"reason": "existing_pending_user_item",
                            "pending_item_ids": [item["id"] for item in open_items]}, conversation=cid, task=task["id"])
                requests = interpreted["payload"]["task_requests"] if interpreted["payload"] else []
                if manager_mode and requests:
                    raise WorkflowBlocked("manager_tool_required", {"reason": "manager 模式必须通过工具执行，不能提交旧版 task_requests"})
                reply = interpreted["payload"]["reply"] if interpreted["payload"] else "\n".join(q["prompt"] for q in interpreted["questions"])
                receipts, blocked = [], None
                if manager_halt:
                    self._event(project, "manager.dispatch_halted", manager_halt,
                                conversation=cid, task=task["id"], run=run["id"])
                    if manager_halt.get("status") == "prerequisite_pending":
                        roots = [candidate for candidate in all_records(self.store, project, "task", conversation_id=cid)
                                 if self._task_data(candidate).get("manager_controlled")
                                 and candidate["state"] in ("queued", "running", "waiting_user")]
                        if roots:
                            self._transition(roots[-1], "paused", "manager_prerequisite_pending")
                if interpreted["result_kind"] == "needs_input":
                    if getattr(self.model_service, "supports_ask_user", False):
                        raise WorkflowBlocked("ask_user_tool_required")
                    self._await_user_questions(task, run, interpreted['questions'])
                    update(self.store, self.store.get(queued['id'], project_id=project), state='blocked',
                           blocked_reason='intent_ambiguous', created_task_id=task['id'])
                    return
                else:
                    # Older persisted coordinator results predate this field and
                    # are replayed as ordinary requests; immutable recovery data
                    # is never reinterpreted as source text.
                    if not manager_mode and interpreted["payload"].get("source_message_kind", "request") == "complete_source_text":
                        full_requests = [r for r in requests if r["intent"] == "generate" and r["stage"] is None
                                         and r["chapter_id"] is None]
                        if len(full_requests) != 1:
                            raise WorkflowBlocked("invalid_source_message_classification")
                        self._import_source_message(message)
                    chapter_requests = [r for r in requests if r["intent"] in ("generate", "jump") and r["stage"] == 9 and r["chapter_id"]]
                    roots = [t for t in all_records(self.store, project, "task", conversation_id=cid)
                             if self._task_data(t).get("is_workflow") and t["state"] == "waiting_user" and not self._task_data(t).get("chapter_ids")]
                    if chapter_requests and roots:
                        self._propose_chapters(roots[-1], chapter_requests, message, reply, run)
                        requests = [r for r in requests if r not in chapter_requests]
                        receipts.append({"status": "chapter_plan_proposed", "task_id": roots[-1]["id"]})
                    for request in requests:
                        try:
                            receipts.append(self._apply_request(request, message, data["presented"], task, run, steered_task))
                        except WorkflowBlocked as error:
                            blocked = error.reason
                            receipts.append({"status": "blocked", "reason": error.reason, "details": error.details})
                if reply:
                    self._message(project, cid, ('处理建议（尚未执行）：\n' if blocked else '') + reply, task=task["id"], run=run["id"])
                if blocked:
                    self._message(project, cid, f"运行回执：请求尚未执行（{blocked}）。已保留请求，条件满足后可继续。", task=task["id"])
                self._checkpoint(task, run, "await_user" if blocked else "none")
                self._close_run(task, run, "succeeded")
                self._transition(self.store.get(task["id"], project_id=project), "succeeded")
                if manager_mode:
                    progressed = manager_state(self, project, cid) != manager_before
                    roots = [candidate for candidate in all_records(self.store, project, "task", conversation_id=cid)
                             if self._task_data(candidate).get("manager_controlled")
                             and candidate["state"] in ("queued", "running", "waiting_user")]
                    for root in roots:
                        members = [root] + all_records(self.store, project, "task", parent_task_id=root["id"])
                        if not any(item["state"] == "open" for member in members
                                   for item in self._task_data(member).get("pending_user_items", [])):
                            if progressed:
                                self._queue_manager_resume(root, message)
                            elif data.get("manager_resume"):
                                self._transition(root, "paused", "manager_no_progress")
                                self._message(project, cid, "任务已暂停：对话协调 Agent 本轮没有推进已授权流程。可在运行数据中查看该 Run 后继续任务。", task=root["id"])
                if "recovered_output" in data:
                    data.pop("recovered_output")
                    self._save_projection(project, "queue_context", queued["id"], data)
                current = self.store.get(queued["id"], project_id=project)
                if blocked:
                    update(self.store, current, state="blocked", blocked_reason=blocked, created_task_id=task["id"])
                else:
                    event = self._event(project, "control.applied" if current["mode"] == "steer" else "queue.dispatched",
                        {"request_id": current["id"], "source_message_id": message["id"], "receipts": receipts}, conversation=cid, task=task["id"], run=run["id"], source=message["id"])
                    update(self.store, current, state="applied" if current["mode"] == "steer" else "dispatched",
                           adopted_run_id=run["id"], created_task_id=next((r["task_id"] for r in receipts if r.get("task_id")), task["id"]), resolved_at=self.store.now(), blocked_reason=None)
                    gate = self._control(project, cid)
                    gate["last_applied_event_seq"] = event["sequence"]
                    self._save_projection(project, "conversation_control", cid, gate)
        except asyncio.CancelledError:
            if not self._closing:
                self._fail_execution(task, run, "user_stop")
        except Exception as error:
            self._fail_execution(task, run, getattr(error, "reason", None) or getattr(error, "code", "configuration_error"), str(error), getattr(error, "details", None))

    def _continuation_wait_target(self, selected, message, stage, chapter):
        """Route an ancestor's continuation only to a uniquely shown waiting item."""
        if selected["conversation_id"] != message["conversation_id"]:
            raise WorkflowBlocked("continuation_target_ambiguous")
        if selected["state"] != "waiting_user":
            return selected
        own = [p for p in self._task_data(selected).get("pending_user_items", []) if p["state"] == "open"]
        if own:
            return selected  # The normal handler still enforces confirmation rules.
        tasks = all_records(self.store, message["project_id"], "task", conversation_id=message["conversation_id"])
        descendants = {selected["id"]}
        while True:
            expanded = descendants | {t["id"] for t in tasks if t["parent_task_id"] in descendants}
            if expanded == descendants:
                break
            descendants = expanded
        candidates, unshown = [], False
        for child in tasks:
            if child["id"] == selected["id"] or child["id"] not in descendants or child["state"] != "waiting_user":
                continue
            if (stage is not None and child["scope"]["stage"] != stage) or (chapter and chapter not in child["scope"]["chapter_ids"]):
                continue
            for pending in self._task_data(child).get("pending_user_items", []):
                if pending["state"] != "open" or pending["task_id"] != child["id"]:
                    continue
                shown = [self.store.get(i, project_id=message["project_id"]) for i in pending["presented_message_ids"]]
                if not shown or any(not h or h["conversation_id"] != message["conversation_id"] or h["role"] != "assistant"
                                    or h["sequence"] >= message["sequence"] for h in shown):
                    unshown = True
                    continue
                candidates.append((child, pending))
        text = body(self.store, message)
        explicit = [(child, item) for child, item in candidates if item["id"] in text]
        chosen = explicit if explicit else candidates
        if not candidates:
            if unshown:
                raise WorkflowBlocked("continuation_target_ambiguous")
            return selected
        if len(chosen) != 1:
            raise WorkflowBlocked("continuation_target_ambiguous")
        child, pending = chosen[0]
        if pending["kind"] == "confirmation":
            raise WorkflowBlocked("confirmation_required")
        if pending["kind"] not in ("question", "decision") or len([p for p in self._task_data(child).get("pending_user_items", []) if p["state"] == "open"]) != 1:
            raise WorkflowBlocked("continuation_target_ambiguous")
        self._event(message["project_id"], "control.continuation_routed", {
            "requested_task_ref": ref(selected), "resolved_task_ref": ref(child), "pending_item_id": pending["id"],
            "source_message_id": message["id"], "basis": "explicit_pending_id" if explicit else "sole_shown_pending_in_scope"},
            conversation=message["conversation_id"], task=child["id"], source=message["id"])
        return child

    def _apply_request(self, request, message, presented, coordinator_task, run, steered_task=None):
        project, cid = message["project_id"], message["conversation_id"]
        if request["source_message_ids"] != [message["id"]]:
            raise WorkflowBlocked("invalid_request_source")
        intent, stage, chapter = request["intent"], request["stage"], request["chapter_id"]
        if intent == "query":
            return {"status": "answered", "task_id": coordinator_task["id"]}
        if intent == "confirm":
            target = request.get("target_ref")
            for candidate in all_records(self.store, project, "task", conversation_id=cid):
                plan = self._task_data(candidate).get("chapter_plan")
                if plan and target and target["record_id"] == plan["decision_id"] and target["version"] == str(plan["version"]):
                    return self._confirm_chapters(candidate, plan, request, message, presented, run)
            confirmation, version, complete = self.workflow.confirm(project, request, message, presented, run["id"])
            self._event(project, "confirmation.recorded", {"confirmation_id": confirmation["id"], "subject": ref(version), "complete": complete}, conversation=cid, source=message["id"])
            for waiting in all_records(self.store, project, "task", conversation_id=cid):
                data = self._task_data(waiting)
                tracked = ([data["result_ref"]] if data.get("result_ref") else []) + list(data.get("result_refs", {}).values())
                if not any(candidate.get("record_id") == version["artifact_id"]
                           and candidate.get("version") == str(version["version"]) for candidate in tracked):
                    continue
                if complete:
                    for item in data.get("pending_user_items", []):
                        matches = any(target.get("subject", {}).get("record_id") == version["artifact_id"]
                                      and target.get("subject", {}).get("version") == str(version["version"])
                                      for target in item.get("targets", []))
                        if item["kind"] == "confirmation" and matches:
                            all_confirmed = all(
                                self.workflow.state(self.workflow.fixed_version(project, target["subject"]))
                                ["confirmation_status"] == "confirmed"
                                for target in item.get("targets", []))
                            if all_confirmed:
                                item.update(state="resolved", answer_message_ids=[message["id"]])
                    self._save_task_data(waiting, data)
                    remaining = any(item["state"] == "open" for item in data.get("pending_user_items", []))
                    pending_versions = [self.workflow.fixed_version(project, fixed_ref)
                                        for fixed_ref in data.get("result_refs", {}).values()]
                    all_results_confirmed = all(self.workflow.state(saved)["confirmation_status"] in
                                                ("confirmed", "not_required") for saved in pending_versions)
                    if not remaining and all_results_confirmed and self.workflow.state(version)["dependency_status"] == "valid" and waiting["state"] == "waiting_user":
                        self._transition(waiting, "succeeded", source=message["id"])
                        self.workflow.writeback_plan(project, version, data["stage"])
            self._unblock_requests(project, cid, ("confirmation_required", "missing_material"))
            return {"status": "confirmed" if complete else "partially_confirmed", "confirmation_id": confirmation["id"], "task_id": coordinator_task["id"]}
        if intent == "continue":
            target = request.get("target_ref")
            selected = self.store.get(target["record_id"], project_id=project) if target else steered_task
            if not selected or selected["record_type"] != "task":
                candidates = [t for t in all_records(self.store, project, "task", conversation_id=cid)
                              if t["state"] == "waiting_user" and t["scope"]["stage"] == stage and (not chapter or chapter in t["scope"]["chapter_ids"])
                              and not self._task_data(t).get("waiting_batch_task_id")]
                if len(candidates) != 1:
                    raise WorkflowBlocked("continuation_target_ambiguous")
                selected = candidates[0]
            selected = self._continuation_wait_target(selected, message, stage, chapter)
            if selected["state"] in ("paused", "stopped"):
                self.control_task(project, selected["id"], "continue")
            elif selected["state"] == "waiting_user":
                data = self._task_data(selected)
                if any(p["kind"] == "confirmation" and p["state"] == "open" for p in data.get("pending_user_items", [])):
                    raise WorkflowBlocked("confirmation_required")
                # A clarification can reopen generation; the agent may ask for remaining missing input.
                data["request"] = data.get("request", "") + f"\n用户补充（{message['id']}）：{body(self.store, message)}"
                for item in data.get("pending_user_items", []):
                    if item["kind"] in ("question", "decision") and item["state"] == "open":
                        item.update(state="resolved", answer_message_ids=[message["id"]])
                if data.get("batch_owned") and data.get("current_run_id"):
                    data.setdefault("answered_needs_input_run_ids", []).append(data["current_run_id"])
                    data.pop("resume_saved_result", None)
                self._save_task_data(selected, data)
                self._transition(selected, "queued", source=message["id"])
                if data.get("batch_owned") and selected["parent_task_id"]:
                    parent = self.store.get(selected["parent_task_id"], project_id=project)
                    parent_data = self._task_data(parent)
                    if parent["state"] == "waiting_user" and parent_data.get("waiting_batch_task_id") == selected["id"]:
                        parent_data.pop("waiting_batch_task_id")
                        self._save_task_data(parent, parent_data)
                        self._transition(parent, "queued", source=message["id"])
            return {"status": "continued", "task_id": selected["id"]}
        queued = next((q for q in all_records(self.store, project, 'queued_request', conversation_id=cid)
                       if q['source_message_id'] == message['id']), None)
        if self._effective_holds(project, self._control(project, cid)) and not (queued and self._single_queue_release_valid(queued)):
            raise WorkflowBlocked("queue_hold")
        if stage and stage in (9, 10) and not chapter:
            raise WorkflowBlocked("chapter_scope_required")
        if stage:
            self.workflow.materials(project, stage, chapter)
        ongoing = [t for t in all_records(self.store, project, "task", conversation_id=cid)
                   if self._task_data(t).get("is_workflow") and t["state"] in ("queued", "running", "waiting_user")]
        if ongoing and stage and intent in ("generate", "jump"):
            root = ongoing[-1]
            if stage not in self._task_data(root).get("stages", range(1, 12)):
                raise WorkflowBlocked("outside_authorized_scope")
            child = self._dispatch(root, stage, chapter, request["request"])
            return {"status": "scheduled", "task_id": child["id"]}
        previous_scopes = all_records(self.store, project, "runtime_event", event_name="chapters.confirmed")
        previous_scope = max(previous_scopes, key=lambda e: e["sequence"])["payload"] if previous_scopes else None
        try: current_source = ref(self.workflow.resolve(project, "source_text"))
        except WorkflowBlocked: current_source = None
        inherited_chapters = previous_scope["chapter_ids"] if previous_scope and previous_scope.get("source_ref") == current_source else []
        root = self._new_task(project, cid, message, intent, request=request["request"], is_workflow=True,
                              source_ref=current_source, stages=[stage] if stage else list(range(1, 12)),
                              chapter_ids=[chapter] if chapter else inherited_chapters)
        if intent == "modify" and request.get("target_ref"):
            previous = self.workflow.fixed_version(project, request["target_ref"])
            if stage is None:
                artifact = self.store.get(previous["artifact_id"], project_id=project)
                stage = artifact["scope"]["stage"]
                data = self._task_data(root)
                data["stages"] = [stage]
                self._save_task_data(root, data)
            child = self._dispatch(root, stage, chapter, request["request"], regenerate=True)
            self._event(project, "dependency.change_requested", {"previous_ref": ref(previous), "request": request["request"]}, conversation=cid, task=child["id"], source=message["id"])
            self._pause_dependents(project, previous, child["id"])
        return {"status": "scheduled", "task_id": root["id"]}

    def _unblock_requests(self, project, conversation, reasons):
        for queued in all_records(self.store, project, "queued_request", conversation_id=conversation):
            if queued["state"] == "blocked" and queued["blocked_reason"] in reasons:
                update(self.store, queued, state="pending", blocked_reason=None)

    def _queue_manager_resume(self, root, message):
        """Queue one idempotent manager continuation for an applied user message."""
        # The caller may have just transitioned this Task (e.g. chapter-plan
        # confirmation). Always inspect the current row before a second state
        # change; the passed snapshot may carry an obsolete row_version.
        root = self.store.get(root["id"], project_id=root["project_id"])
        if not self._task_data(root).get("manager_controlled"):
            return None
        project, conversation = root["project_id"], root["conversation_id"]
        key = f"{project}:manager_resume:{message['id']}"
        saved = self.store.projection_get(key)
        if saved:
            return self.store.get(saved["queue_id"], project_id=project)
        if root["state"] == "waiting_user":
            self._transition(root, "queued", source=message["id"])
        # A user message already owns its queue sequence. A manager turn that
        # ends before the authorized workflow is done needs a new, explicitly
        # program-owned continuation rather than a second queue entry for it.
        existing = [item for item in all_records(self.store, project, "queued_request", conversation_id=conversation)
                    if item["sequence"] == message["sequence"]]
        queue_message = (self._message(project, conversation, "系统继续已授权的改编任务", role="assistant", visibility="internal")
                         if existing else message)
        queued = self.store.put(new_record("queued_request", project,
            conversation_id=conversation, source_message_id=queue_message["id"], mode="queue",
            target_run_id=None, sequence=queue_message["sequence"],
            scope=scope(description="确认后继续由对话协调 Agent 调度"), state="pending",
            adopted_run_id=None, adopted_context_snapshot_id=None, created_task_id=None,
            resolved_at=None, blocked_reason=None))
        presented = self._projection(project, "presentations", conversation, targets=[])
        self._save_projection(project, "queue_context", queued["id"], {
            "presented": deepcopy(presented["targets"]),
            "request_hash": hashlib.sha256(canonical_bytes([root["id"], message["id"], "manager_resume"])).hexdigest(),
            "manager_resume": True, "origin_message_id": message["id"]})
        self.store.projection_put(key, {"project_id": project, "queue_id": queued["id"]})
        return queued

    def _pause_dependents(self, project, previous, excluding):
        for task in all_records(self.store, project, "task"):
            if task["id"] == excluding or task["state"] not in ("queued", "running", "waiting_user"):
                continue
            data = self._task_data(task)
            if data.get("coordinator") and not data.get("chapter_planning"):
                # Reading the old version to interpret a revision request is not
                # production that depends on that version remaining effective.
                continue
            run = self.store.get(task["current_run_id"], project_id=project) if task["current_run_id"] else None
            if run and any(r["record_id"] == previous["artifact_id"] and r["version"] == str(previous["version"]) for r in run["input_refs"]):
                self._transition(task, "paused", "dependency_changed")

    def _propose_chapters(self, root, requests, message, reply, run):
        ids = [r["chapter_id"] for r in requests]
        if len(ids) != len(set(ids)):
            raise WorkflowBlocked("duplicate_chapter_id")
        if any(r["source_message_ids"] != [message["id"]] for r in requests):
            raise WorkflowBlocked("invalid_request_source")
        decision_id = str(uuid4())
        decision = self.store.put(new_record("decision", root["project_id"], decision_id=decision_id,
            version=1, topic="全剧章节范围与顺序", value={"storage": "inline_json", "value": {"chapter_ids": ids, "requests": requests}},
            origin="agent", status="proposed", reason=reply, scope=root["scope"], source_refs=[ref(message)], supersedes_ref=None))
        target = {"record_id": decision_id, "version": "1", "item_id": None, "json_pointer": None}
        shown = self._message(root["project_id"], root["conversation_id"], reply + "\n请确认以上章节列表和顺序，或提出调整。", task=root["id"], run=run["id"])
        data = self._task_data(root)
        data["chapter_plan"] = {"decision_id": decision_id, "record_id": decision["id"], "version": 1, "chapter_ids": ids, "requests": requests,
                                "source_ref": ref(self.workflow.resolve(root["project_id"], "source_text"))}
        data["pending_user_items"] = [self._wait_item(root, "confirmation", shown, "确认章节列表与顺序", [{"subject": target, "selections": [WHOLE]}])]
        self._save_task_data(root, data)
        presentations = self._projection(root["project_id"], "presentations", root["conversation_id"], targets=[])
        presentations["targets"].append({"subject": target, "selections": [WHOLE], "message_id": shown["id"], "task_id": root["id"]})
        self._save_projection(root["project_id"], "presentations", root["conversation_id"], presentations)

    def _confirm_chapters(self, root, plan, request, message, presented, run):
        if self._task_data(root).get("confirmed_chapter_plan_ref"):
            return {"status": "already_confirmed", "task_id": root["id"]}
        if not any(p["subject"]["record_id"] == plan["decision_id"] and p["subject"]["version"] == str(plan["version"]) for p in presented):
            raise WorkflowBlocked("confirmation_not_presented")
        if "" not in request["requested_confirmation_paths"] and "/value" not in request["requested_confirmation_paths"]:
            raise WorkflowBlocked("chapter_plan_partial_confirmation")
        if plan.get("source_ref") != ref(self.workflow.resolve(root["project_id"], "source_text")):
            raise WorkflowBlocked("dependency_changed")
        target = {"record_id": plan["decision_id"], "version": str(plan["version"]), "item_id": None, "json_pointer": None}
        confirmation = self.store.put(new_record("confirmation", root["project_id"], subject=target,
            selections=[WHOLE], action="confirm", basis="user_statement", source_message_ids=[message["id"]],
            carried_from_confirmation_ids=[], scope_mapping_ref=None, revokes_confirmation_ids=[], applied_run_id=run["id"] if run else None))
        data = self._task_data(root)
        proposed = self.store.get(plan["record_id"], project_id=root["project_id"])
        approved = self.store.put(new_record("decision", root["project_id"], decision_id=proposed["decision_id"],
            version=proposed["version"] + 1, topic=proposed["topic"], value=proposed["value"], origin=proposed["origin"],
            status="confirmed", reason=proposed["reason"], scope=proposed["scope"],
            source_refs=[ref(proposed), ref(message), ref(confirmation)], supersedes_ref=ref(proposed)))
        data["confirmed_chapter_plan_ref"] = ref(approved)
        data["chapter_ids"] = plan["chapter_ids"]
        for item in data["pending_user_items"]:
            item.update(state="resolved", answer_message_ids=[message["id"]])
        self._save_task_data(root, data)
        self._transition(root, "queued", source=message["id"])
        self._event(root["project_id"], "chapters.confirmed", {"chapter_ids": plan["chapter_ids"], "confirmation_id": confirmation["id"], "source_ref": plan["source_ref"], "decision_ref": ref(approved)}, conversation=root["conversation_id"], task=root["id"], source=message["id"])
        if run is None:
            self._queue_manager_resume(root, message)
        return {"status": "confirmed", "task_id": root["id"]}

    async def _consume_steers(self, task, run, token):
        targets = {run["id"], *self._task_data(task).get("recovery_run_ids", [])}
        queued = sorted([q for q in all_records(self.store, task["project_id"], "queued_request") if q["target_run_id"] in targets], key=lambda q: q["sequence"])
        for request in queued:
            if request["state"] == "pending":
                await self._coordinate(request, token, steered_task=task)
        unresolved = [q for q in all_records(self.store, task["project_id"], "queued_request")
                      if q["target_run_id"] in targets and q["state"] in ("pending", "blocked")]
        if unresolved:
            raise WorkflowBlocked("control_unresolved", {"request_ids": [q["id"] for q in unresolved]})

    def _fixed_config(self, task, stage):
        data = self._task_data(task)
        config = self.store.get(data["config_version_id"], project_id=task["project_id"]) if data.get("config_version_id") else self.config_service.resolve(task["project_id"], f"step{stage}")
        data["config_version_id"] = config["id"]
        self._save_task_data(task, data)
        return config

    def _delivered_baseline(self, project):
        deliveries = all_records(self.store, project, "runtime_event", event_name="project.delivered")
        if not deliveries:
            return None, None
        target = max(deliveries, key=lambda e: e["sequence"])["payload"]["artifact_ref"]
        version = self.workflow.fixed_version(project, target)
        profile = body(self.store, version)
        sidecar = self._projection(project, "graph_sidecar", version["id"]).get("original")
        if sidecar:
            from .graph import restore_sidecar
            profile = restore_sidecar(profile, sidecar)
        return profile, target

    def _assemble(self, task):
        config = self._fixed_config(task, 11)
        schemas = config["values"].get("schemas")
        data = self._task_data(task)
        root = self.store.get(data["workflow_root_task_id"], project_id=task["project_id"])
        roster = self._task_data(root).get("chapter_ids", [])
        if not roster:
            raise WorkflowBlocked("chapter_scope_required")
        versions = [self.workflow.resolve(task["project_id"], "chapter_graph", c) for c in roster]
        plan = self.workflow.resolve(task["project_id"], "adaptation_plan")
        plan_body = body(self.store, plan)["payload"]
        project = self.store.get(task["project_id"], project_id=task["project_id"])
        baseline, baseline_ref = self._delivered_baseline(task["project_id"])
        graph, sidecar = assemble_project(project_id=project.get("nexo_project_id") or project["id"],
            name=plan_body["title"], description=plan_body["logline"], prompt="", updated_at=self.store.now(),
            chapter_order=roster, chapters=[body(self.store, v) for v in versions], entity_catalog=plan_body["entity_specs"], schemas=schemas, baseline=baseline)
        candidate = self.workflow.save(task["project_id"], "nexo_graph", graph, stage=11, origin="program",
                                       inputs=[ref(plan), *[ref(v) for v in versions], *([baseline_ref] if baseline_ref else [])], config=config,
                                       historical_inputs=[baseline_ref] if baseline_ref else [])
        event = self._event(task["project_id"], "graph.checked", {"artifact_ref": ref(candidate), "checks": quality_checks(graph, schemas)}, conversation=task["conversation_id"], task=task["id"])
        state = self.workflow.state(candidate)
        update(self.store, state, check_record_ids=[event["id"]])
        data["candidate_ref"] = ref(candidate)
        data["graph_check_ref"] = ref(event)
        self._save_task_data(task, data)
        if sidecar:
            self._save_projection(task["project_id"], "graph_sidecar", candidate["id"], {"original": sidecar})
        return candidate

    def _graph_contract_material(self, task):
        content = load_graph_contract()
        event = self._event(task["project_id"], "graph.contract_frozen", content,
                            conversation=task["conversation_id"], task=task["id"])
        return {"schema_id": "runtime.graph_contract", "builtin": "runtime.graph_contract", "required": True,
                "ref": ref(event), "content": content, "projection_pointer": "/payload"}

    def _graph_write_materials(self, task):
        config = self._fixed_config(task, 10)
        schemas = config["values"].get("schemas")
        project = task["project_id"]
        data = self._task_data(task)
        root = self.store.get(data["workflow_root_task_id"], project_id=project)
        roster = self._task_data(root).get("chapter_ids", []) or [data["chapter_id"]]
        plan = self.workflow.resolve(project, "adaptation_plan")
        planned = body(self.store, plan)["payload"]
        identity = self.store.get(project, project_id=project)
        baseline = {"id": identity.get("nexo_project_id") or project, "name": planned["title"],
                    "description": planned["logline"], "prompt": "", "revision": 0, "updatedAt": self.store.now(),
                    "chapters": [], "chapterEdges": [], "variables": [], "scenes": []}
        sources = [ref(plan)]
        delivered, delivered_ref = self._delivered_baseline(project)
        if delivered:
            baseline, sidecar = project_profile(delivered, schemas)
            prior_order = [c["id"] for c in baseline["chapters"]]
            roster = list(dict.fromkeys(prior_order + roster))
            sources.append(delivered_ref)
        for chapter in roster:
            try:
                previous = self.workflow.resolve(project, "chapter_graph", chapter)
            except WorkflowBlocked:
                continue
            baseline = merge_chapter(baseline, body(self.store, previous), chapter_id=chapter,
                                     chapter_order=roster, entity_catalog=planned["entity_specs"], schemas=schemas)
            sources.append(ref(previous))
        version = self.workflow.save(project, "nexo_graph", baseline, stage=10, origin="program", inputs=sources, config=config)
        update(self.store, self.workflow.state(version), confirmation_status="not_required", effective_selections=[WHOLE])
        metadata = self._event(project, "graph.metadata_reserved", {"project_id": baseline["id"],
            "chapter_ids": roster, "revision": baseline["revision"], "updatedAt": baseline["updatedAt"],
            "prompt": baseline["prompt"], "entity_catalog": planned["entity_specs"]},
            conversation=task["conversation_id"], task=task["id"])
        content = {"baseline_ref": ref(version), "target_chapter_id": data["chapter_id"],
                   "reserved_identity_refs": [ref(metadata)], "metadata_ref": ref(metadata),
                   "change_scope": {"chapter_ids": [data["chapter_id"]], "removed_node_ids": [], "removed_edge_ids": [],
                                    "existing_identity_policy": "reuse", "new_identity_policy": "task-local-unique"}}
        context = self._event(project, "graph.write_context_created", content, conversation=task["conversation_id"], task=task["id"])
        data["baseline_ref"] = ref(version)
        data["graph_write_ref"] = ref(context)
        self._save_task_data(task, data)
        # The frozen baseline and write context are consumed by the Harness
        # when validating and merging chapter_graph, not by the Step10 model.
        return []

    def _review_materials(self, task):
        project = task["project_id"]
        data = self._task_data(task)
        config = self.store.get(data["config_version_id"], project_id=project) if data.get("config_version_id") else self.config_service.resolve(project, "step11")
        data["config_version_id"] = config["id"]
        candidate = self.workflow.fixed_version(project, data["candidate_ref"])
        content = {"reviewed_artifact_refs": [data["candidate_ref"]], "criteria_ref": ref(config)}
        event = self._event(project, "review.requested", content, conversation=task["conversation_id"], task=task["id"])
        data["review_context_ref"] = ref(event)
        self._save_task_data(task, data)
        return [{"schema_id": "nexo_graph", "kind": "nexo_graph", "required": True,
                 "ref": ref(candidate), "content": body(self.store, candidate),
                 "state": self.workflow.state(candidate)}]

    def _deliver_script_validated(self, task, candidate, config):
        """Deliver without a model call when the optional validator is disabled."""
        graph = body(self.store, candidate)
        checks = quality_checks(graph, config["values"].get("schemas"))
        if any(check["status"] != "pass" for check in checks):
            raise GraphValidationError(checks)
        state = self.workflow.state(candidate)
        update(self.store, state, confirmation_status="not_required", quality_status="passed",
               effective_selections=[WHOLE])
        artifact = self.store.get(candidate["artifact_id"], project_id=task["project_id"])
        update(self.store, artifact, current_effective_version=candidate["version"])
        data = self._task_data(task)
        self._event(task["project_id"], "project.delivered", {
            "artifact_ref": ref(candidate), "review_ref": None,
            "check_ref": data["graph_check_ref"], "validation_agent_enabled": False,
        }, conversation=task["conversation_id"], task=task["id"])
        self._message(task["project_id"], task["conversation_id"],
            "脚本校验已通过：Graph 符合 schema_contract，且不存在不可达节点。校验 Agent 未启用，最终 Nexo Graph JSON 已交付。",
            task=task["id"])
        self._transition(self.store.get(task["id"], project_id=task["project_id"]), "succeeded")

    def _deliver_or_repair(self, task, run, version, report):
        data = self._task_data(task)
        payload = report["payload"]
        candidate = self.workflow.fixed_version(task["project_id"], data["candidate_ref"])
        target = data["candidate_ref"]
        reviewed = any(r["record_id"] == target["record_id"] and r["version"] == target["version"] for r in payload["reviewed_artifact_refs"])
        blocking = [f for f in payload["findings"] if f["severity"] in ("blocker", "major")]
        root = self.store.get(data["workflow_root_task_id"], project_id=task["project_id"])
        roster = set(self._task_data(root).get("chapter_ids", []))
        if not reviewed or payload["unchecked_scope"]:
            raise WorkflowBlocked("review_scope_incomplete")
        if any(chapter not in roster for chapter in payload["checked_scope"]):
            raise WorkflowBlocked("review_scope_format_invalid", {
                "expected_chapter_ids": sorted(roster),
                "actual_checked_scope": deepcopy(payload["checked_scope"]),
                "instruction": "checked_scope只填实际完整审读的裸章节ID，不写说明句或未知ID。保留真实审核发现、findings与proposed_verdict；未读范围不能伪装已读，未完整审读的章节应留在unchecked_scope。只修正范围表达，不为满足覆盖而填写未读章节。",
            })
        if not roster <= set(payload["checked_scope"]):
            raise WorkflowBlocked("review_scope_incomplete")
        expected_criteria = ref(self.store.get(run["config_version_id"], project_id=task["project_id"]))
        if payload["criteria_ref"] != expected_criteria:
            raise WorkflowBlocked("review_criteria_mismatch")
        if payload["graph_checks"]:
            raise WorkflowBlocked("unexecuted_check_claimed_pass")
        review_message = self._message(task["project_id"], task["conversation_id"],
            stage_result_text(11, report, version=version["version"]), task=task["id"], run=run["id"])
        self._event(task["project_id"], "artifact.presented",
                    {"artifact_ref": ref(version), "message_id": review_message["id"], "selections": [WHOLE]},
                    conversation=task["conversation_id"], task=task["id"], run=run["id"])
        if blocking or payload["proposed_verdict"] != "pass":
            self._checkpoint(task, run, "await_user")
            self._close_run(task, run, "succeeded")
            current = self.store.get(task["id"], project_id=task["project_id"])
            config = self.store.get(run["config_version_id"], project_id=task["project_id"])["values"]
            if current["repair_rounds_used"] >= _cfg(config, "repair.max_rounds", 2) + data.get("additional_repair_rounds", 0):
                self._transition(current, "paused", "repair_exhausted")
                return
            self._transition(current, "waiting_user")
            root = self.store.get(data["workflow_root_task_id"], project_id=task["project_id"])
            concrete = bool(blocking) and all(f["suggested_fix"].strip() and f["graph_targets"]
                and all(g["chapter_id"] in self._task_data(root).get("chapter_ids", []) for g in f["graph_targets"])
                and f["suggested_owner"] in ("chapter_writer", "章节生成 Agent", "step10", "chapter_designer", "step9") for f in blocking)
            if concrete:
                grouped = {}
                for finding in blocking:
                    stage = 9 if finding["suggested_owner"] in ("chapter_designer", "step9") else 10
                    for target in finding["graph_targets"]:
                        grouped.setdefault((stage, target["chapter_id"]), []).append(finding)
                repair_ids = []
                for (stage, chapter), findings in grouped.items():
                    child = self._dispatch(root, stage, chapter,
                        "根据固定审核报告修复明确问题，保留其他正文/ID/已确认设计。若修复要求新的创作取舍，返回needs_input，不自行替换确认。\n" + json.dumps(findings, ensure_ascii=False), regenerate=True)
                    repair_ids.append(child["id"])
                current = self.store.get(task["id"], project_id=task["project_id"])
                update(self.store, current, repair_rounds_used=current["repair_rounds_used"] + 1)
                data["repair_task_ids"] = repair_ids
                self._save_task_data(task, data)
                self._event(task["project_id"], "review.repair_scheduled", {"review_ref": ref(version), "task_ids": repair_ids}, conversation=task["conversation_id"], task=task["id"])
                return
            data["pending_user_items"] = [self._wait_item(task, "decision", review_message, "依据审核问题确定修改范围与创作取舍")]
            self._save_task_data(task, data)
            return
        graph = body(self.store, candidate)
        checks = quality_checks(graph, self.store.get(run["config_version_id"], project_id=task["project_id"])["values"].get("schemas"))
        if any(c["status"] != "pass" for c in checks) or not self._dependencies_valid(run):
            raise WorkflowBlocked("delivery_invalid")
        state = self.workflow.state(candidate)
        update(self.store, state, confirmation_status="not_required", quality_status="passed", effective_selections=[WHOLE])
        artifact = self.store.get(candidate["artifact_id"], project_id=task["project_id"])
        update(self.store, artifact, current_effective_version=candidate["version"])
        self._event(task["project_id"], "project.delivered", {
            "artifact_ref": ref(candidate), "review_ref": ref(version),
            "check_ref": data["graph_check_ref"], "validation_agent_enabled": True,
        }, conversation=task["conversation_id"], task=task["id"], run=run["id"])
        self._message(task["project_id"], task["conversation_id"], "脚本校验和校验 Agent 均已通过，最终 Nexo Graph JSON 已交付。", task=task["id"], run=run["id"])
        self._checkpoint(task, run, "deliver")
        self._close_run(task, run, "succeeded")
        self._transition(self.store.get(task["id"], project_id=task["project_id"]), "succeeded")

    async def _recover(self, project, conversation):
        with self.store.transaction():
            self.store.advisory_lock(f"{project}:conversation:{conversation}")
            now = self.store.now()
            for task in all_records(self.store, project, "task", conversation_id=conversation):
                if self._task_data(task).get("parent_owned") or self._task_data(task).get("batch_owned"):
                    continue
                if not task["current_run_id"]:
                    continue
                old = self.store.get(task["current_run_id"], project_id=project)
                if old["state"] in CLOSED_RUNS or (old["lease_expires_at"] and old["lease_expires_at"] > now):
                    continue
                data = self._task_data(task)
                update(self.store, old, state="interrupted", finished_at=now, lease_owner=None, lease_expires_at=None, fencing_token=old["fencing_token"] + 1)
                session = self.store.get(old["session_id"], project_id=project)
                update(self.store, session, lease_owner=None, lease_expires_at=None, fencing_token=session["fencing_token"] + 1)
                task = update(self.store, task, current_run_id=None)
                if data.get("stop", {}).get("requested") or task["state"] == "stopping":
                    self._transition(task, "stopped", "user_stop")
                    continue
                if task["state"] in ("paused", "stopped", "waiting_user", "succeeded", "failed"):
                    continue
                config = self.store.get(old["config_version_id"], project_id=project)["values"]
                maximum = _cfg(config, "recovery.max_attempts", 3)
                if data.get("recovery_attempts_used", 0) >= maximum:
                    self._transition(task, "paused", "recovery_exhausted")
                    continue
                if not task["latest_checkpoint_id"]:
                    self._transition(task, "paused", "checkpoint_invalid")
                    continue
                data["recovery_attempts_used"] = data.get("recovery_attempts_used", 0) + 1
                event = self._event(project, "recovery.claimed", {"previous_run_id": old["id"], "attempt": data["recovery_attempts_used"]}, conversation=conversation, task=task["id"])
                data["recovery_run_ids"] = list(dict.fromkeys(data.get("recovery_run_ids", []) + [old["id"]]))
                config = self.store.get(old["config_version_id"], project_id=project)["values"]
                backoff = _cfg(config, "recovery.backoff_seconds", [5, 30, 120])
                model_calls = [c for c in all_records(self.store, project, "model_call", task_id=task["id"])
                               if c["run_id"] in data["recovery_run_ids"]]
                used = len({c["operation_id"] for c in model_calls})
                data["allowance"]["used"] = used
                data.update(previous_run_id=old["id"], last_recovery_event_id=event["id"],
                            remaining_turns=max(0, data["allowance"]["limit"] - used),
                            next_retry_at=_after(now, backoff[min(data["recovery_attempts_used"] - 1, len(backoff) - 1)]))
                self._save_task_data(task, data)
                archived_result = self._projection(project, "run_result", old["id"]).get("output")
                result = self._saved_output(task, old["id"])
                if data.get("resume_saved_result") and self._output_rejected(task, data["resume_saved_result"]["source_run_id"]):
                    data.pop("resume_saved_result")
                    self._save_task_data(task, data)
                calls = all_records(self.store, project, "model_call", run_id=old["id"])
                uncertain = [c for c in calls if c["state"] in ("pending", "running", "unknown")]
                if uncertain or (archived_result is None and data.get("model_dispatched")):
                    for call in uncertain:
                        if call["state"] != "unknown":
                            update(self.store, call, state="unknown")
                    self._transition(task, "paused", "operation_uncertain")
                    continue
                if result is not None and not data.get("coordinator"):
                    stage = data["stage"]
                    if data.get("parent_owned"):
                        self._transition(task, "paused", "child_blocked")
                        continue
                    if not self._dependencies_valid(old):
                        self._transition(task, "paused", "dependency_changed")
                        continue
                    # Resume through the normal commit path, including pending Steer interpretation.
                    data["resume_saved_result"] = {"source_run_id": old["id"], "output": result}
                    self._save_task_data(task, data)
                    self._transition(task, "queued")
                elif result is not None:
                    queue = self._projection(project, "queue_context", data["queue_id"])
                    queue["recovered_output"] = result
                    self._save_projection(project, "queue_context", data["queue_id"], queue)
                    self._transition(task, "queued")
                else:
                    self._transition(task, "queued")
