"""Conservative, archive-backed classification and format-only correction."""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import re

from .schemas import OutputValidationError


def candidate_hash(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
        separators=(',', ':')).encode()).hexdigest()


def response_text(archived):
    """Only a completed final message can be treated as a repair candidate."""
    if not isinstance(archived, dict) or archived.get('terminal_status') != 'completed':
        return None
    output = archived.get('output') or []
    if any(item.get('type') == 'function_call' for item in output):
        return None
    texts = [part.get('text', '') for item in output if item.get('type') == 'message'
             for part in item.get('content', []) if part.get('type') == 'output_text']
    return ''.join(texts) if texts else None


def unique_json_text(raw):
    """Extract exactly one fenced object/array, leaving other prose unused."""
    fences = re.findall(r'```(?:json)?\s*\n([\s\S]*?)\n```', raw, flags=re.I)
    if len(fences) != 1:
        return None
    try:
        value = json.loads(fences[0])
    except (TypeError, ValueError):
        return None
    return fences[0] if isinstance(value, (dict, list)) else None


def assess(raw, schema_id, catalog, schemas=None):
    """Classify with parsers and validators, never with another model's guess."""
    try:
        candidate = json.loads(raw)
        origin = 'direct'
    except (TypeError, ValueError) as error:
        extracted = unique_json_text(raw) if isinstance(raw, str) else None
        if extracted is None:
            return {'category': 'format', 'candidate': None, 'origin': 'malformed',
                'problems': [{'path': '', 'validator': 'json_parse', 'message': str(error),
                              'category': 'format'}]}
        candidate = json.loads(extracted)
        origin = 'unique_fence'
    try:
        catalog.validate(schema_id, candidate, schemas)
        return {'category': 'valid', 'candidate': candidate, 'origin': origin, 'problems': []}
    except OutputValidationError as error:
        problems = error.problems
    except (ValueError, TypeError, KeyError) as error:
        problems = [{'path': '', 'validator': 'stage_contract', 'message': str(error),
                     'category': 'content'}]
    category = 'format' if problems and all(p['category'] == 'format' for p in problems) else 'content'
    return {'category': category, 'candidate': candidate, 'origin': origin, 'problems': problems}


def _without_trailing_commas(raw):
    """Remove only commas directly before a closing object/array delimiter.

    The scan tracks JSON strings so a comma inside story text is untouched.
    Other malformed syntax cannot be proved safe and remains with the producer.
    """
    output = []
    in_string = escaped = False
    for position, char in enumerate(raw):
        if in_string:
            output.append(char)
            if escaped:
                escaped = False
            elif char == '\\':
                escaped = True
            elif char == '"':
                in_string = False
        elif char == '"':
            in_string = True
            output.append(char)
        elif char == ',':
            following = position + 1
            while following < len(raw) and raw[following].isspace():
                following += 1
            if following >= len(raw) or raw[following] not in '}]':
                output.append(char)
        else:
            output.append(char)
    return ''.join(output)


def _strict_json(raw):
    def no_duplicates(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError('候选包含重复 JSON 字段')
            value[key] = item
        return value

    def no_nonfinite(value):
        raise ValueError(f'候选包含非 JSON 数值：{value}')

    return json.loads(raw, object_pairs_hook=no_duplicates, parse_constant=no_nonfinite)


def _pointer_parts(pointer):
    if not isinstance(pointer, str) or not pointer.startswith('/'):
        raise ValueError('补丁必须使用绝对 JSON Pointer')
    return [part.replace('~1', '/').replace('~0', '~') for part in pointer[1:].split('/')]


def _retained_elsewhere(root, removed):
    if type(root) is type(removed) and root == removed:
        return True
    if isinstance(root, dict):
        return any(_retained_elsewhere(value, removed) for value in root.values())
    if isinstance(root, list):
        return any(_retained_elsewhere(value, removed) for value in root)
    return False


def _empty(value):
    return value is None or (isinstance(value, str) and not value.strip()) or value == [] or value == {}


def apply_format_repair(raw, assessment, reply):
    """Accept only hash-bound, value-preserving fixes for extra fields.

    Malformed syntax has no trustworthy patch base. An entire JSON reply is
    accepted only when it equals the mechanically verified trailing-comma fix.
    """
    if assessment['category'] != 'format':
        raise ValueError('非格式问题不得交给格式修复 Agent')
    base = assessment['candidate']
    if base is None:
        if not isinstance(raw, str) or not raw.lstrip().startswith('{') or not raw.rstrip().endswith('}'):
            raise ValueError('无安全 JSON 基底；需交回原阶段 Agent')
        mechanical = _without_trailing_commas(raw)
        if mechanical == raw:
            raise ValueError('只有尾逗号可机械验证；需交回原阶段 Agent')
        expected = _strict_json(mechanical)
        fixed = _strict_json(reply)
        compact = lambda value: json.dumps(value, ensure_ascii=False, sort_keys=True,
                                           separators=(',', ':'), allow_nan=False)
        if compact(fixed) != compact(expected):
            raise ValueError('修复改变 JSON 结构或内容，无法证明安全')
        return fixed
    response = json.loads(reply)
    if not isinstance(response, dict) or response.get('candidate_sha256') != candidate_hash(base):
        raise ValueError('格式补丁未绑定原候选 SHA-256')
    patch = response.get('patch')
    if not isinstance(patch, list) or not patch or len(patch) > 20:
        raise ValueError('格式补丁数量无效')
    allowed = set()
    for problem in assessment['problems']:
        if problem.get('validator') != 'additionalProperties':
            raise ValueError('补丁范围超出格式错误')
        for name in problem.get('unexpected_properties', []):
            escaped = name.replace('~', '~0').replace('/', '~1')
            allowed.add(problem['path'].rstrip('/') + '/' + escaped)
    fixed = deepcopy(base)
    seen = set()
    for operation in patch:
        if not isinstance(operation, dict) or operation.get('op') not in ('remove', 'move'):
            raise ValueError('只允许删除或原样移动校验器明确指出的多余字段')
        move = operation['op'] == 'move'
        if set(operation) != ({'op', 'from', 'path'} if move else {'op', 'path'}):
            raise ValueError('格式补丁字段不符合受控协议')
        path = operation['from'] if move else operation['path']
        if path not in allowed or path in seen:
            raise ValueError('补丁路径未被校验器授权')
        seen.add(path)
        parts = _pointer_parts(path)
        node = fixed
        for part in parts[:-1]:
            node = node[int(part)] if isinstance(node, list) else node[part]
        if not isinstance(node, dict) or parts[-1] not in node:
            raise ValueError('补丁未定位原候选字段')
        removed = node.pop(parts[-1])
        if move:
            if (len(parts) != 1 or parts[0] != 'description'
                    or operation['path'] != '/notes/-'
                    or not isinstance(removed, str) or not isinstance(fixed, dict)
                    or not isinstance(fixed.get('notes'), list)
                    or any(not isinstance(item, str) for item in fixed['notes'])):
                raise ValueError('只允许原样移动根字符串到现有 notes 字符串数组')
            fixed['notes'].append(removed)
        elif not _empty(removed) and not _retained_elsewhere(fixed, removed):
            raise ValueError('多余字段含有唯一业务内容，不能无损删除')
    return fixed
