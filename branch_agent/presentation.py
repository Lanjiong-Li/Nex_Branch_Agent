"""Project fixed structured artifacts into readable Markdown.

The structured value remains authoritative. The same projection is used by
the conversation UI and the downloadable .md file; confirmation controls are
separate from the document.
"""
from __future__ import annotations

import json
import re


STAGE_TITLES = {
    1: "原作切分",
    2: "原作知识资产",
    3: "玩家与改编策略",
    4: "互动改编方案",
    5: "游戏事件设计",
    6: "补充事件叙事功能",
    7: "结局路线",
    8: "玩家画像",
    9: "章节设计",
    10: "章节互动剧本",
    11: "质量审核",
}

ARTIFACT_TITLES = {
    "source_text": "原作全文",
    "source_global_events": "作品事件视图",
    "source_character_events": "主要人物事件视图",
}

LABELS = {
    "title": "标题", "name": "名称", "summary": "概要", "description": "说明",
    "premise": "故事前提", "premise_and_scope": "故事前提与改编范围", "logline": "一句话故事",
    "world_rules": "世界规则", "themes": "主题", "conflicts": "核心冲突", "characters": "人物分析",
    "event_causality": "事件因果", "canon_constraints": "原作约束", "uncertainties": "待核实内容",
    "knowledge_type": "知识类型",
    "motivation": "动机", "arc": "人物弧光", "relationships": "人物关系",
    "preservation_items": "原作保留建议", "category": "类别", "content": "内容",
    "suggested_retention": "建议处理", "rationale": "理由", "statement": "结论",
    "player_identity": "玩家身份", "player_role": "玩家角色", "strategy_basis": "策略依据",
    "experience_goals": "体验目标", "user_ideas": "用户想法", "adaptation_principles": "改编原则",
    "interaction_principles": "互动原则", "constraints": "约束", "narrative_constraints": "叙事约束",
    "world_and_character_changes": "世界与人物调整", "entity_specs": "人物与地点设定",
    "writing_style": "写作风格", "events": "事件", "event_links": "事件关系",
    "global_events": "全局事件", "character_views": "主要人物视角", "aliases": "别名",
    "analysis": "事件分析",
    "event_id": "事件 ID", "character_event_id": "人物事件 ID",
    "involvement": "参与方式", "narrative_order": "叙事顺序", "story_time": "故事时间",
    "event_annotations": "事件功能与玩家意图", "narrative_function": "叙事功能",
    "updates": "事件叙事功能补充", "game_event_id": "事件 ID",
    "player_intent": "玩家意图", "endings": "结局", "routes": "路线",
    "game_event_ids": "相关游戏事件 ID",
    "state_requirements": "状态需求", "profiles": "玩家画像", "design_implications": "设计影响",
    "assumptions": "待验证假设", "linear_body": "完整线性正文", "segments": "正文",
    "text": "正文", "interactions": "互动设计", "prompt": "提示语", "options": "选项",
    "choices": "选项", "outcome": "结果", "flow_links": "剧情流转", "entry_exit_contracts": "入口与出口",
    "graph_compatibility": "Graph 兼容性", "chapter": "章节", "nodes": "剧本节点",
    "kind": "类型", "body": "正文", "choiceGroups": "选择组", "qtes": "QTE",
    "success": "成功结果", "failure": "失败结果", "shared_scenes": "共享场景",
    "shared_variables": "共享变量", "value": "初始值", "value_type": "变量类型",
    "conditionDsl": "条件逻辑", "scriptDsl": "变量逻辑", "proposed_verdict": "审核结论",
    "checked_scope": "已审核范围", "unchecked_scope": "未审核范围", "findings": "审核发现",
    "severity": "严重程度", "suggested_fix": "修改建议", "suggested_owner": "建议处理阶段",
    "metrics": "审核指标", "interpretation": "说明", "graph_checks": "程序检查",
    "status": "状态", "recommendations": "建议", "limitations": "限制", "task": "任务",
    "progress": "当前进展", "decisions": "已确认决定", "unfinished_items": "待办事项",
    "recent_user_changes": "用户近期修改", "notes": "模型说明",
}

ENUMS = {
    "preserve": "保留", "adapt": "改编", "optional": "可选", "exclude": "排除",
    "pass": "通过", "fail": "未通过", "warning": "需注意", "blocker": "阻塞",
    "major": "严重", "minor": "一般", "story": "剧情", "choice": "选择",
    "qte": "QTE", "game": "小游戏", "condition": "条件", "variable": "变量", "jump": "跳转",
    "true": "是", "false": "否",
    "fact": "原作事实", "inference": "推断", "adaptation_suggestion": "改编建议",
}

SKIP_KEYS = {
    "result_kind", "payload", "questions", "evidence_refs", "source_anchors",
    "covered_source_anchors", "remaining_source_anchors", "stage_artifact_refs",
    "removed_node_ids", "removed_edge_ids", "edges", "chapterEdges",
    "x", "y", "collapsed", "revision", "updatedAt", "order",
}


def _technical(key: str) -> bool:
    if key in {"event_id", "character_event_id", "game_event_id"}:
        return False
    return (key in SKIP_KEYS or key == "id" or key.endswith("_id") or key.endswith("_ids")
            or key.endswith("_ref") or key.endswith("_refs") or key in {"characterIds", "locationIds", "sceneId"})


def _label(key: str) -> str:
    if key in LABELS:
        return LABELS[key]
    words = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", key).replace("_", " ")
    return words[:1].upper() + words[1:]


def _scalar(value) -> str:
    if isinstance(value, bool):
        return "是" if value else "否"
    text = str(value)
    return ENUMS.get(text, text)


def _empty(value) -> bool:
    return value is None or value == "" or value == [] or value == {}


def _headline(value: dict):
    for key in ("title", "name", "prompt", "statement", "summary", "text", "content", "task"):
        if isinstance(value.get(key), (str, int, float)) and not _empty(value[key]):
            return key, _scalar(value[key])
    return None, None


def _markdown_scalar(value) -> str:
    """Keep model text literal even when it contains Markdown or HTML syntax."""
    text = _scalar(value).replace("\\", "\\\\")
    text = re.sub(r"([*`_\[\]{}()!#|<>])", r"\\\1", text)
    text = re.sub(r"(?m)^(\s*)([-+])(?=\s)", r"\1\\\2", text)
    return re.sub(r"(?m)^(\s*)(\d+)\.(?=\s)", r"\1\2\\.", text)


def _section(lines: list[str], label: str, depth: int):
    if lines and lines[-1] != "":
        lines.append("")
    lines.extend([f"{'#' * min(depth, 6)} {_markdown_scalar(label)}", ""])


def _render_mapping(value: dict, lines: list[str], depth=2, omit=frozenset()):
    for key, item in value.items():
        if key in omit or _technical(key) or _empty(item):
            continue
        label = _label(key)
        if isinstance(item, (str, int, float, bool)):
            _section(lines, label, depth)
            lines.append(_markdown_scalar(item))
        elif isinstance(item, dict):
            _section(lines, label, depth)
            _render_mapping(item, lines, depth + 1)
        elif isinstance(item, list):
            visible = [entry for entry in item if not _empty(entry)]
            if not visible:
                continue
            _section(lines, label, depth)
            for index, entry in enumerate(visible, 1):
                if isinstance(entry, dict):
                    headline_key, headline = _headline(entry)
                    _section(lines, f"{index}. {headline or label}", depth + 1)
                    _render_mapping(entry, lines, depth + 2,
                                    {headline_key} if headline_key else frozenset())
                else:
                    lines.append(f"{index}. {_markdown_scalar(entry)}")


def artifact_to_markdown(content: dict | str, *, stage: int | str | None = None,
                         artifact_kind: str | None = None, version: int | None = None) -> str:
    """Render one fixed artifact version. This document is independent of chat."""
    number = int(str(stage).removeprefix("step")) if stage is not None else None
    title = ARTIFACT_TITLES.get(artifact_kind) or STAGE_TITLES.get(number, "阶段产物")
    heading = f"Step {number} · {title}" if number is not None else title
    if version is not None:
        heading += f"（v{version}）"
    lines = [f"# {heading}"]
    if isinstance(content, str):
        if content.strip():
            lines.extend(["", content.strip()])
        return "\n".join(lines).rstrip() + "\n"
    payload = content.get("payload") if isinstance(content, dict) else None
    if isinstance(content, dict) and not isinstance(payload, dict):
        # Generic fixed artifacts such as the final graph have no model
        # envelope. Preserve every field instead of producing an empty file.
        lines.extend(["", "```json", json.dumps(content, ensure_ascii=False, indent=2),
                      "```"])
        return "\n".join(lines).rstrip() + "\n"
    if number == 1 and isinstance(payload, dict):
        remaining = payload.get("remaining_source_anchors")
        lines.extend(["", "> 处理范围：已通读并覆盖完整原作。" if remaining == []
                      else "> 处理范围：仍有原作范围需要补充处理。"])
    if isinstance(payload, dict):
        _render_mapping(payload, lines)
    notes = content.get("notes", []) if isinstance(content, dict) else []
    if notes:
        _section(lines, "模型说明", 2)
        for index, note in enumerate(notes, 1):
            lines.append(f"{index}. {_markdown_scalar(note)}")
    if len(lines) == 1:
        lines.extend(["", "该阶段产物已生成并保存。"])
    return "\n".join(lines).rstrip() + "\n"


def stage_result_text(stage: int | str, result: dict | str, *, version: int | None = None,
                      confirmation: bool = False, artifact_kind: str | None = None) -> str:
    """Compatibility entry point for stage presentation and confirmation cards."""
    document = artifact_to_markdown(result, stage=stage, artifact_kind=artifact_kind,
                                    version=version).rstrip()
    return document + ("\n\n请确认以上结果，或直接提出需要修改的内容。" if confirmation else "")
