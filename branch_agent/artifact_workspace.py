"""Human-facing current artifacts and account defaults for new projects.

An account default is only a creation template. Once copied, it is an ordinary
effective ArtifactVersion in the project's normal version chain.
"""
from __future__ import annotations

from copy import deepcopy
import json

from psycopg.types.json import Jsonb

from .configuration import ARTIFACT_PRODUCERS
from .records import ROOT
from .schemas import SchemaCatalog
from .storage import VersionConflict
from .workflow import Workflow, all_records, body


EDITABLE_KINDS = ("adaptation_strategy", "adaptation_plan")
ARTIFACT_INFO = {
    "source_text": ("原作全文", "导入的原文，供 Step1 读取。"),
    "source_global_events": ("全局事件视图", "原作事件的全局时间线与关键事件。"),
    "source_character_events": ("主要人物事件视图", "主要人物各自经历的事件；逐事件不保存原文位置。"),
    "source_global_analysis": ("全局事件分析", "全局事件的因果关系和改编分析。"),
    "source_knowledge_asset": ("原作知识资产", "综合全局事件、全局分析和人物事件形成的结构化原作知识。"),
    "adaptation_strategy": ("玩家与改编策略", "玩家身份与互动改编策略；可在运行前人工设置。"),
    "adaptation_plan": ("互动剧本改编方案", "完整的互动改编方案；可在运行前人工设置。"),
    "game_event_view": ("游戏事件视图", "游戏事件及其叙事功能。"),
    "ending_routes": ("结局路线", "结局、路线与达成条件。"),
    "player_profiles": ("目标玩家画像", "目标玩家画像及体验需求。"),
    "chapter_design": ("章节设计", "单章的内容与互动设计。"),
    "chapter_graph": ("章节 Graph", "单章的互动结构数据。"),
    "nexo_graph": ("完整互动剧本", "组装并校验后的完整互动剧本。"),
}


def _initial_strategy():
    legacy = json.loads((ROOT / "docs/context/defaults.json").read_text(encoding="utf-8"))
    principle = legacy["adaptation"]["default_strategy"]
    return {
        "result_kind": "ready",
        "payload": {
            "source_knowledge_asset_ref": None,
            "player_identity": {"character_ref": None, "role_description": "", "perspective": "", "decision_refs": []},
            "strategy_basis": {"default_strategy_ref": None, "mode": "default", "adjustments": []},
            "experience_goals": [], "user_ideas": [],
            "adaptation_principles": [principle], "interaction_principles": [], "constraints": [],
        },
        "questions": [], "evidence_refs": [], "notes": [],
    }


def _starter_plan():
    return {
        "result_kind": "ready",
        "payload": {
            "title": "", "logline": "", "premise_and_scope": "",
            "source_knowledge_asset_ref": None,
            "strategy_ref": None,
            "player_role": {"character_ref": None, "description": "", "perspective": ""},
            "experience_goals": [], "world_and_character_changes": [],
            "entity_specs": [], "narrative_constraints": [],
            "writing_style": {"tone": "", "dialogue": "", "narration": "", "content_boundaries": []},
            "stage_artifact_refs": {
                "game_events": None, "event_functions": None,
                "ending_routes": None, "player_profiles": None,
            },
        },
        "questions": [], "evidence_refs": [], "notes": [],
    }


class ArtifactWorkspace:
    def __init__(self, store, config_service):
        self.store = store
        self.config_service = config_service
        self.workflow = Workflow(store)
        self.catalog = SchemaCatalog()

    def _validate_default(self, kind, content, account_id=None):
        if content is None:
            return
        if not isinstance(content, dict) or content.get("result_kind") != "ready":
            raise ValueError("默认产物必须是 result_kind=ready 的完整 JSON 对象")
        # The same configured output contract will be used by later Agent runs.
        schemas = (self.config_service.values_for_account(account_id, f"step{ARTIFACT_PRODUCERS[kind]}")[0]
                   .get("schemas") if account_id else None)
        self.catalog.validate(kind, content, schemas)

        def visit(value):
            if isinstance(value, dict):
                if "record_id" in value and "version" in value:
                    raise ValueError("全局默认值不能包含某个项目的固定记录引用")
                for child in value.values():
                    visit(child)
            elif isinstance(value, list):
                for child in value:
                    visit(child)
        visit(content)

    def defaults(self, account_id):
        rows = self.store._connection().execute(
            "SELECT artifact_kind,content FROM account_artifact_defaults WHERE account_id=%s", (account_id,)
        ).fetchall()
        result = {"adaptation_strategy": _initial_strategy(), "adaptation_plan": None}
        result.update({row["artifact_kind"]: row["content"] for row in rows})
        return deepcopy(result)

    def defaults_view(self, account_id):
        return {**self.defaults(account_id), "starters": {
            "adaptation_strategy": _initial_strategy(),
            "adaptation_plan": _starter_plan(),
        }}

    def save_defaults(self, account_id, changes):
        if not changes or set(changes) - set(EDITABLE_KINDS):
            raise ValueError("只能设置 adaptation_strategy 或 adaptation_plan 的默认值")
        for kind, content in changes.items():
            self._validate_default(kind, content, account_id)
        with self.store.transaction():
            self.store.advisory_lock("account:artifact-defaults:" + account_id)
            for kind, content in changes.items():
                self.store._connection().execute("""
                    INSERT INTO account_artifact_defaults(account_id,artifact_kind,content)
                    VALUES(%s,%s,%s)
                    ON CONFLICT(account_id,artifact_kind) DO UPDATE SET
                        content=excluded.content,
                        revision=account_artifact_defaults.revision+1,
                        updated_at=clock_timestamp()
                """, (account_id, kind, Jsonb(content)))
        return self.defaults_view(account_id)

    def seed_project(self, project_id, account_id):
        # Copy both kinds from one account-consistent snapshot in the caller's
        # project creation transaction.
        self.store.advisory_lock("account:artifact-defaults:" + account_id)
        for kind, content in self.defaults(account_id).items():
            if content is None:
                continue
            self._validate_default(kind, content, account_id)
            config = self.config_service.resolve(project_id, f"step{ARTIFACT_PRODUCERS[kind]}")
            self.workflow.save(project_id, kind, content, stage=ARTIFACT_PRODUCERS[kind],
                               origin="program", effective=True, config=config)

    def _agent(self, kind, values):
        stage = ARTIFACT_PRODUCERS.get(kind)
        if kind == "source_text":
            return None
        if kind in ("source_global_events", "source_character_events"):
            view = "global" if kind == "source_global_events" else "character"
            return values["prompts"]["step1_view_agents"].get(view)
        return values["prompts"]["stage_agents"].get(f"step{stage}") if stage is not None else None

    def item(self, project_id, kind, values=None, artifact=None, include_content=True):
        values = values or self.config_service.values(project_id, "coordinator")[0]
        if artifact is None:
            artifacts = all_records(self.store, project_id, "artifact", artifact_kind=kind)
            artifacts = [a for a in artifacts if not a["scope"]["chapter_ids"]]
            artifact = max(artifacts, key=lambda a: a["created_at"]) if artifacts else None
        version = None
        if artifact and artifact["latest_version"]:
            version = next(v for v in self.workflow.versions(project_id, artifact["id"])
                           if v["version"] == artifact["latest_version"])
        producer_stage = None
        agent_key = self._agent(kind, values)
        if version and version.get("producer_run_id"):
            run = self.store.get(version["producer_run_id"], project_id=project_id)
            if run:
                agent_key = run["agent_key"]
                task = self.store.get(run["task_id"], project_id=project_id)
                if task:
                    producer_stage = task["scope"].get("stage")
        elif version and version["origin"] == "program":
            agent_key = None
            config = (self.store.get(version["config_version_id"], project_id=project_id)
                      if version.get("config_version_id") else None)
            scope_key = config.get("scope_key") if config else None
            if isinstance(scope_key, str) and scope_key.startswith("step") and scope_key[4:].isdigit():
                producer_stage = int(scope_key[4:])
        elif version and version["origin"] in ("user", "import"):
            agent_key = None
        label, description = ARTIFACT_INFO.get(kind, (kind, "项目中保存的中间产物。"))
        if not version:
            display_stage = ARTIFACT_PRODUCERS.get(kind)
        elif version["origin"] == "user":
            display_stage = ARTIFACT_PRODUCERS.get(kind)
        elif version["origin"] in ("program", "import"):
            display_stage = producer_stage
        else:
            display_stage = producer_stage if producer_stage is not None else ARTIFACT_PRODUCERS.get(kind)
        producer_name = ("Harness 写入" if version and version["origin"] == "program" else
                         "人工编辑" if version and version["origin"] == "user" else
                         "导入" if version and version["origin"] == "import" else
                         values["prompts"].get("agent_names", {}).get(agent_key) if agent_key else None)
        return {
            "kind": kind, "label": label,
            "stage": display_stage,
            "expected_stage": ARTIFACT_PRODUCERS.get(kind),
            "producer_stage": producer_stage,
            "agent_key": agent_key,
            "agent_name": producer_name,
            "description": description, "editable": kind in EDITABLE_KINDS,
            "artifact_id": artifact["id"] if artifact else None,
            "chapter_id": artifact["scope"]["chapter_ids"][0] if artifact and artifact["scope"]["chapter_ids"] else None,
            "scope": deepcopy(artifact["scope"]) if artifact else None,
            "version": version["version"] if version else None,
            "effective_version": artifact["current_effective_version"] if artifact else None,
            "effective": bool(version and artifact["current_effective_version"] == version["version"]),
            "has_content": bool(version),
            "content": deepcopy(body(self.store, version)) if version and include_content else None,
            "origin": version["origin"] if version else None,
            "updated_at": version["created_at"] if version else None,
        }

    def items(self, project_id, selected_kinds=()):
        values = self.config_service.values(project_id, "coordinator")[0]
        selected = set(selected_kinds)
        if selected - set(ARTIFACT_INFO):
            raise ValueError("包含未注册的产物类型")
        items = []
        for kind in ARTIFACT_INFO:
            artifacts = all_records(self.store, project_id, "artifact", artifact_kind=kind)
            if artifacts:
                items.extend(self.item(project_id, kind, values, artifact, include_content=kind in selected)
                             for artifact in artifacts)
            else:
                items.append(self.item(project_id, kind, values, include_content=False))
        return {"artifacts": items}

    def save_project(self, project_id, kind, content, expected_version):
        if kind not in EDITABLE_KINDS:
            raise ValueError("此产物只可查看，暂不支持人工编辑")
        if type(expected_version) is not int and expected_version is not None:
            raise ValueError("expected_version 必须是当前版本号或 null")
        if not isinstance(content, dict) or content.get("result_kind") != "ready":
            raise ValueError("产物必须是 result_kind=ready 的完整 JSON 对象")
        with self.store.transaction():
            self.store.advisory_lock(project_id + ":artifact-edit:" + kind)
            current = self.item(project_id, kind)
            if current["version"] != expected_version:
                raise VersionConflict("产物已更新，请刷新后重试")
            current_config = self.config_service.resolve(project_id, f"step{ARTIFACT_PRODUCERS[kind]}")
            self.workflow.save(project_id, kind, content, stage=ARTIFACT_PRODUCERS[kind],
                               origin="user", effective=True, config=current_config,
                               artifact_id=current["artifact_id"])
            return self.item(project_id, kind)
