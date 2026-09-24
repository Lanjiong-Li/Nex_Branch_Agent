"""Explicit, evidence-backed repair of the pre-fix SDK terminal classification.

This maintenance operation never dispatches a model or resumes a task. It only
recognizes the exact SDK terminal diagnostic; unknown transport outcomes stay put.
"""
from copy import deepcopy
import re

from .workflow import all_records, update


_LEGACY_OUTPUT_LIMIT = re.compile(
    r"(?:模型输出不符合结构约定：)?Responses stream ended with terminal event "
    r"`response\.incomplete`\. status=incomplete; "
    r"incomplete_details=IncompleteDetails\(reason=['\"]max_output_tokens['\"]\)\."
)


def reconcile_legacy_output_limit(engine, project_id, task_id):
    store = engine.store
    with store.transaction():
        task = store.get(task_id, project_id=project_id)
        if not task or task["record_type"] != "task":
            return {"changed": False}
        store.advisory_lock(f"{project_id}:conversation:{task['conversation_id']}")
        task = store.get(task_id, project_id=project_id)
        if task["state"] != "paused" or task["pause_reason"] != "operation_uncertain":
            return {"changed": False}
        runs = all_records(store, project_id, "run", task_id=task_id)
        if not runs:
            return {"changed": False}
        run = max(runs, key=lambda r: (r["created_at"], r["id"]))
        error = run.get("error") or {}
        if (run["state"] != "failed" or error.get("code") != "output_validation_failed"
            or not _LEGACY_OUTPUT_LIMIT.fullmatch(error.get("message", ""))
            or engine._projection(project_id, "run_result", run["id"]).get("output") is not None):
            return {"changed": False}
        calls = all_records(store, project_id, "model_call", run_id=run["id"])
        unresolved = [c for c in calls if c["state"] in ("pending", "running", "unknown")]
        if len(unresolved) != 1:
            return {"changed": False}
        call = unresolved[0]
        if (call["task_id"] != task_id or call["state"] != "unknown"
            or (call.get("error") or {}).get("code") != "ModelBehaviorError"
            or call["response_history_ids"] or call["provider_response_id"]):
            return {"changed": False}

        config = store.get(run["config_version_id"], project_id=project_id)["values"]
        limit = config["model"]["max_output_tokens"]
        message = f"模型输出达到 {limit} tokens 上限（含推理），响应未完成。"
        details = {"known_outcome": True, "terminal_status": "incomplete",
                   "terminal_event": "response.incomplete", "incomplete_reason": "max_output_tokens",
                   "max_output_tokens": limit, "evidence_kind": "legacy_sdk_terminal_error",
                   "model_call_id": call["id"]}
        corrected_error = {"code": "output_limit_exceeded", "message": message,
                           "retryable": False, "details": details}
        event = engine._event(project_id, "model.failure_reclassified", {
            "before": {"task": deepcopy(task), "run": deepcopy(run), "model_call": deepcopy(call)},
            "classification": corrected_error, "usage_preserved": True, "automatically_resumed": False,
        }, conversation=task["conversation_id"], task=task_id, run=run["id"])
        # Do not invent missing usage, response IDs or provider archives. Updating
        # classification leaves the recorded start/finish times and budgets intact.
        update(store, call, state="failed", error=corrected_error)
        update(store, run, error=corrected_error)
        task = engine._transition(task, "paused", "output_limit_exceeded")
        usage = call["usage"]
        if engine.cost_gates_enabled and usage.get("reported_cost") is None and usage.get("estimated_cost") is None:
            engine._hold(task, "usage_uncertain", event["id"])
            message += "旧版本未保存此次调用的实际用量，费用仍待核实。"
        engine._message(project_id, task["conversation_id"],
                        "错误原因已更正：" + message + "原有进度已保留，任务仍暂停，未自动重跑。",
                        task=task_id, run=run["id"])
        return {"changed": True, "event_id": event["id"], "task_id": task_id,
                "run_id": run["id"], "model_call_id": call["id"], "reason": "output_limit_exceeded"}
