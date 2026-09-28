from branch_agent.presentation import artifact_to_markdown, stage_result_text


def test_stage_result_is_presented_as_readable_prose_without_json_envelope():
    result = {
        "result_kind": "ready",
        "payload": {
            "source_global_events_ref": {"record_id": "global", "version": "1", "item_id": None, "json_pointer": None},
            "source_character_events_ref": {"record_id": "character", "version": "1", "item_id": None, "json_pointer": None},
            "premise": "一个守塔人必须在真相与亲情之间做出选择。",
            "world_rules": [{"finding_id": "rule-1", "statement": "雾潮会抹去人的短期记忆。", "evidence_refs": []}],
            "themes": [{"finding_id": "theme-1", "statement": "记忆与责任", "evidence_refs": []}],
            "conflicts": [],
            "characters": [{"character_ref": {"record_id": "char", "version": "1", "item_id": "lin", "json_pointer": None},
                            "motivation": "找回失踪的妹妹。", "arc": "从逃避走向承担。", "relationships": [], "evidence_refs": []}],
            "key_event_refs": [],
            "preservation_items": [{"item_id": "keep-1", "category": "关系", "content": "兄妹关系必须保留。",
                                    "suggested_retention": "preserve", "rationale": "推动主冲突。", "evidence_refs": []}],
        },
        "questions": [],
        "evidence_refs": [],
        "notes": ["重点关注主角对妹妹的愧疚。"],
    }

    text = stage_result_text(2, result, version=3, confirmation=True)

    assert "Step 2 · 原作知识资产" in text
    assert "一个守塔人必须在真相与亲情之间做出选择。" in text
    assert "雾潮会抹去人的短期记忆。" in text
    assert "找回失踪的妹妹。" in text
    assert "兄妹关系必须保留。" in text
    assert "重点关注主角对妹妹的愧疚。" in text
    assert "请确认以上结果" in text
    assert '"result_kind"' not in text
    assert '"payload"' not in text
    assert "{" not in text
    assert text.startswith("# Step 2 · 原作知识资产（v3）")
    assert "## 世界规则" in text


def test_chapter_graph_presentation_contains_model_written_story_and_choices():
    result = {
        "result_kind": "ready",
        "payload": {
            "chapter": {
                "id": "chapter-1",
                "title": "灯塔来信",
                "summary": "主角在风暴前收到妹妹的信。",
                "nodes": [{
                    "id": "node-1", "kind": "story", "name": "值夜", "body": "雨水敲打玻璃。林默拆开那封湿透的信。",
                    "choiceGroups": [{"id": "choice-1", "prompt": "要不要立即回信？", "choices": [
                        {"id": "a", "text": "立即回信", "outcome": "暴露自己的位置"},
                        {"id": "b", "text": "烧掉信件", "outcome": "暂时隐藏行踪"},
                    ]}],
                }],
                "edges": [],
            },
            "shared_scenes": [], "shared_variables": [], "removed_node_ids": [], "removed_edge_ids": [],
        },
        "questions": [], "evidence_refs": [], "notes": [],
    }

    text = stage_result_text(10, result, version=1, confirmation=True)

    assert "灯塔来信" in text
    assert "雨水敲打玻璃。林默拆开那封湿透的信。" in text
    assert "要不要立即回信？" in text
    assert "立即回信" in text
    assert "暴露自己的位置" in text
    assert '"choiceGroups"' not in text


def test_step1_markdown_keeps_event_id_but_not_source_anchor_and_escapes_html():
    result = {"payload": {"global_events": [{"event_id": "EVT-01", "title": "第一次相遇",
        "summary": "<script>alert(1)</script>",
        "analysis": "这次相遇建立人物冲突。",
        "source_anchors": [{"start_utf16": 0, "end_utf16": 5}]}],
        "remaining_source_anchors": []}}
    document = artifact_to_markdown(result, stage=1,
        artifact_kind="source_global_events", version=2)
    assert document.startswith("# Step 1 · 作品事件视图（v2）")
    assert "EVT-01" in document
    assert "#### 事件分析" in document
    assert "这次相遇建立人物冲突。" in document
    assert "source_anchors" not in document
    assert "\\<script\\>" in document
    assert "请确认以上结果" not in document


def test_non_envelope_fixed_artifact_is_not_rendered_as_empty_markdown():
    document = artifact_to_markdown({"chapters": [{"id": "ch-1"}]},
        artifact_kind="nexo_graph", version=1)
    assert "```json" in document
    assert '"ch-1"' in document
