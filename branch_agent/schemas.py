"""Versioned JSON schemas used directly by Agents SDK structured outputs."""
from __future__ import annotations
from copy import deepcopy
import hashlib
import json
from functools import lru_cache
from pathlib import Path
from agents import AgentOutputSchemaBase, ModelBehaviorError
from agents.exceptions import UserError
from agents.strict_schema import ensure_strict_json_schema
from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parents[1]


class OutputValidationError(ValueError):
    """Machine-readable schema/domain findings kept out of public error text."""

    def __init__(self, problems):
        self.problems = problems
        super().__init__('; '.join(f"{item['path']}: {item['message']}" for item in problems[:12]))


def _pointer(parts):
    return '/' + '/'.join(str(part).replace('~', '~0').replace('/', '~1') for part in parts)


def validation_problems(schema, payload):
    findings = []
    for error in sorted(Draft202012Validator(schema).iter_errors(payload), key=lambda e: str(list(e.path))):
        path = _pointer(error.path)
        finding = {'path': path, 'validator': error.validator,
                   'message': error.message[:500],
                   'message_truncated': len(error.message) > 500,
                   'category': 'content'}
        if error.validator == 'additionalProperties' and isinstance(error.instance, dict):
            extras = sorted(set(error.instance) - set(error.schema.get('properties', {})))
            finding['unexpected_properties'] = extras
            finding['category'] = 'format' if extras else 'content'
        if error.validator == 'required' and isinstance(error.instance, dict):
            finding['missing_properties'] = sorted(set(error.validator_value) - set(error.instance))
        findings.append(finding)
    return findings

@lru_cache(maxsize=256)
def _check_schema_text(encoded):
    # Cache only identical immutable schema text; edits get a different key and
    # are always validated again before a configuration can be published.
    Draft202012Validator.check_schema(json.loads(encoded))


def check_schema_definition(schema):
    _check_schema_text(json.dumps(schema,ensure_ascii=False,sort_keys=True,separators=(',',':')))

def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def alternatives(node, root, seen=frozenset()):
    if '$ref' in node:
        ref = node['$ref']
        if not ref.startswith('#/'):
            raise ValueError('只支持当前 Schema 内的引用')
        if ref in seen:
            return []
        value = root
        for part in ref[2:].split('/'):
            value = value[part.replace('~1', '/').replace('~0', '~')]
        return alternatives(value, root, seen | {ref})
    if 'anyOf' in node or 'oneOf' in node:
        return [b for v in node.get('anyOf', node.get('oneOf', [])) for b in alternatives(v, root, seen)]
    return [node]


def schema_path(root, path):
    current = [root]
    if path == '':
        return current
    if not path.startswith('/'):
        return []
    for part in path[1:].split('/'):
        part = part.replace('~1', '/').replace('~0', '~')
        following = []
        for node in current:
            for branch in alternatives(node, root):
                if part == '*' and branch.get('type') == 'array':
                    following.append(branch['items'])
                elif part in branch.get('properties', {}):
                    following.append(branch['properties'][part])
        current = following
    return current


class SchemaCatalog:
    def __init__(self, path=None):
        self.path = Path(path) if path else ROOT / 'docs/output-schemas/v2'
        self.registry = json.loads((self.path / 'registry.json').read_text())
        self.schemas = {x['schema_id']: json.loads((self.path / x['file']).read_text()) for x in self.registry['schemas']}
        self.entries = {x['schema_id']: x for x in self.registry['schemas']}
        for schema in self.schemas.values():
            check_schema_definition(schema)

    def schema_for(self, stage, config=None):
        bindings = (config or {}).get('output', {}).get('bindings', self.registry['bindings'])
        key = 'conversation_coordinator' if stage == 'coordinator' else stage
        return bindings[key]

    def binding(self, schema_id, schemas=None):
        schema = (schemas or self.schemas)[schema_id]
        original = self.schemas[schema_id]
        version = self.entries[schema_id]['version']
        return {'schema_id': schema_id, 'version': version if schema == original else 'custom-' + digest(schema)[:12],
                'sha256': self.entries[schema_id]['sha256'] if schema == original else digest(schema)}

    def validate(self, schema_id, payload, schemas=None):
        schema = (schemas or self.schemas)[schema_id]
        problems = validation_problems(schema, payload)
        if problems:
            raise OutputValidationError(problems)
        if schema_id != 'nexo_graph':
            if payload['result_kind'] == 'ready' and (payload['payload'] is None or payload['questions']):
                raise OutputValidationError([{'path': '/payload', 'validator': 'stage_ready',
                    'message': 'ready 必须包含完整 payload 且 questions 为空', 'category': 'content'}])
            if payload['result_kind'] == 'needs_input' and not payload['questions']:
                raise OutputValidationError([{'path': '/questions', 'validator': 'stage_needs_input',
                    'message': 'needs_input 必须包含明确问题', 'category': 'content'}])
            if schema_id == 'source_global_step1_result' \
                    and payload['result_kind'] == 'ready' and not payload['analysis'].strip():
                raise OutputValidationError([{'path': '/analysis', 'validator': 'stage_analysis',
                    'message': 'Step1 全局事件视图与分析必须同时完整返回', 'category': 'content'}])
        return payload

    def output_type(self, schema_id, schemas=None, *, strict=True):
        return JSONOutput(self, schema_id, schemas, strict=strict)

    def validate_publication(self, schemas, profiles=None):
        if set(schemas) != set(self.schemas):
            raise ValueError(f'必须保留全部{len(self.schemas)}种输出类型')
        # The Step1 model contract contains an additional analysis field, but
        # its remaining envelope is saved as a separate event-view artifact.
        # Keep their structures aligned when either editable Schema changes.
        def runtime_shape(value):
            if isinstance(value,dict):
                return {key:runtime_shape(child) for key,child in value.items()
                        if key not in ('description','title','examples')}
            if isinstance(value,list):
                return [runtime_shape(child) for child in value]
            return value
        for view,result in (('source_global_events','source_global_step1_result'),
                            ('source_character_events','source_character_step1_result')):
            combined=deepcopy(schemas[result])
            combined['properties'].pop('analysis',None)
            combined['required']=[field for field in combined['required'] if field!='analysis']
            if runtime_shape(combined)!=runtime_shape(schemas[view]):
                raise ValueError(f'{result} 的事件视图结构必须与 {view} 一致')
        for name, schema in schemas.items():
            check_schema_definition(schema)
            self._check_strict(schema)
            try:
                ensure_strict_json_schema(deepcopy(schema))
            except (UserError,TypeError,ValueError) as error:
                raise ValueError(f'{name}: Agents SDK strict Schema 不兼容：{error}') from error
            # Existing consumed fields cannot change meaning without a registered adapter.
            self._compatible(self.schemas[name], schema, name)
            if name=='nexo_graph' and set(schema.get('properties',{}))!=set(self.schemas[name]['properties']):
                raise ValueError('nexo_graph根字段由程序组装；新增根字段需要配置其来源和组装适配器')
            if name=='nexo_graph':
                def constraints(value):
                    if isinstance(value,dict):return {k:constraints(v) for k,v in value.items() if k not in ('description','title','examples')}
                    if isinstance(value,list):return [constraints(v) for v in value]
                    return value
                old=self.schemas[name]
                protected=[('ChapterEdge',old['$defs']['ChapterEdge'],schema['$defs']['ChapterEdge'])]
                protected.extend((key,old['properties'][key],schema['properties'][key]) for key in ('id','revision','updatedAt'))
                for key,definition in old['$defs'].items():
                    for field in ('x','y','collapsed'):
                        if field in definition.get('properties',{}):
                            protected.append((key+'.'+field,definition['properties'][field],schema['$defs'][key]['properties'][field]))
                for field,a,b in protected:
                    if constraints(a)!=constraints(b):raise ValueError(f'{field}由程序生成；调整其结构或约束需要生成适配器')
        if any(schemas['chapter_graph']['$defs'].get(key) != value for key, value in schemas['nexo_graph']['$defs'].items()):
            raise ValueError('chapter_graph 与 nexo_graph 共享领域定义必须同步')
        if profiles:
            groups = [profiles.get('common_materials', [])] + [p['materials'] for p in profiles.get('profiles', [])]
            for group in groups:
                for item in group:
                    name = item['source'].get('schema_id')
                    if name:
                        for path in item['selectors']:
                            if name in ('work_summary','source_global_analysis') and path=='':
                                continue  # Internal plain-text artifacts.
                            if not schema_path(schemas[name], path):
                                raise ValueError(f'消费者字段不存在: {name}{path}')

    def _check_strict(self, node):
        if not isinstance(node, dict):
            return
        if '$ref' in node and not node['$ref'].startswith('#/'):
            raise ValueError('不支持外部引用')
        unsupported=set(node)&{'allOf','oneOf','not','if','then','else','dependentRequired','dependentSchemas','patternProperties','unevaluatedProperties'}
        if unsupported:raise ValueError('当前严格输出适配未支持这些Schema关键字：'+', '.join(sorted(unsupported)))
        if node.get('type') == 'object':
            if node.get('additionalProperties') is not False or set(node.get('required', [])) != set(node.get('properties', {})):
                raise ValueError('严格输出对象需要 additionalProperties=false 且所有字段必填；可空请使用null联合类型')
        for key, child in node.items():
            if key in ('properties', '$defs'):
                for value in child.values(): self._check_strict(value)
            elif key in ('items',): self._check_strict(child)
            elif key in ('anyOf', 'oneOf', 'allOf'):
                for value in child: self._check_strict(value)

    def _compatible(self, old, new, path):
        # Descriptions, examples and constraints can change; consumed types/identities cannot.
        for key in ('type', '$ref', 'const'):
            if old.get(key) != new.get(key):
                raise ValueError(f'{path}: 字段类型/引用变化需要新的消费者适配器')
        if 'enum' in old and ('enum' not in new or not set(new['enum'])<=set(old['enum'])):
            raise ValueError(f'{path}: 新增枚举语义需要消费者适配器')
        for key in ('properties', '$defs'):
            for name, child in old.get(key, {}).items():
                if name not in new.get(key, {}):
                    raise ValueError(f'{path}/{name}: 删除字段需要消费者迁移')
                self._compatible(child, new[key][name], path + '/' + name)
        if 'items' in old: self._compatible(old['items'], new.get('items', {}), path + '/*')
        for key in ('anyOf', 'oneOf'):
            if key in old:
                if len(old[key]) != len(new.get(key, [])):
                    raise ValueError(f'{path}: 联合类型变更需适配')
                for a, b in zip(old[key], new[key]): self._compatible(a, b, path)


class JSONOutput(AgentOutputSchemaBase):
    def __init__(self, catalog, schema_id, schemas=None, *, strict=True):
        self.catalog, self.schema_id, self.schemas, self.strict = catalog, schema_id, schemas, strict
    def is_plain_text(self): return False
    def name(self): return self.schema_id
    def is_strict_json_schema(self): return self.strict
    def json_schema(self):
        schema = deepcopy((self.schemas or self.catalog.schemas)[self.schema_id])
        schema.pop('$schema', None)
        return schema
    def validate_json(self, json_str):
        try:
            return self.catalog.validate(self.schema_id, json.loads(json_str), self.schemas)
        except (ValueError, TypeError) as exc:
            raise ModelBehaviorError(str(exc)) from exc
