import copy
import json
from pathlib import Path

import pytest

from branch_agent.graph import (GraphError, assemble_project, automatic_chapter_edges,
                                project_profile, restore_sidecar, quality_checks)

EXAMPLES = Path(__file__).resolve().parents[1] / "docs/output-schemas/v2/examples"


def chapter():
    return json.loads((EXAMPLES / "chapter_graph.example.json").read_text())


def assemble(output):
    scene_ids = output["payload"]["shared_scenes"]
    catalog = [{"entity_id": i, "kind": kind, "name": i}
               for scene in scene_ids for field, kind in (("characterIds", "character"), ("locationIds", "location"))
               for i in scene[field]]
    return assemble_project(project_id="project", name="故事", description="", prompt="",
                            updated_at="2026-09-21T00:00:00Z", chapter_order=[output["payload"]["chapter"]["id"]],
                            chapters=[output], entity_catalog=catalog)


def test_assembly_and_only_two_quality_checks():
    result, sidecar = assemble(chapter())
    assert sidecar == {}
    assert result["id"] == "project"
    assert [c["check_id"] for c in quality_checks(result)] == ["schema_contract", "unreachable_nodes"]
    assert all(c["status"] == "pass" for c in quality_checks(result))
    assert result["chapterEdges"] == automatic_chapter_edges(result["chapters"])


def test_missing_endpoint_is_a_reachability_failure():
    result, _ = assemble(chapter())
    result["chapters"][0]["edges"][0]["target"] = "does-not-exist"
    assert quality_checks(result)[1]["status"] == "fail"
    assert "连线端点不存在" in quality_checks(result)[1]["details"][0]


def test_unlinked_node_is_reported():
    result, _ = assemble(chapter())
    result["chapters"][0]["nodes"].append({**copy.deepcopy(result["chapters"][0]["nodes"][1]),
        "id": "n:island", "title": "孤岛"})
    check = quality_checks(result)[1]
    assert check["status"] == "fail"
    assert any("n:island" in detail and "没有任何入边" in detail for detail in check["details"])


def test_condition_that_can_never_be_true_makes_target_unreachable():
    result, _ = assemble(chapter())
    variable = next(node for node in result["chapters"][0]["nodes"] if node["kind"] == "variable")
    variable["scriptDsl"] = "trust = 0;"
    check = quality_checks(result)[1]
    assert check["status"] == "fail"
    assert any("n:qte" in detail and "trust >= 1" in detail for detail in check["details"])


def test_unsupported_condition_is_conservatively_treated_as_reachable():
    result, _ = assemble(chapter())
    condition = next(node for node in result["chapters"][0]["nodes"] if node["kind"] == "condition")
    condition["branches"][0]["conditionDsl"] = "custom(trust)"
    assert quality_checks(result)[1]["status"] == "pass"


def test_foreign_entities_are_not_invented():
    output = chapter()
    output["payload"]["shared_scenes"][0]["characterIds"] = ["invented"]
    with pytest.raises(GraphError, match="unregistered"):
        assemble_project(project_id="p", name="s", description="", prompt="", updated_at="now",
                         chapter_order=[output["payload"]["chapter"]["id"]], chapters=[output], entity_catalog=[])


def test_sidecar_preserves_subtitles_and_extra_project_fields():
    original, _ = assemble(chapter())
    original["chapterCanvas"] = {"zoom": 2}
    original["chapters"][0]["nodes"][0]["subtitles"] = [{"text": "keep"}]
    profile, sidecar = project_profile(original)
    assert "chapterCanvas" not in profile
    assert "subtitles" not in profile["chapters"][0]["nodes"][0]
    restored = restore_sidecar(profile, sidecar)
    assert restored == original


def test_implicit_deletion_never_accepted():
    from branch_agent.graph import merge_chapter
    output = chapter()
    project, _ = assemble(output)
    old_id = output["payload"]["chapter"]["nodes"].pop()["id"]
    with pytest.raises(GraphError, match="exactly"):
        merge_chapter(project, output, chapter_id=project["chapters"][0]["id"],
                      chapter_order=[project["chapters"][0]["id"]], entity_catalog=[])
    output["payload"]["removed_node_ids"] = [old_id]
    with pytest.raises(GraphError, match="authorization"):
        merge_chapter(project, output, chapter_id=project["chapters"][0]["id"],
                      chapter_order=[project["chapters"][0]["id"]], entity_catalog=[])


def test_custom_frozen_schema_is_used_by_assembly_and_preserves_new_field():
    from branch_agent.graph import schema
    schemas = {key: schema(key) for key in ("nexo_graph", "chapter_graph")}
    for value in schemas.values():
        for name, definition in value["$defs"].items():
            if name == "StoryNode":
                definition["properties"]["custom_note"] = {"type": "string"}
                definition["required"].append("custom_note")
    output = chapter()
    for node in output["payload"]["chapter"]["nodes"]:
        if node["kind"] == "story": node["custom_note"] = "configured field"
    # Shared entity names are derived by the same fixed-ID catalog as normal assembly.
    catalog = [{"entity_id": i, "kind": kind, "name": i}
               for scene in output["payload"]["shared_scenes"]
               for field, kind in (("characterIds", "character"), ("locationIds", "location")) for i in scene[field]]
    result, _ = assemble_project(project_id="project", name="故事", description="", prompt="", updated_at="2026-09-21T00:00:00Z",
        chapter_order=[output["payload"]["chapter"]["id"]], chapters=[output], entity_catalog=catalog, schemas=schemas)
    assert any(n.get("custom_note") == "configured field" for n in result["chapters"][0]["nodes"])
    assert quality_checks(result, schemas)[0]["status"] == "pass"
    assert quality_checks(result)[0]["status"] == "fail"
    assert project_profile(result, schemas)[0] == result
