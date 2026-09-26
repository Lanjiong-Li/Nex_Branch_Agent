"""Turn validated model payloads into readable conversation messages.

The structured value remains the authoritative artifact.  This module only
projects the model-authored semantic fields for the conversation UI; audit
references and raw JSON remain available through runtime data.
"""
from __future__ import annotations

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


def _render_mapping(value: dict, indent=0, omit=frozenset()) -> list[str]:
    lines = []
    pad = "  " * indent
    for key, item in value.items():
        if key in omit or _technical(key) or _empty(item):
            continue
        label = _label(key)
        if isinstance(item, (str, int, float, bool)):
            lines.append(f"{pad}{label}：{_scalar(item)}")
        elif isinstance(item, dict):
            nested = _render_mapping(item, indent + 1)
            if nested:
                lines.append(f"{pad}{label}")
                lines.extend(nested)
        elif isinstance(item, list):
            visible = [entry for entry in item if not _empty(entry)]
            if not visible:
                continue
            lines.append(f"{pad}{label}")
            for index, entry in enumerate(visible, 1):
                if isinstance(entry, dict):
                    headline_key, headline = _headline(entry)
                    lines.append(f"{pad}{index}. {headline or ''}".rstrip())
                    lines.extend(_render_mapping(entry, indent + 1, {headline_key} if headline_key else frozenset()))
                else:
                    lines.append(f"{pad}{index}. {_scalar(entry)}")
    return lines


def stage_result_text(stage: int | str, result: dict | str, *, version: int | None = None,
                      confirmation: bool = False) -> str:
    """Render exact semantic model fields without exposing the JSON envelope."""
    number = int(str(stage).removeprefix("step"))
    heading = f"Step {number} · {STAGE_TITLES.get(number, '阶段结果')}"
    if version is not None:
        heading += f"（v{version}）"
    lines = [heading]
    if isinstance(result, str) and result.strip():
        lines.extend(["", result.strip()])
        if confirmation:
            lines.extend(["", "请确认以上结果，或直接提出需要修改的内容。"])
        return "\n".join(lines)
    payload = result.get("payload") if isinstance(result, dict) else None
    if number == 1 and isinstance(payload, dict):
        remaining = payload.get("remaining_source_anchors")
        lines.append("处理范围：已通读并覆盖完整原作。" if remaining == [] else "处理范围：仍有原作范围需要补充处理。")
    if isinstance(payload, dict):
        lines.extend(["", *_render_mapping(payload)])
    notes = result.get("notes", []) if isinstance(result, dict) else []
    if notes:
        lines.extend(["", "模型说明"])
        lines.extend(f"{index}. {_scalar(note)}" for index, note in enumerate(notes, 1))
    if len(lines) <= 2:
        lines.extend(["", "该阶段产物已生成并保存。"])
    if confirmation:
        lines.extend(["", "请确认以上结果，或直接提出需要修改的内容。"])
    return "\n".join(lines)
