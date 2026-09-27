"""Format repairs must preserve business content and be tied to exact candidates."""
import json

import pytest

from branch_agent.output_repair import (apply_format_repair, assess, candidate_hash,
                                        response_text)
from branch_agent.schemas import OutputValidationError, validation_problems


SCHEMA = {'type': 'object', 'required': ['title', 'body'],
          'additionalProperties': False,
          'properties': {'title': {'type': 'string'}, 'body': {'type': 'string'}}}


class Catalog:
    def __init__(self, schema=SCHEMA):
        self.schema = schema

    def validate(self, schema_id, value, schemas=None):
        problems = validation_problems(self.schema, value)
        if problems:
            raise OutputValidationError(problems)
        return value


def assessment(value):
    return assess(json.dumps(value, ensure_ascii=False), 'example', Catalog())


def patch(value, path):
    return json.dumps({'candidate_sha256': candidate_hash(value),
                       'patch': [{'op': 'remove', 'path': path}]})


def test_empty_or_duplicated_extra_property_can_be_removed_without_rewriting_body():
    for extra in ('', '完整对白保留'):
        value = {'title': '甲乙相遇', 'body': '完整对白保留', 'description': extra}
        found = assessment(value)
        assert found['category'] == 'format'
        assert found['problems'][0]['unexpected_properties'] == ['description']
        fixed = apply_format_repair(json.dumps(value, ensure_ascii=False), found,
                                    patch(value, '/description'))
        assert fixed == {'title': '甲乙相遇', 'body': '完整对白保留'}
        Catalog().validate('example', fixed)


def test_nonempty_unique_extra_property_must_not_be_silently_deleted():
    value = {'title': '甲乙相遇', 'body': '正文', 'description': '唯一的故事规则'}
    found = assessment(value)
    with pytest.raises(ValueError, match='唯一业务内容'):
        apply_format_repair(json.dumps(value, ensure_ascii=False), found,
                            patch(value, '/description'))


def test_nonempty_unique_root_text_can_move_verbatim_into_existing_notes():
    schema = {**SCHEMA, 'required': ['title', 'body', 'notes'],
              'properties': {**SCHEMA['properties'], 'notes': {
                  'type': 'array', 'items': {'type': 'string'}}}}
    value = {'title': '甲乙相遇', 'body': '正文', 'notes': [],
             'description': '唯一的故事规则：😀不能回头。'}
    raw = json.dumps(value, ensure_ascii=False)
    found = assess(raw, 'example', Catalog(schema))
    reply = json.dumps({'candidate_sha256': candidate_hash(value),
                        'patch': [{'op': 'move', 'from': '/description',
                                   'path': '/notes/-'}]})
    fixed = apply_format_repair(raw, found, reply)
    assert fixed['notes'] == ['唯一的故事规则：😀不能回头。']
    assert 'description' not in fixed
    Catalog(schema).validate('example', fixed)
    different = {**value, 'other_story_field': value['description']}
    different.pop('description')
    other_found = assess(json.dumps(different, ensure_ascii=False), 'example', Catalog(schema))
    with pytest.raises(ValueError, match='根字符串'):
        apply_format_repair(json.dumps(different, ensure_ascii=False), other_found,
            json.dumps({'candidate_sha256': candidate_hash(different),
                        'patch': [{'op': 'move', 'from': '/other_story_field',
                                   'path': '/notes/-'}]}))


def test_hash_and_pointer_boundaries_reject_wrong_candidate_or_content_change():
    value = {'title': '甲乙相遇', 'body': '正文', 'description': ''}
    found = assessment(value)
    with pytest.raises(ValueError, match='SHA-256'):
        apply_format_repair(json.dumps(value, ensure_ascii=False), found,
            json.dumps({'candidate_sha256': '0' * 64,
                        'patch': [{'op': 'remove', 'path': '/description'}]}))
    with pytest.raises(ValueError, match='只允许删除'):
        apply_format_repair(json.dumps(value, ensure_ascii=False), found,
            json.dumps({'candidate_sha256': candidate_hash(value),
                        'patch': [{'op': 'replace', 'path': '/body', 'value': '改写'}]}))


def test_missing_required_field_is_content_and_never_goes_to_format_agent():
    found = assessment({'title': '甲乙相遇'})
    assert found['category'] == 'content'
    assert found['problems'][0]['missing_properties'] == ['body']


def test_unique_fenced_json_is_deterministically_extracted():
    raw = '说明如下：\n```json\n{"title":"相遇","body":"原文😀"}\n```'
    found = assess(raw, 'example', Catalog())
    assert found['category'] == 'valid' and found['origin'] == 'unique_fence'
    assert found['candidate']['body'] == '原文😀'


def test_malformed_json_syntax_repair_only_accepts_mechanical_trailing_comma_fix():
    raw = '{"title":"甲乙相遇","body":"原文😀",}'
    found = assess(raw, 'example', Catalog())
    assert found['category'] == 'format' and found['candidate'] is None
    fixed = apply_format_repair(raw, found, '{"title":"甲乙相遇","body":"原文😀"}')
    assert fixed['body'] == '原文😀'
    with pytest.raises(ValueError, match='结构或内容'):
        apply_format_repair(raw, found, '{"title":"甲乙相遇","body":"改写"}')


def test_malformed_json_repair_rejects_structural_reassignment_with_same_lexemes():
    raw = '{"a":1,"b":2,"c":3,}'
    found = assess(raw, 'example', Catalog())
    with pytest.raises(ValueError, match='结构或内容'):
        apply_format_repair(raw, found, '{"a":[1,{"b":2}],"c":3}')
    with pytest.raises(ValueError, match='只有尾逗号'):
        apply_format_repair('{"a":1 "b":2}', found, '{"a":1,"b":2}')


def test_only_completed_final_messages_can_be_classified():
    response = {'terminal_status': 'completed', 'output': [
        {'type': 'message', 'content': [{'type': 'output_text', 'text': '{"x":1}'}]}]}
    assert response_text(response) == '{"x":1}'
    assert response_text({**response, 'terminal_status': 'incomplete'}) is None
    assert response_text({**response, 'output': response['output'] +
                          [{'type': 'function_call', 'name': 'read_record'}]}) is None
