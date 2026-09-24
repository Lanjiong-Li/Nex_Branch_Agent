"""Strict record constructors. These helpers never invent business evidence."""
from __future__ import annotations

import copy
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from jsonschema import Draft202012Validator, FormatChecker

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = json.loads((ROOT / "docs/harness-data/records.schema.json").read_text())
DEFINITIONS = SCHEMA["$defs"]
KINDS = {value["properties"]["record_type"]["const"]: name
         for name, value in DEFINITIONS.items() if "record_type" in value.get("properties", {})}
VALIDATORS = {kind: Draft202012Validator({"$ref": f"#/$defs/{name}", "$defs": DEFINITIONS},
                                      format_checker=FormatChecker()) for kind, name in KINDS.items()}


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                      allow_nan=False).encode("utf-8")


def content_hash(content: dict) -> str:
    if content["storage"] == "inline_text":
        raw = content["text"].encode("utf-8")
    elif content["storage"] == "inline_json":
        raw = canonical_bytes(content["value"])
    else:
        raise ValueError("Blob content hash must be obtained from its persisted Blob record")
    return hashlib.sha256(raw).hexdigest()


def validate_record(record: dict) -> None:
    kind = record.get("record_type")
    if kind not in VALIDATORS:
        raise ValueError(f"Unknown record_type: {kind!r}")
    canonical_bytes(record)
    errors = sorted(VALIDATORS[kind].iter_errors(record), key=lambda e: str(list(e.path)))
    if errors:
        error = errors[0]
        path = "/" + "/".join(map(str, error.path))
        raise ValueError(f"{kind}{path}: {error.message}")
    if record["id"] == "00000000-0000-0000-0000-000000000000":
        raise ValueError("Reserved zero UUID is not a business identity")
    if kind == "project" and record["project_id"] != record["id"]:
        raise ValueError("Project.project_id must equal its id")
    if kind == "config_version":
        if (record["project_id"] is None) != (record["scope_kind"] == "global"):
            raise ValueError("Only global configuration can have project_id=null")
        if record["scope_kind"] == "global" and not record.get("owner_account_id"):
            raise ValueError("Account-global configuration requires owner_account_id")
        if record["scope_kind"] != "global" and record.get("owner_account_id") is not None:
            raise ValueError("Project configuration cannot declare owner_account_id")
    if kind == "checkpoint" and record["cursor"]["handler_version"] == "runtime.v1":
        checkpoint_schema = json.loads((ROOT / "docs/runtime/checkpoint-state.schema.json").read_text())
        Draft202012Validator(checkpoint_schema, format_checker=FormatChecker()).validate(record["cursor"]["state"])


def scope(stage: int | None = None, chapter_id: str | None = None, **kwargs) -> dict:
    result = {"stage": stage, "chapter_ids": [chapter_id] if chapter_id else [],
              "branch_ids": [], "target_refs": [], "description": "当前任务范围"}
    result.update(copy.deepcopy(kwargs))
    Draft202012Validator({"$ref": "#/$defs/Scope", "$defs": DEFINITIONS},
                         format_checker=FormatChecker()).validate(result)
    return result


def usage(**kwargs) -> dict:
    result = {field: None for field in DEFINITIONS["Usage"]["properties"]}
    result.update(copy.deepcopy(kwargs))
    Draft202012Validator({"$ref": "#/$defs/Usage", "$defs": DEFINITIONS}).validate(result)
    return result


def budget(**kwargs) -> dict:
    result = {"max_active_seconds": 3600, "max_cost": {"amount": "20", "currency": "USD"},
              "disabled_limits": []}
    result.update(copy.deepcopy(kwargs))
    Draft202012Validator({"$ref": "#/$defs/Budget", "$defs": DEFINITIONS}).validate(result)
    return result


DEFAULTS = {
    "project": {"state": "active"},
    "conversation": {"state": "active", "last_message_seq": 0},
    "history_record": {"visibility": "conversation", "kind": "message"},
    "work_session": {"state": "active", "generation": 1, "last_item_seq": 0, "fencing_token": 0},
    "session_item": {"generation": 1},
    "artifact": {"latest_version": 0},
    "artifact_version": {"version": 1},
    "artifact_state": {"version": 1, "confirmation_status": "unconfirmed", "dependency_status": "review_required", "quality_status": "unchecked"},
    "decision": {"version": 1, "status": "proposed"},
    "confirmation": {"action": "confirm", "basis": "user_statement"},
    "task": {"state": "queued", "repair_rounds_used": 0},
    "run": {"state": "created", "execution_kind": "runner", "max_turns": 10, "model_turns_used": 0, "fencing_token": 0},
    "queued_request": {"state": "pending"},
    "checkpoint": {"session_generation": 1, "repair_rounds_used": 0},
    "config_version": {"version": 1, "state": "draft"},
    "model_call": {"state": "pending", "attempt": 1, "turn_index": 1},
    "tool_call": {"state": "pending", "attempt": 1},
    "dependency": {"state": "review_required"},
    "blob": {"state": "pending"},
    "runtime_event": {"actor_kind": "program"},
    "idempotency_record": {"state": "in_progress"},
}


def new_record(record_kind: str, project_id: str | None, **values) -> dict:
    kind = record_kind
    if kind not in KINDS:
        kind = next((key for key, name in KINDS.items() if name == kind), kind)
    if kind not in KINDS:
        raise ValueError(f"Unknown record kind: {kind}")
    props = DEFINITIONS[KINDS[kind]]["properties"]
    timestamp = now_utc()
    identity = values.get("id", str(uuid4()))
    result = {"record_type": kind, "schema_version": "1.0.0", "id": identity,
              "project_id": identity if kind == "project" else project_id, "created_at": timestamp}
    if kind == "project" and project_id not in (None, identity):
        raise ValueError("A Project cannot belong to another project")
    for name, spec in props.items():
        if name in result:
            continue
        if spec.get("type") == "array":
            result[name] = []
        elif any(variant.get("type") == "null" for variant in spec.get("anyOf", [])):
            result[name] = None
    if "row_version" in props:
        result.update(updated_at=timestamp, row_version=1)
    if "scope" in props:
        result["scope"] = scope()
    if "usage" in props:
        result["usage"] = usage()
    if "budget" in props:
        result["budget"] = budget()
    result.update(copy.deepcopy(DEFAULTS.get(kind, {})))
    result.update(copy.deepcopy(values))
    if kind == "task" and "budget_root_task_id" not in result and result.get("parent_task_id") is None:
        result["budget_root_task_id"] = identity
    if kind == "config_version" and "values" in result and "sha256" not in result:
        result["sha256"] = hashlib.sha256(canonical_bytes(result["values"])).hexdigest()
    if kind == "artifact_version" and "content" in result and "content_sha256" not in result:
        if result["content"]["storage"] != "blob":
            result["content_sha256"] = content_hash(result["content"])
    validate_record(result)
    return result
