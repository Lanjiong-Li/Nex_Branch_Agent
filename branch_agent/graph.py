"""Deterministic chapter assembly. Domain authorization is separate from quality checks."""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import ast
import json
import re

from jsonschema import Draft202012Validator

SCHEMA_DIR = Path(__file__).resolve().parents[1] / "docs/output-schemas/v2"


class GraphError(ValueError):
    pass


class GraphValidationError(GraphError):
    """Deterministic Graph checks failed; details are safe to show in chat."""

    code = "script_validation_failed"

    def __init__(self, checks: list[dict]):
        self.checks = deepcopy(checks)
        lines = []
        for check in checks:
            if check["status"] == "pass":
                continue
            details = check.get("details") or ["未提供具体原因"]
            lines.append(f"{check['check_id']}：" + "；".join(map(str, details)))
        super().__init__("脚本校验未通过：\n- " + "\n- ".join(lines))


def schema(schema_id: str, schemas: dict | None = None) -> dict:
    if schemas is not None:
        return schemas[schema_id]
    return json.loads((SCHEMA_DIR / f"{schema_id}.schema.json").read_text())


def validate_output(schema_id: str, value: dict, schemas: dict | None = None) -> None:
    Draft202012Validator(schema(schema_id, schemas)).validate(value)


def automatic_chapter_edges(chapters: list[dict]) -> list[dict]:
    if not chapters:
        return []
    ids = ["begin", *[chapter["id"] for chapter in chapters], "end"]
    return [{"id": f"auto:{a}:{b}", "source": a, "target": b}
            for a, b in zip(ids, ids[1:])]


_UNKNOWN = object()


def _initial_variables(project: dict) -> dict:
    values = {}
    for variable in project.get("variables", []):
        raw = variable.get("value")
        try:
            if variable.get("type") == "boolean":
                values[variable["key"]] = {"true": True, "false": False}[str(raw).lower()]
            elif variable.get("type") == "number":
                number = float(raw)
                values[variable["key"]] = int(number) if number.is_integer() else number
            elif variable.get("type") == "string":
                values[variable["key"]] = str(raw)
            else:
                values[variable["key"]] = _UNKNOWN
        except (KeyError, TypeError, ValueError):
            if variable.get("key"):
                values[variable["key"]] = _UNKNOWN
    return values


def _python_expression(source: str) -> str:
    source = re.sub(r"/\*.*?\*/", " ", source, flags=re.S)
    source = re.sub(r"//[^\n]*", " ", source)
    source = source.replace("&&", " and ").replace("||", " or ")
    source = re.sub(r"(?<![=!<>])!(?!=)", " not ", source)
    source = re.sub(r"\btrue\b", "True", source, flags=re.I)
    return re.sub(r"\bfalse\b", "False", source, flags=re.I).strip()


def _eval_expression(source: str, values: dict):
    """Evaluate the Nexo expression subset; unsupported input stays unknown."""
    try:
        tree = ast.parse(_python_expression(source), mode="eval")
    except (SyntaxError, ValueError):
        return _UNKNOWN

    def visit(node):
        if isinstance(node, ast.Expression):
            return visit(node.body)
        if isinstance(node, ast.Constant) and isinstance(node.value, (str, int, float, bool)):
            return node.value
        if isinstance(node, ast.Name):
            return values.get(node.id, _UNKNOWN)
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.Not, ast.UAdd, ast.USub)):
            value = visit(node.operand)
            if value is _UNKNOWN:
                return _UNKNOWN
            try:
                return not value if isinstance(node.op, ast.Not) else +value if isinstance(node.op, ast.UAdd) else -value
            except (TypeError, ValueError):
                return _UNKNOWN
        if isinstance(node, ast.BoolOp) and isinstance(node.op, (ast.And, ast.Or)):
            values_ = [visit(value) for value in node.values]
            if isinstance(node.op, ast.And):
                if any(value is not _UNKNOWN and not bool(value) for value in values_):
                    return False
                return _UNKNOWN if any(value is _UNKNOWN for value in values_) else True
            if any(value is not _UNKNOWN and bool(value) for value in values_):
                return True
            return _UNKNOWN if any(value is _UNKNOWN for value in values_) else False
        if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Sub, ast.Mult, ast.Div)):
            left, right = visit(node.left), visit(node.right)
            if _UNKNOWN in (left, right):
                return _UNKNOWN
            try:
                return {ast.Add: lambda: left + right, ast.Sub: lambda: left - right,
                        ast.Mult: lambda: left * right, ast.Div: lambda: left / right}[type(node.op)]()
            except (TypeError, ValueError, ZeroDivisionError):
                return _UNKNOWN
        if isinstance(node, ast.Compare):
            left = visit(node.left)
            result = True
            for operator, comparator in zip(node.ops, node.comparators):
                right = visit(comparator)
                if _UNKNOWN in (left, right):
                    return _UNKNOWN
                try:
                    current = {
                        ast.Eq: lambda: left == right, ast.NotEq: lambda: left != right,
                        ast.Gt: lambda: left > right, ast.GtE: lambda: left >= right,
                        ast.Lt: lambda: left < right, ast.LtE: lambda: left <= right,
                    }.get(type(operator))
                    if current is None or not current():
                        return False
                except (TypeError, ValueError):
                    return _UNKNOWN
                left = right
            return result
        return _UNKNOWN

    try:
        return visit(tree)
    except (RecursionError, TypeError, ValueError):
        return _UNKNOWN


def _apply_script(source: str, values: dict) -> dict:
    next_values = dict(values)
    source = re.sub(r"/\*.*?\*/", " ", source, flags=re.S)
    source = re.sub(r"//[^\n]*", " ", source)
    statements = [part.strip() for part in re.split(r"[;\n]+", source) if part.strip()]
    for statement in statements:
        match = re.fullmatch(r"([A-Za-z_][A-Za-z_0-9]*)\s*(=|\+=|-=)\s*(.+)", statement, flags=re.S)
        if not match:
            # Unsupported code can change any known value; widen conservatively.
            return {key: _UNKNOWN for key in next_values}
        key, operator, expression = match.groups()
        value = _eval_expression(expression, next_values)
        if operator == "=":
            next_values[key] = value
        else:
            current = next_values.get(key, _UNKNOWN)
            if _UNKNOWN in (current, value):
                next_values[key] = _UNKNOWN
            else:
                try:
                    next_values[key] = current + value if operator == "+=" else current - value
                except (TypeError, ValueError):
                    next_values[key] = _UNKNOWN
    return next_values


def _state_key(values: dict):
    def stable(value):
        return ("unknown",) if value is _UNKNOWN else (type(value).__name__, repr(value))
    return tuple((key, stable(value)) for key, value in sorted(values.items()))


def _reachable_nodes(chapter: dict, initial: dict) -> tuple[set[str], list[str], dict[str, list[str]]]:
    nodes = {node.get("id"): node for node in chapter.get("nodes", []) if node.get("id")}
    starts = [node_id for node_id, node in nodes.items() if node.get("chapterStart")]
    outgoing = {node_id: [] for node_id in nodes}
    incoming = {node_id: [] for node_id in nodes}
    structural = []
    for edge in chapter.get("edges", []):
        source, target = edge.get("source"), edge.get("target")
        if source not in nodes or target not in nodes:
            missing = []
            if source not in nodes:
                missing.append(f"source={source!r}")
            if target not in nodes:
                missing.append(f"target={target!r}")
            structural.append(f"{chapter.get('id')}/{edge.get('id')}：连线端点不存在（{', '.join(missing)}）")
            continue
        outgoing[source].append(edge)
        incoming[target].append(edge)
    if not starts and nodes:
        structural.append(f"{chapter.get('id')}：没有 chapterStart 节点")

    queue = [(node_id, dict(initial)) for node_id in starts]
    seen_states = {node_id: set() for node_id in nodes}
    reachable = set()
    blocked_conditions: dict[str, list[str]] = {}
    processed = 0
    while queue and processed < 8192:
        node_id, values = queue.pop(0)
        key = _state_key(values)
        if key in seen_states[node_id]:
            continue
        if len(seen_states[node_id]) >= 64:
            values = {name: _UNKNOWN for name in values}
            key = _state_key(values)
            if key in seen_states[node_id]:
                continue
        seen_states[node_id].add(key)
        reachable.add(node_id)
        processed += 1
        node = nodes[node_id]
        if node.get("kind") == "variable":
            values = _apply_script(node.get("scriptDsl", ""), values)

        edges = outgoing[node_id]
        if node.get("kind") != "condition":
            queue.extend((edge["target"], dict(values)) for edge in edges)
            continue
        by_port = {}
        for edge in edges:
            by_port.setdefault(edge.get("sourcePort"), []).append(edge)
        can_fall_through = True
        for branch in node.get("branches", []):
            if not can_fall_through:
                break
            result = _eval_expression(branch.get("conditionDsl", ""), values)
            if result is not False:
                queue.extend((edge["target"], dict(values)) for edge in by_port.get(branch.get("id"), []))
            else:
                for edge in by_port.get(branch.get("id"), []):
                    blocked_conditions.setdefault(edge["target"], []).append(branch.get("conditionDsl", ""))
            if result is True:
                can_fall_through = False
        if can_fall_through:
            queue.extend((edge["target"], dict(values)) for edge in by_port.get("default", []))
        else:
            for edge in by_port.get("default", []):
                blocked_conditions.setdefault(edge["target"], []).append("default 分支永远不会进入")

    explanations = {}
    for node_id in nodes.keys() - reachable:
        if not incoming[node_id]:
            explanations[node_id] = ["没有任何入边"]
        elif blocked_conditions.get(node_id):
            conditions = sorted({value for value in blocked_conditions[node_id] if value})
            explanations[node_id] = ["所有可达条件路径均无法满足" + ("：" + "；".join(conditions) if conditions else "")]
        else:
            explanations[node_id] = ["从 chapterStart 没有可执行路径可以到达"]
    return reachable, structural, explanations


def quality_checks(project: dict, schemas: dict | None = None) -> list[dict]:
    """The two enabled deterministic checks; no model judgment is involved."""
    errors = sorted(Draft202012Validator(schema("nexo_graph", schemas)).iter_errors(project),
                    key=lambda e: str(list(e.path)))
    schema_details = []
    for error in errors:
        pointer = "/" + "/".join(str(part).replace("~", "~0").replace("/", "~1") for part in error.path)
        schema_details.append(f"{pointer or '/'}：{error.message}")
    unreachable = []
    initial = _initial_variables(project)
    for chapter in project.get("chapters", []):
        _, structural, explanations = _reachable_nodes(chapter, initial)
        unreachable.extend(structural)
        for node_id, reasons in sorted(explanations.items()):
            unreachable.append(f"{chapter.get('id')}/{node_id}：{'；'.join(reasons)}")
    return [
        {"check_id": "schema_contract", "check_kind": "schema_contract",
         "status": "fail" if errors else "pass", "details": schema_details},
        {"check_id": "unreachable_nodes", "check_kind": "chapter_topology",
         "status": "fail" if unreachable else "pass", "details": unreachable},
    ]


def _project_fields(value: dict, definition: dict, defs: dict) -> dict:
    if "$ref" in definition:
        definition = defs[definition["$ref"].split("/")[-1]]
    if "anyOf" in definition or "oneOf" in definition:
        options = definition.get("anyOf", definition.get("oneOf", []))
        for option in options:
            resolved = defs.get(option.get("$ref", "").split("/")[-1], option)
            kind_spec = resolved.get("properties", {}).get("kind", {})
            kind = kind_spec.get("const", (kind_spec.get("enum") or [None])[0])
            if kind is not None and isinstance(value, dict) and value.get("kind") == kind:
                return _project_fields(value, resolved, defs)
        for option in options:
            if option.get("type") == "object" and isinstance(value, dict):
                return _project_fields(value, option, defs)
        return deepcopy(value)
    if definition.get("type") == "object" and isinstance(value, dict):
        return {k: _project_fields(v, definition["properties"][k], defs)
                for k, v in value.items() if k in definition.get("properties", {})}
    if definition.get("type") == "array" and isinstance(value, list):
        return [_project_fields(v, definition["items"], defs) for v in value]
    return deepcopy(value)


def project_profile(original: dict, schemas: dict | None = None) -> tuple[dict, dict]:
    """Return a narrow creative projection and an untouched sidecar snapshot."""
    contract = schema("nexo_graph", schemas)
    return _project_fields(original, contract, contract["$defs"]), deepcopy(original)


def restore_sidecar(profile: dict, original: dict) -> dict:
    """Merge by stable ID; omitted objects are not resurrected, omitted fields survive."""
    def merge(new, old):
        if isinstance(new, dict):
            result = deepcopy(old) if isinstance(old, dict) else {}
            for key, value in new.items():
                result[key] = merge(value, result.get(key))
            return result
        if isinstance(new, list):
            previous = {item["id"]: item for item in (old or [])
                        if isinstance(item, dict) and "id" in item}
            return [merge(item, previous.get(item.get("id")))
                    if isinstance(item, dict) and "id" in item else deepcopy(item) for item in new]
        return deepcopy(new)
    return merge(profile, original)


def _unique(items, name):
    ids = [x["id"] for x in items]
    if len(ids) != len(set(ids)):
        raise GraphError(f"duplicate {name} IDs")


def merge_chapter(project: dict, output: dict, *, chapter_id: str,
                  chapter_order: list[str], entity_catalog: list[dict],
                  allowed_removed_ids: set[str] | None = None,
                  allowed_shared_changes: set[str] | None = None,
                  schemas: dict | None = None) -> dict:
    validate_output("chapter_graph", output, schemas)
    if output["result_kind"] != "ready" or output["payload"] is None:
        raise GraphError("chapter is not ready")
    payload = output["payload"]
    chapter = deepcopy(payload["chapter"])
    if chapter["id"] != chapter_id or chapter_id not in chapter_order:
        raise GraphError("chapter identity/order differs from frozen task")
    result = deepcopy(project)
    previous = next((c for c in result["chapters"] if c["id"] == chapter_id), None)
    _unique(chapter["nodes"], "node")
    _unique(chapter["edges"], "edge")
    declared = _declared_ids(chapter)
    if len(declared) != len(set(declared)):
        raise GraphError("duplicate chapter or nested object identity")
    foreign = set().union(*(_nested_ids(c) for c in result["chapters"] if c["id"] != chapter_id),
                          _nested_ids(result["scenes"]), _nested_ids(result["variables"]))
    if set(declared) & foreign:
        raise GraphError("identity belongs to another chapter or shared object")
    allowed_removed_ids = allowed_removed_ids or set()
    for collection, removed in (("nodes", "removed_node_ids"), ("edges", "removed_edge_ids")):
        old_ids = {x["id"] for x in previous[collection]} if previous else set()
        new_ids = {x["id"] for x in chapter[collection]}
        removal = set(payload[removed])
        if old_ids - new_ids != removal or len(removal) != len(payload[removed]):
            raise GraphError(f"{removed} must exactly describe removed existing objects")
        if not removal <= allowed_removed_ids:
            raise GraphError("deletion requires explicit scoped authorization")
    if previous:
        old_nodes = {n["id"]: n for n in previous["nodes"]}
        for node in chapter["nodes"]:
            old = old_nodes.get(node["id"])
            if old is None:
                continue
            if old["kind"] != node["kind"]:
                raise GraphError("existing node kind cannot change")
            for field in ("x", "y", "collapsed"):
                node[field] = old[field]
            old_children = _nested_ids(old)
            if not (old_children - _nested_ids(node)) <= allowed_removed_ids:
                raise GraphError("nested interaction deletion is not authorized")
    for index, node in enumerate(chapter["nodes"]):
        if not previous or not any(n["id"] == node["id"] for n in previous["nodes"]):
            node.update(x=float(index * 320), y=0.0, collapsed=False)
    catalog = {e["entity_id"]: e for e in entity_catalog}
    for scene in payload["shared_scenes"]:
        for field, kind in (("characterIds", "character"), ("locationIds", "location")):
            if any(i not in catalog or catalog[i]["kind"] != kind for i in scene[field]):
                raise GraphError("scene refers to an unregistered character/location")
        scene = deepcopy(scene)
        scene["characters"] = "、".join(catalog[i]["name"] for i in scene["characterIds"])
        scene["location"] = "、".join(catalog[i]["name"] for i in scene["locationIds"])
        _merge_shared(result["scenes"], scene, allowed_shared_changes or set())
    for variable in payload["shared_variables"]:
        if any(v["key"] == variable["key"] and v["id"] != variable["id"] for v in result["variables"]):
            raise GraphError("variable key is already assigned to another stable ID")
        _merge_shared(result["variables"], variable, allowed_shared_changes or set())
    chapters = {c["id"]: c for c in result["chapters"]}
    chapters[chapter_id] = chapter
    result["chapters"] = [chapters[i] for i in chapter_order if i in chapters]
    result["chapterEdges"] = automatic_chapter_edges(result["chapters"])
    return result


def _declared_ids(value):
    ids = []
    if isinstance(value, dict):
        if "id" in value: ids.append(value["id"])
        for key, child in value.items():
            if key != "jumpTarget": ids.extend(_declared_ids(child))
    elif isinstance(value, list):
        for child in value: ids.extend(_declared_ids(child))
    return ids


def _nested_ids(value):
    return set(_declared_ids(value))


def _merge_shared(existing, proposed, allowed_changes):
    current = next((item for item in existing if item["id"] == proposed["id"]), None)
    if current is None:
        existing.append(deepcopy(proposed))
    elif current != proposed:
        if current["id"] not in allowed_changes:
            raise GraphError("shared entity change requires scoped authorization")
        current.clear()
        current.update(deepcopy(proposed))


def assemble_project(*, project_id: str, name: str, description: str, prompt: str,
                     updated_at: str, chapter_order: list[str], chapters: list[dict],
                     entity_catalog: list[dict], baseline: dict | None = None,
                     allowed_removed_ids: set[str] | None = None,
                     allowed_shared_changes: set[str] | None = None,
                     schemas: dict | None = None) -> tuple[dict, dict]:
    if baseline:
        project, sidecar = project_profile(baseline, schemas)
    else:
        project = {"id": project_id, "name": name, "description": description,
                   "prompt": prompt, "revision": 0, "updatedAt": updated_at,
                   "chapters": [], "chapterEdges": [], "variables": [], "scenes": []}
        sidecar = {}
    if project["id"] != project_id:
        raise GraphError("project identity is program controlled")
    for output in chapters:
        project = merge_chapter(project, output, chapter_id=output["payload"]["chapter"]["id"],
                                chapter_order=chapter_order, entity_catalog=entity_catalog,
                                allowed_removed_ids=allowed_removed_ids,
                                allowed_shared_changes=allowed_shared_changes, schemas=schemas)
    if [c["id"] for c in project["chapters"]] != chapter_order:
        raise GraphError("complete frozen chapter roster has not been generated")
    project.update(name=name, description=description, updatedAt=updated_at)
    project["revision"] += 1
    checks = quality_checks(project, schemas)
    if any(c["status"] != "pass" for c in checks):
        raise GraphValidationError(checks)
    return project, sidecar
