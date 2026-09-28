"""Versioned workflow operations. No model text is an authorization record."""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from uuid import uuid4

from .records import new_record, scope
from .graph import SCHEMA_DIR, validate_output
from .schemas import SchemaCatalog

STAGES = {1: "source_global_events", 2: "source_knowledge_asset", 3: "adaptation_strategy",
          4: "adaptation_plan", 5: "game_event_view", 6: "game_event_view",
          7: "ending_routes", 8: "player_profiles", 9: "chapter_design",
          10: "chapter_graph", 11: "review_report"}
STAGE_OUTPUTS = {
    1: ("source_global_events", "source_character_events"),
}
AGENTS = {1: "source_parser", 2: "source_knowledge_analyst", 3: "adaptation_planner",
          4: "adaptation_planner", 5: "interaction_architect", 6: "interaction_architect",
          7: "interaction_architect", 8: "interaction_architect", 9: "chapter_designer",
          10: "chapter_writer", 11: "validation_agent"}
REQUIRES = {1: ["source_text"], 2: ["source_global_events", "source_character_events"],
            3: ["source_knowledge_asset"],
            4: ["source_knowledge_asset", "adaptation_strategy"],
            5: ["source_global_events", "source_character_events", "source_knowledge_asset"],
            6: ["game_event_view"],
            7: ["game_event_view"],
            8: ["game_event_view", "ending_routes"],
            9: ["adaptation_plan", "source_text"],
            10: ["chapter_design", "source_text"], 11: ["nexo_graph"]}
PROGRAM_REQUIRES = {5: ["adaptation_plan"], 6: ["adaptation_plan"],
                    7: ["adaptation_plan"], 8: ["adaptation_plan"]}
WHOLE = {"item_id": None, "json_pointer": ""}
PLAIN_ARTIFACTS = {"source_text"}


class WorkflowBlocked(ValueError):
    def __init__(self, reason, details=None):
        super().__init__(reason)
        self.reason, self.details = reason, details or {}


def ref(record, pointer=None):
    business = record.get("record_type") in ("artifact_version", "decision")
    return {"record_id": record.get("artifact_id", record.get("decision_id", record["id"])),
            "version": str(record["version"]) if business else None,
            "item_id": None, "json_pointer": pointer}


def update(store, record, **changes):
    value = deepcopy(record)
    value.update(changes)
    return store.update(value, expected_version=record["row_version"])


def all_records(store, project_id, kind=None, **filters):
    result, offset = [], 0
    while True:
        batch = store.list(project_id, record_type=kind, limit=200, offset=offset, filters=filters or None)
        result.extend(batch)
        if len(batch) < 200:
            return result
        offset += len(batch)


def body(store, record):
    content = record["content"]
    if content["storage"] == "inline_json":
        return content["value"]
    if content["storage"] == "inline_text":
        return content["text"]
    raw = store.blob_read(content["blob_id"], project_id=record["project_id"])
    return raw.decode("utf-8") if isinstance(raw, bytes) else raw


class Workflow:
    def __init__(self, store):
        self.store = store
        self.registry = json.loads((SCHEMA_DIR / "registry.json").read_text())
        self.catalog = SchemaCatalog()

    def binding(self, kind):
        entries = self.registry.get("schemas", self.registry.get("contracts", []))
        entry = next((e for e in entries if e["schema_id"] == kind), None)
        if entry is None:
            # registry key is deliberately resolved without changing contract IDs.
            entry = next((e for v in self.registry.values() if isinstance(v, list)
                          for e in v if isinstance(e, dict) and e.get("schema_id") == kind), None)
        return {k: entry[k] for k in ("schema_id", "version", "sha256")} if entry else None

    def versions(self, project_id, artifact_id):
        return sorted(all_records(self.store, project_id, "artifact_version", artifact_id=artifact_id),
                      key=lambda v: v["version"])

    def state(self, version):
        states = all_records(self.store, version["project_id"], "artifact_state", artifact_version_id=version["id"])
        return states[0] if states else None

    def resolve(self, project_id, kind, chapter_id=None, effective=True):
        artifacts = all_records(self.store, project_id, "artifact", artifact_kind=kind)
        artifacts = [a for a in artifacts if not chapter_id or chapter_id in a["scope"]["chapter_ids"]]
        if not artifacts:
            raise WorkflowBlocked("missing_material", {"kind": kind, "chapter_id": chapter_id})
        artifact = max(artifacts, key=lambda a: a["created_at"])
        number = artifact["current_effective_version"] if effective else artifact["latest_version"]
        if not number:
            raise WorkflowBlocked("confirmation_required", {"artifact_id": artifact["id"]})
        version = next(v for v in self.versions(project_id, artifact["id"]) if v["version"] == number)
        state = self.state(version)
        if effective and state["dependency_status"] != "valid":
            raise WorkflowBlocked("dependency_changed", {"artifact_id": artifact["id"]})
        return version

    def materials(self, project_id, stage, chapter_id=None, input_kinds=None):
        # Program-only prerequisites gate the stage but are deliberately not
        # exposed to the model or frozen into its ContextSnapshot.
        selected_kinds = list(REQUIRES[stage] if input_kinds is None else input_kinds)
        if len(selected_kinds) != len(set(selected_kinds)):
            raise WorkflowBlocked("invalid_stage_inputs", {"stage": stage})
        plan = None
        for kind in PROGRAM_REQUIRES.get(stage, ()):
            prerequisite = self.resolve(project_id, kind)
            if kind == "adaptation_plan":
                plan = prerequisite
        if stage == 9:
            plan = self.resolve(project_id, "adaptation_plan")
        indexed_events = self.planned_game_events(project_id, plan=plan) if stage in (6, 7, 8, 9) else None
        indexed_routes = self.planned_ending_routes(project_id, plan=plan) if stage in (8, 9) else None
        indexed_profiles = self.planned_player_profiles(project_id, plan=plan) if stage == 9 else None
        global_events = (self.resolve(project_id, "source_global_events")
                         if stage in (2, 5) and ("source_global_events" in selected_kinds
                                                  or stage == 5 and "source_text" in selected_kinds) else None)
        character_events = (self.resolve(project_id, "source_character_events")
                            if stage in (2, 5) and "source_character_events" in selected_kinds else None)
        if stage in (2, 5) and global_events and character_events:
            self.require_approved_step1_inputs(project_id, [
                {"kind": "source_global_events", "record": global_events},
                {"kind": "source_character_events", "record": character_events},
            ])
        chapter_design = self.resolve(project_id, "chapter_design", chapter_id) if stage == 10 else None
        if global_events:
            if character_events and ref(self.original_for(project_id, global_events)) != ref(self.original_for(project_id, character_events)):
                raise WorkflowBlocked("source_reference_mismatch", {"kind": "step1_views"})
            global_payload = body(self.store, global_events)["payload"]
            if "source_global_events" in selected_kinds and any(
                    not isinstance(event.get("analysis"), str) or not event["analysis"].strip()
                    for event in global_payload["global_events"]):
                raise WorkflowBlocked("source_event_analysis_migration_required",
                                      {"kind": "source_global_events", "reason": "old_global_view"})
        if stage == 5 and "source_global_events" in selected_kinds:
            if any(not event.get("source_anchors") for event in global_payload["global_events"]):
                raise WorkflowBlocked("source_index_migration_required", {"kind": "source_global_events"})
        if indexed_events:
            if any("source_anchors" not in event or (event.get("adaptation_kind") != "new" and not event["source_anchors"])
                   for event in body(self.store, indexed_events)["payload"]["events"]):
                raise WorkflowBlocked("source_index_migration_required", {"kind": "game_event_view"})
        if chapter_design and "chapter_source_anchors" not in body(self.store, chapter_design)["payload"]:
            raise WorkflowBlocked("source_index_migration_required", {"kind": "chapter_design"})
        if stage in (7, 8, 9):
            events = body(self.store, indexed_events)["payload"]["events"]
            if not events or any(not isinstance(event.get("narrative_function"), str) or
                                 not event["narrative_function"].strip() for event in events):
                raise WorkflowBlocked("missing_material", {"kind": "game_events.narrative_function"})
        if stage in (8, 9) and not any(source.get("record_id") == ref(indexed_events)["record_id"] and
                                     source.get("version") == ref(indexed_events)["version"]
                                     for source in indexed_routes["source_refs"]):
            raise WorkflowBlocked("dependency_changed", {"kind": "ending_routes.game_events"})
        if stage == 9 and not all(any(source.get("record_id") == version["artifact_id"] and
                                         source.get("version") == str(version["version"])
                                         for source in indexed_profiles["source_refs"])
                                  for version in (indexed_events, indexed_routes)):
            raise WorkflowBlocked("dependency_changed", {"kind": "player_profiles.stage_artifact_refs"})
        items = []
        for kind in selected_kinds:
            # A candidate full Project is frozen before audit but not yet deliverable.
            if stage == 9 and kind == "adaptation_plan":
                version = plan
            elif stage in (7, 8, 9) and kind == "game_event_view":
                version = indexed_events
            elif stage in (8, 9) and kind == "ending_routes":
                version = indexed_routes
            elif stage == 9 and kind == "player_profiles":
                version = indexed_profiles
            elif kind == "source_text" and stage == 5:
                version = self.original_for(project_id, global_events)
            elif kind == "source_text" and stage == 9:
                version = self.original_for(project_id, indexed_events)
            elif kind == "source_text" and stage == 10:
                version = self.original_for(project_id, chapter_design)
            elif kind == "chapter_design" and stage == 10:
                version = chapter_design
            elif kind in ("chapter_design", "chapter_graph"):
                version = self.resolve(project_id, kind, chapter_id,
                                       effective=kind != "chapter_graph")
            elif kind == "nexo_graph":
                version = self.resolve(project_id, kind, effective=False)
            else:
                version = self.resolve(project_id, kind, chapter_id if kind == "chapter_design" else None,
                                       effective=kind != "nexo_graph")
            if stage == 6 and kind == "game_event_view" and ref(version) != ref(indexed_events):
                raise WorkflowBlocked("dependency_changed", {"kind": "adaptation_plan.stage_artifact_refs.game_events"})
            items.append({"ref": ref(version), "kind": kind, "schema_id": kind, "required": True, "record": version,
                          "content": body(self.store, version), "state": self.state(version)})
        # The current version is a real artifact. Its producing stage may use
        # it as fixed input without skipping generation of a new version.
        if stage in (3, 4):
            current_kind = STAGES[stage]
            try:
                current = self.resolve(project_id, current_kind)
            except WorkflowBlocked as error:
                if error.reason not in ("missing_material", "confirmation_required", "dependency_changed"):
                    raise
            else:
                items.append({"ref": ref(current), "kind": current_kind, "schema_id": current_kind,
                              "required": True, "record": current, "content": body(self.store, current),
                              "state": self.state(current), "material_role": "current_stage_baseline"})
        return items

    def require_approved_step1_inputs(self, project_id, materials):
        """Reject unapproved or superseded views, including fixed Run recovery inputs."""
        kinds = ("source_global_events", "source_character_events")
        versions = {}
        for kind in kinds:
            matches = [material for material in materials
                       if (material.get("kind") or material.get("schema_id")) == kind]
            if len(matches) != 1:
                raise WorkflowBlocked("invalid_stage_inputs", {"kind": kind})
            material = matches[0]
            versions[kind] = (material.get("record") or
                              self.fixed_version(project_id, material["ref"]))
        current_pair = {kind: ref(version) for kind, version in versions.items()}
        for kind, version in versions.items():
            if ref(self.resolve(project_id, kind)) != current_pair[kind]:
                raise WorkflowBlocked("dependency_changed", {"kind": kind})
        source_ref = ref(self.original_for(project_id, versions[kinds[0]]))
        if ref(self.original_for(project_id, versions[kinds[1]])) != source_ref:
            raise WorkflowBlocked("source_reference_mismatch", {"kind": "step1_views"})
        if any(version.get("producer_run_id") for version in versions.values()):
            if any(self.state(version)["confirmation_status"] != "confirmed"
                   for version in versions.values()):
                raise WorkflowBlocked("confirmation_required", {"kind": "step1_views"})
        reviews = []
        for task in all_records(self.store, project_id, "task"):
            data = self.store.projection_get(f"{project_id}:task_runtime:{task['id']}") or {}
            review = data.get("step1_review")
            if review and review.get("source_ref") == source_ref:
                reviews.append(data)
        if reviews and not any(data.get("step1_approved_refs") == current_pair for data in reviews):
            raise WorkflowBlocked("confirmation_required", {"kind": "step1_views"})

    @staticmethod
    def require_fixed_source_event_analysis(stage, materials):
        """Reject pre-change Step1 inputs frozen by a Step2/5 Run."""
        if stage not in (2, 5):
            return
        def kind(material):
            return material.get("kind") or material.get("schema_id")
        if any(kind(item) == "source_global_analysis" for item in materials):
            raise WorkflowBlocked("source_event_analysis_migration_required",
                                  {"reason": "old_analysis_artifact"})
        global_views = [item for item in materials if kind(item) == "source_global_events"]
        if len(global_views) > 1 or (stage == 2 and not global_views):
            raise WorkflowBlocked("invalid_stage_inputs",
                                  {"stage": stage, "kind": "source_global_events"})
        if stage == 2 and sum(kind(item) == "source_character_events" for item in materials) != 1:
            raise WorkflowBlocked("invalid_stage_inputs",
                                  {"stage": stage, "kind": "source_character_events"})
        if not global_views:
            return
        content = global_views[0].get("content")
        payload = content.get("payload") if isinstance(content, dict) else None
        events = payload.get("global_events") if isinstance(payload, dict) else None
        if not isinstance(events, list) or any(
                not isinstance(event, dict) or not isinstance(event.get("analysis"), str)
                or not event["analysis"].strip() for event in events):
            raise WorkflowBlocked("source_event_analysis_migration_required",
                                  {"reason": "old_global_view"})

    def original_for(self, project_id, version, _seen=None):
        """Follow fixed provenance to the original text; never select the newest import."""
        _seen = set() if _seen is None else _seen
        identity = (version["artifact_id"], version["version"])
        if identity in _seen:
            raise WorkflowBlocked("missing_material", {"kind": "source_text", "reason": "provenance_cycle"})
        _seen.add(identity)
        artifact = self.store.get(version["artifact_id"], project_id=project_id)
        kind = artifact["artifact_kind"]
        if kind == "source_text":
            return version
        if kind in ("source_global_events", "source_character_events"):
            target = body(self.store, version)["payload"]["source_ref"]
            source = self.fixed_version(project_id, target)
            if self.store.get(source["artifact_id"], project_id=project_id)["artifact_kind"] != "source_text":
                raise WorkflowBlocked("source_reference_mismatch")
            return source
        for target in version.get("source_refs", []):
            if target.get("version") is None:
                continue
            parent = self.fixed_version(project_id, target)
            parent_kind = self.store.get(parent["artifact_id"], project_id=project_id)["artifact_kind"]
            if parent_kind in ("source_text", "source_global_events", "source_character_events"):
                return self.original_for(project_id, parent, _seen)
            try:
                return self.original_for(project_id, parent, _seen)
            except WorkflowBlocked as error:
                if error.reason != "missing_material":
                    raise
        raise WorkflowBlocked("missing_material", {"kind": "source_text", "artifact_kind": kind})

    def _planned_artifact(self, project_id, field, kind, plan=None):
        """Resolve a confirmed fixed version from the current plan."""
        plan = plan or self.resolve(project_id, "adaptation_plan")
        target = body(self.store, plan)["payload"]["stage_artifact_refs"].get(field)
        if not target:
            raise WorkflowBlocked("missing_material", {"kind": f"adaptation_plan.stage_artifact_refs.{field}"})
        version = self.fixed_version(project_id, target)
        artifact = self.store.get(version["artifact_id"], project_id=project_id)
        state = self.state(version)
        if artifact["artifact_kind"] != kind or ref(version) != target:
            raise WorkflowBlocked("fixed_material_reference_invalid", {"kind": kind})
        if not state or state["dependency_status"] != "valid" or state["confirmation_status"] not in ("confirmed", "not_required"):
            raise WorkflowBlocked("dependency_changed", {"kind": kind})
        return version

    def planned_game_events(self, project_id, plan=None):
        return self._planned_artifact(project_id, "game_events", "game_event_view", plan)

    def planned_ending_routes(self, project_id, plan=None):
        return self._planned_artifact(project_id, "ending_routes", "ending_routes", plan)

    def planned_player_profiles(self, project_id, plan=None):
        return self._planned_artifact(project_id, "player_profiles", "player_profiles", plan)

    def save(self, project_id, kind, content, *, stage=None, chapter_id=None, run=None,
             inputs=(), origin="model", effective=False, quality="passed", artifact_id=None, config=None, historical_inputs=()):
        custom = config["values"].get("schemas") if config else None
        if run:
            config = self.store.get(run["config_version_id"], project_id=project_id)
            custom = config["values"].get("schemas")
        plain_content = kind in PLAIN_ARTIFACTS and isinstance(content, str)
        if kind not in PLAIN_ARTIFACTS:
            self.catalog.validate(kind, content, custom)
            self.validate_evidence(project_id, content)
        elif not isinstance(content, str) or not content.strip():
            raise ValueError(f"{kind} 必须是非空文本")
        existing = all_records(self.store, project_id, "artifact", artifact_kind=kind)
        existing = [a for a in existing if a["scope"]["chapter_ids"] == ([chapter_id] if chapter_id else [])]
        artifact = self.store.get(artifact_id, project_id=project_id) if artifact_id else (existing[0] if existing else None)
        if artifact is None:
            artifact = self.store.put(new_record("artifact", project_id, artifact_kind=kind,
                scope=scope(stage=stage, chapter_id=chapter_id, description=kind),
                latest_version=0, current_effective_version=None))
        previous = artifact["latest_version"]
        previous_effective = artifact["current_effective_version"]
        encoded = content.encode() if plain_content else json.dumps(content, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        version = new_record("artifact_version", project_id, artifact_id=artifact["id"],
            version=previous + 1, parent_version=previous or None,
            content={"storage": "inline_text", "text": content} if plain_content else {"storage": "inline_json", "value": content},
            content_sha256=hashlib.sha256(encoded).hexdigest(),
            output_schema=None if kind in PLAIN_ARTIFACTS else self.catalog.binding(kind, custom), source_refs=list(inputs), dependency_ids=[],
            producer_run_id=run["id"] if run else None,
            config_version_id=run["config_version_id"] if run else config["id"] if config else None, origin=origin)
        dependencies = []
        for source in inputs:
            if source.get("version") and source["record_id"] != artifact["id"] and not any(
                    all(source[k] == historical[k] for k in ("record_id", "version"))
                    for historical in historical_inputs):
                dep = new_record("dependency", project_id, consumer_ref=ref(version), producer_ref=source,
                    relation="source" if kind in ("source_global_events", "source_character_events") else "material",
                    consumer_selections=[WHOLE], producer_selections=[WHOLE], state="valid", assessment_ref=None)
                dependencies.append(dep)
        version["dependency_ids"] = [d["id"] for d in dependencies]
        self.store.put(version)
        for dep in dependencies:
            self.store.put(dep)
        self.store.put(new_record("artifact_state", project_id, artifact_id=artifact["id"],
            artifact_version_id=version["id"], version=version["version"],
            confirmation_status="not_required" if effective else "unconfirmed",
            dependency_status="valid", quality_status=quality,
            effective_selections=[WHOLE] if effective else [], confirmation_ids=[], check_record_ids=[]))
        update(self.store, artifact, latest_version=version["version"],
               current_effective_version=version["version"] if effective else artifact["current_effective_version"])
        if effective and previous_effective:
            old = next(v for v in self.versions(project_id, artifact["id"]) if v["version"] == previous_effective)
            self.invalidate_changed(old, version)
        return version

    def validate_evidence(self, project_id, content):
        from .context import _refs, pointer_values, _identity, MaterialError, unwrap
        from .storage import InvalidRecord
        for evidence in _refs(content):
            try:
                target = self.store.resolve_ref(evidence, project_id)
            except InvalidRecord as error:
                # Only fixed output-reference failures are model repair feedback.
                # Storage/configuration invariant failures keep their original type.
                if str(error) not in {
                    "EvidenceRef.version must be a positive decimal version",
                    "Logical reference missing in project",
                    "Referenced record/version does not exist in this project",
                    "Business references require a logical ID and explicit version",
                }:
                    raise
                raise WorkflowBlocked("evidence_reference_invalid", {
                    "reference": deepcopy(evidence), "validation_error": str(error),
                }) from error
            value = body(self.store, target) if target["record_type"] == "artifact_version" else target
            path = evidence.get("json_pointer")
            if path is not None:
                if "*" in path.split("/"):
                    raise WorkflowBlocked("evidence_pointer_not_concrete")
                try:
                    pointer_values(value, path, required=True)
                except MaterialError as error:
                    if error.code != "missing_material_field":
                        raise
                    raise WorkflowBlocked("evidence_pointer_missing", {
                        "reference": deepcopy(evidence), "json_pointer": path,
                        "validation_error": str(error),
                    }) from error
            identity = evidence.get("item_id")
            if identity:
                matches = []
                def visit(node, prefix=""):
                    if isinstance(node, dict):
                        if _identity(node) == identity:
                            matches.append(prefix)
                        for key, child in node.items():
                            visit(child, prefix + "/" + str(key).replace("~", "~0").replace("/", "~1"))
                    elif isinstance(node, list):
                        for i, child in enumerate(node): visit(child, prefix + "/" + str(i))
                visit(value)
                matches = [p for p in matches if path is None or path == p or path.startswith(p + "/") or p.startswith(path.rstrip("/") + "/")]
                if len(matches) != 1:
                    raise WorkflowBlocked("evidence_item_ambiguous_or_missing", {"reference": evidence})

    def fixed_version(self, project_id, target):
        if not target or not target.get("version"):
            raise WorkflowBlocked("fixed_version_required")
        versions = self.versions(project_id, target["record_id"])
        version = next((v for v in versions if str(v["version"]) == target["version"]), None)
        if version is None:
            raise WorkflowBlocked("version_not_found")
        return version

    def confirm(self, project_id, request, message, presented, run_id=None):
        """Only current user evidence and the submitted-time presentation are eligible."""
        if message["role"] != "user" or request["source_message_ids"] != [message["id"]]:
            raise WorkflowBlocked("invalid_confirmation_source")
        target = request.get("target_ref")
        version = self.fixed_version(project_id, target)
        artifact = self.store.get(version["artifact_id"], project_id=project_id)
        state = self.state(version)
        if artifact["latest_version"] != version["version"] or state["dependency_status"] != "valid":
            raise WorkflowBlocked("confirmation_candidate_changed")
        if (artifact["artifact_kind"] in ("source_global_events", "source_character_events")
                and artifact["current_effective_version"] != version["version"]):
            raise WorkflowBlocked("confirmation_candidate_changed")
        matching = [p for p in presented if p["subject"]["record_id"] == target["record_id"]
                    and p["subject"]["version"] == target["version"]]
        if not matching:
            raise WorkflowBlocked("confirmation_not_presented")
        paths = request.get("requested_confirmation_paths") or []
        if not paths:
            raise WorkflowBlocked("confirmation_scope_required")
        if (artifact["artifact_kind"] in ("source_global_events", "source_character_events")
                and not any(path in ("", "/payload") for path in paths)):
            raise WorkflowBlocked("confirmation_scope_incomplete")
        value = body(self.store, version)
        selections = []
        shown = {s["json_pointer"] for p in matching for s in p["selections"]}
        for path in paths:
            if "" not in shown and not any(path == p or path.startswith(p + "/") for p in shown):
                raise WorkflowBlocked("confirmation_scope_not_presented")
            pointed = json_pointer(value, path)
            item_id = None
            if isinstance(pointed, dict):
                item_id = next((v for k, v in pointed.items() if k.endswith("_id") and isinstance(v, str)), None)
            if item_id is None and path:
                parts = path.split("/")
                for length in range(len(parts) - 1, 1, -1):
                    parent = json_pointer(value, "/".join(parts[:length]))
                    if isinstance(parent, dict):
                        item_id = next((v for k, v in parent.items() if (k.endswith("_id") or k == "id") and isinstance(v, str)), None)
                        if item_id:
                            break
            selections.append({"item_id": item_id, "json_pointer": path})
        confirmation = self.store.put(new_record("confirmation", project_id, subject=ref(version),
            selections=selections, action="confirm", basis="user_statement",
            source_message_ids=[message["id"]], carried_from_confirmation_ids=[],
            scope_mapping_ref=None, revokes_confirmation_ids=[], applied_run_id=run_id))
        selections = state["effective_selections"] + [s for s in selections if s not in state["effective_selections"]]
        complete = "" in {s["json_pointer"] for s in selections}
        # A full payload is the complete business result; envelope metadata is never a user decision.
        complete |= "/payload" in {s["json_pointer"] for s in selections}
        selected_paths = [s["json_pointer"] for s in selections]
        if artifact["artifact_kind"] == "adaptation_strategy":
            required = ["/payload/player_identity", "/payload/user_ideas", "/payload/strategy_basis"]
        else:
            required = ["/payload/" + field for field in value.get("payload", {})]
        complete |= bool(required) and all(any(path == chosen or path.startswith(chosen + "/") for chosen in selected_paths) for path in required)
        state = update(self.store, state, confirmation_status="confirmed" if complete else "partial",
            effective_selections=selections, confirmation_ids=state["confirmation_ids"] + [confirmation["id"]])
        self._record_confirmed_decisions(project_id, version, message, confirmation, selections)
        if complete and state["dependency_status"] == "valid":
            artifact = self.store.get(version["artifact_id"], project_id=project_id)
            old_number = artifact["current_effective_version"]
            update(self.store, artifact, current_effective_version=version["version"])
            if old_number and old_number != version["version"]:
                old = next(v for v in self.versions(project_id, artifact["id"]) if v["version"] == old_number)
                self.invalidate_changed(old, version)
        return confirmation, version, complete

    def _record_confirmed_decisions(self, project, version, message, confirmation, selections):
        artifact = self.store.get(version["artifact_id"], project_id=project)
        fields = {"adaptation_strategy": ("player_identity", "strategy_basis", "user_ideas", "constraints"),
                  "adaptation_plan": ("player_role", "narrative_constraints", "world_and_character_changes")}.get(artifact["artifact_kind"], ())
        for field in fields:
            path = "/payload/" + field
            if not any(s["json_pointer"] == "" or path == s["json_pointer"] or path.startswith(s["json_pointer"].rstrip("/") + "/") for s in selections):
                continue
            value = json_pointer(body(self.store, version), path)
            topic = artifact["artifact_kind"] + ":" + field
            previous = [d for d in all_records(self.store, project, "decision", topic=topic)
                        if d["scope"]["chapter_ids"] == artifact["scope"]["chapter_ids"]]
            old = max(previous, key=lambda d: d["version"]) if previous else None
            content = {"storage": "inline_json", "value": value}
            if old and old["status"] == "confirmed" and old["value"] == content:
                continue
            self.store.put(new_record("decision", project, decision_id=old["decision_id"] if old else str(uuid4()),
                version=old["version"] + 1 if old else 1, topic=topic, value=content, origin="agent", status="confirmed",
                reason="从用户实际确认的产物范围登记", scope=artifact["scope"],
                source_refs=[ref(message), ref(confirmation), ref(version, path)], supersedes_ref=ref(old) if old else None))

    def invalidate_changed(self, old, new):
        project = old["project_id"]
        a, b = body(self.store, old), body(self.store, new)
        artifact = self.store.get(old["artifact_id"], project_id=project)
        if artifact["artifact_kind"] == "adaptation_plan" and new["origin"] == "program":
            left, right = deepcopy(a), deepcopy(b)
            left["payload"].pop("stage_artifact_refs", None)
            right["payload"].pop("stage_artifact_refs", None)
            if left == right:
                return []  # Program index writeback has no independent creative change.
        dependencies = all_records(self.store, project, "dependency")
        changed, queue, seen = [], [(ref(old), True)], set()
        while queue:
            producer, compare_fields = queue.pop(0)
            marker = (producer["record_id"], producer["version"])
            if marker in seen: continue
            seen.add(marker)
            for dep in dependencies:
                if any(dep["producer_ref"][k] != producer[k] for k in ("record_id", "version")):
                    continue
                if compare_fields and all(_same_selection(a, b, selection["json_pointer"]) for selection in dep["producer_selections"]):
                    continue
                live = self.store.get(dep["id"], project_id=project)
                if live["state"] != "review_required": update(self.store, live, state="review_required")
                try:
                    consumer = self.fixed_version(project, dep["consumer_ref"])
                    state = self.state(consumer)
                    if state["dependency_status"] != "review_required":
                        update(self.store, state, dependency_status="review_required")
                        changed.append(consumer)
                    queue.append((ref(consumer), False))
                except WorkflowBlocked:
                    pass
        return changed

    def writeback_plan(self, project_id, confirmed, stage):
        if stage not in (5, 6, 7, 8):
            return None
        # Writeback is a Harness index update.  It must remain possible even
        # when the creative plan is marked review_required by an upstream
        # artifact change; the program creates a new version and preserves the
        # existing plan content while updating only stage_artifact_refs.
        try:
            previous = self.resolve(project_id, "adaptation_plan")
        except WorkflowBlocked as error:
            if error.reason != "dependency_changed":
                raise
            previous = self.resolve(project_id, "adaptation_plan", effective=False)
        value = deepcopy(body(self.store, previous))
        field = {5: "game_events", 6: "game_events", 7: "ending_routes", 8: "player_profiles"}[stage]
        if value["payload"]["stage_artifact_refs"][field] == ref(confirmed):
            return previous
        value["payload"]["stage_artifact_refs"][field] = ref(confirmed)
        if stage == 6 and "event_functions" in value["payload"]["stage_artifact_refs"]:
            # The former standalone event_function_map is retired. Keep the
            # legacy slot for old plan versions, but never point it at a new
            # artifact; the enriched game_event_view is the single source.
            value["payload"]["stage_artifact_refs"]["event_functions"] = None
        # The writeback is a Harness action triggered by the confirmed stage.
        # Retain that stage's config snapshot so the latest plan version reports
        # its actual origin and uses the same published schema contract.
        config = (self.store.get(confirmed["config_version_id"], project_id=project_id)
                  if confirmed.get("config_version_id") else None)
        return self.save(project_id, "adaptation_plan", value, stage=4, origin="program",
                         inputs=[ref(previous), ref(confirmed)], effective=True, config=config)


def _same_selection(a, b, path):
    try:
        return json_pointer(a, path) == json_pointer(b, path)
    except (ValueError, KeyError, IndexError, TypeError):
        return False


def json_pointer(value, path):
    if not path:
        return value
    if not path.startswith("/"):
        raise WorkflowBlocked("invalid_json_pointer")
    for token in path[1:].split("/"):
        key = token.replace("~1", "/").replace("~0", "~")
        value = value[int(key)] if isinstance(value, list) else value[key]
    return value


def session_key(stage, chapter_id=None, sharing=None):
    if isinstance(stage, str) and stage.startswith("step") and stage[4:].isdigit():
        stage = int(stage[4:])
    explicit = isinstance(stage, int) and sharing is not None and f"step{stage}" in sharing
    if explicit:
        current = f"step{stage}"
        seen = set()
        while sharing.get(current):
            if current in seen:
                raise WorkflowBlocked("session_sharing_cycle", {"stage": current})
            seen.add(current)
            current = sharing[current]
        if current.startswith("step") and current[4:].isdigit():
            stage = int(current[4:])
    if stage == "coordinator":
        return "coordinator"
    if explicit:
        if stage == 2:
            return "adaptation_direction"
        if stage == 5:
            return "interactive_structure"
        if stage == 7:
            return "ending_routes"
        if stage == 8:
            return "player_profiles"
        if stage in (9, 10):
            if not chapter_id:
                raise WorkflowBlocked("chapter_scope_required")
            return f"chapter:{chapter_id}" if stage == 9 else f"chapter:{chapter_id}:writer"
        return f"step{stage}"
    if stage in (2, 3, 4):
        return "adaptation_direction"
    if stage in (5, 6):
        return "interactive_structure"
    if stage == 7:
        return "ending_routes"
    if stage == 8:
        return "player_profiles"
    if stage in (9, 10):
        if not chapter_id:
            raise WorkflowBlocked("chapter_scope_required")
        return f"chapter:{chapter_id}" if stage == 9 else f"chapter:{chapter_id}:writer"
    return f"step{stage}"


def locate_source_anchors(output, source, source_ref, *, full_coverage=True):
    """Resolve exact quotes to UTF-16 without guessing between repeated matches."""
    value = deepcopy(output)
    source_bytes = source.encode("utf-16-le")
    def walk(node, path=""):
        if isinstance(node, dict):
            if {"exact_quote", "start_utf16", "end_utf16"} <= node.keys():
                if node["source_ref"]["record_id"] != source_ref["record_id"] or node["source_ref"]["version"] != source_ref["version"]:
                    raise WorkflowBlocked("source_reference_mismatch")
                quote = node["exact_quote"]
                a, b = node["start_utf16"], node["end_utf16"]
                if a is not None and b is not None:
                    try:
                        if type(a) is not int or type(b) is not int or not 0 <= a < b <= len(source_bytes) // 2:
                            raise ValueError("invalid source interval")
                        excerpt = source_bytes[a*2:b*2].decode("utf-16-le")
                        if quote is not None and excerpt != quote:
                            raise ValueError("quote differs from interval")
                    except (ValueError, UnicodeError):
                        raise WorkflowBlocked("source_anchor_invalid")
                else:
                    if quote is None or not quote or (a is None) != (b is None):
                        raise WorkflowBlocked("source_anchor_invalid")
                    hits, start = [], 0
                    while quote and (position := source.find(quote, start)) >= 0:
                        before, after = source[:position], source[position+len(quote):]
                        if (node["prefix"] is None or before.endswith(node["prefix"])) and (node["suffix"] is None or after.startswith(node["suffix"])):
                            hits.append(position)
                        start = position + 1
                    if len(hits) != 1:
                        count = len(hits)
                        message = (f"原文引文命中 {count} 次；请提供精确 UTF-16 起止位置或足以消歧的 prefix/suffix"
                                   if count else "原文引文在固定原文中未命中；请核对引文与固定原文，或提供可验证的精确 UTF-16 起止位置")
                        raise WorkflowBlocked("source_anchor_ambiguous", {
                            "code": "source_anchor_ambiguous", "validation_errors": [{
                            "path": path or "/", "validator": "source_anchor_unique_match",
                            "message": message, "category": "content", "match_count": count,
                            "candidate_start_utf16": [
                                len(source[:position].encode("utf-16-le")) // 2 for position in hits[:8]],
                        }]})
                    node["start_utf16"] = len(source[:hits[0]].encode("utf-16-le")) // 2
                    node["end_utf16"] = node["start_utf16"] + len(quote.encode("utf-16-le")) // 2
            for key, item in node.items():
                walk(item, path + "/" + str(key).replace("~", "~0").replace("/", "~1"))
        elif isinstance(node, list):
            for index, item in enumerate(node):
                walk(item, path + "/" + str(index))
    walk(value)
    payload = value["payload"]
    if full_coverage:
        if payload["remaining_source_anchors"]:
            raise WorkflowBlocked("source_coverage_incomplete")
        spans = sorted((a["start_utf16"], a["end_utf16"]) for a in payload["covered_source_anchors"])
        end = 0
        for a, b in spans:
            if a > end:
                raise WorkflowBlocked("source_coverage_incomplete")
            end = max(end, b)
        if end != len(source_bytes) // 2:
            raise WorkflowBlocked("source_coverage_incomplete")
    return value
