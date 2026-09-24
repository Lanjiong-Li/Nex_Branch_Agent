"""离线合同/样例检查；不是 Harness 生产校验器，不调用模型或 Nexo API。

运行：/Users/llj/miniconda3/envs/agent/bin/python validate.py
"""
import copy
import hashlib
import json
import math
import re
from pathlib import Path

from jsonschema import Draft202012Validator
from agents.strict_schema import ensure_strict_json_schema

ROOT = Path(__file__).resolve().parent


def read(name):
    return json.loads((ROOT / name).read_text())


def resolve(root, pointer):
    value = root
    for token in pointer.removeprefix('#/').split('/'):
        value = value[token.replace('~1', '/').replace('~0', '~')]
    return value


def walk(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from walk(child)


def require(condition, message):
    if not condition:
        raise ValueError(message)


def ports(node):
    if node['kind'] == 'jump':
        return []
    if node['kind'] == 'condition':
        return [b['id'] for b in node['branches']] + ['default']
    if node['kind'] == 'story' and (node['choiceGroups'] or node['qtes']):
        return [c['id'] for g in node['choiceGroups'] for c in g['choices'] if c['action'] == 'jump'] + [
            q['id'] + (':failure' if outcome == 'failure' else '')
            for q in node['qtes'] for outcome in ['success', 'failure']
            if (outcome != 'failure' or q['failureEnabled']) and q[outcome + 'Action'] == 'jump'
        ]
    return ['next']


def graph_checks(project):
    """仅核对本样例可确定的关系。DSL语法/实际权限/语义覆盖另行验收。"""
    chapters = project['chapters']
    require(bool(chapters), '完整剧本没有章节')
    chapter_map = {c['id']: c for c in chapters}
    nodes = {n['id']: (c, n) for c in chapters for n in c['nodes']}
    scenes = {s['id']: s for s in project['scenes']}
    entities = list(chapters) + list(project['scenes']) + list(project['variables']) + list(project['chapterEdges'])
    for c in chapters:
        entities += c['nodes'] + c['edges']
        for n in c['nodes']:
            entities += n['choiceGroups'] + n['qtes'] + n['branches']
            entities += [choice for group in n['choiceGroups'] for choice in group['choices']]
    ids = [o['id'] for o in entities]
    require(len(ids) == len(set(ids)), '重复对象 ID')
    require(all(i and i not in ['begin', 'end'] for i in ids), '空 ID 或占用保留哨兵')
    keys = [v['key'] for v in project['variables']]
    require(len(keys) == len(set(keys)), '重复变量 key')
    for v in project['variables']:
        require(bool(re.fullmatch(r'[A-Za-z_][A-Za-z_0-9]*', v['key'])), '无效变量 key')
        if v['type'] == 'number':
            require(bool(re.fullmatch(r'-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?', v['value'])), '非法 number 字符串')
            require(math.isfinite(float(v['value'])), '非有限数值')
        if v['type'] == 'boolean':
            require(v['value'] in ['true', 'false'], '非法 boolean 字符串')
    expected = [{'id': f"auto:begin:{chapters[0]['id']}", 'source': 'begin', 'target': chapters[0]['id']}]
    expected += [dict(id=f"auto:{a['id']}:{b['id']}", source=a['id'], target=b['id']) for a, b in zip(chapters, chapters[1:])]
    expected.append(dict(id=f"auto:{chapters[-1]['id']}:end", source=chapters[-1]['id'], target='end'))
    require(project['chapterEdges'] == expected, '章节图不匹配当前 Studio 自动顺序链')
    adjacent = {c['id']: set() for c in chapters}
    for a, b in zip(chapters, chapters[1:]):
        adjacent[a['id']].add(b['id'])
    successors = {id: set() for id in nodes}
    for c in chapters:
        local = {n['id']: n for n in c['nodes']}
        starts = [n for n in c['nodes'] if n['chapterStart']]
        require(len(starts) == 1, '完整章节必须有且只有一个开始节点')
        route_keys = [(e['source'], e['sourcePort']) for e in c['edges']]
        require(len(route_keys) == len(set(route_keys)), '一个端口多条边')
        edges_by_port = {(e['source'], e['sourcePort']): e for e in c['edges']}
        for e in c['edges']:
            require(e['source'] in local and e['target'] in local, '边跨章或悬空')
            source, target = local[e['source']], local[e['target']]
            require(source['id'] != target['id'], '节点直接连自己')
            require(not target['chapterStart'], '开始节点存在入线')
            require(e['sourcePort'] in ports(source), '无效出口')
            require(not source['chapterEnd'] or target['kind'] == 'jump', '章结束出口只能连同章 jump')
            successors[source['id']].add(target['id'])
        for n in c['nodes']:
            require(not (n['chapterStart'] and n['chapterEnd']), '开始/结束互斥')
            require(not n['sceneId'] or n['sceneId'] in scenes, '未解析的场次')
            for interaction in n['choiceGroups'] + n['qtes']:
                require(0 <= interaction['scriptInline']['afterLine'] < len(n['body'].split('\n')), '正文互动行号越界')
            outcomes = [(choice['id'], choice['action'], choice['targetNodeId'], True)
                        for g in n['choiceGroups'] for choice in g['choices']]
            outcomes += [(q['id'] + (':failure' if outcome == 'failure' else ''), q[outcome+'Action'], q[outcome+'TargetNodeId'], outcome == 'success' or q['failureEnabled'])
                         for q in n['qtes'] for outcome in ['success', 'failure']]
            for port, action, target, enabled in outcomes:
                visible = edges_by_port.get((n['id'], port))
                if action == 'continue':
                    require(target == '' and visible is None, 'continue 有目标或可视边')
                elif enabled:
                    require(target in local and visible is not None and visible['target'] == target, '互动目标与可视连线不一致')
                else:
                    require(visible is None and (target == '' or target in local), '禁用失败仍有边或悬空草稿目标')
            if n['kind'] == 'variable':
                require(bool(n['scriptDsl'].strip()), '变量节点无 DSL')
            if n['kind'] == 'condition':
                require(all(b['conditionDsl'].strip() for b in n['branches']), '条件分支无 DSL')
            if n['kind'] == 'jump':
                target = n['jumpTarget']
                if target['kind'] == 'chapter':
                    require(target['id'] in adjacent[c['id']], 'jump 目标不是下一章')
                    targets = [t for t in chapter_map[target['id']]['nodes'] if t['chapterStart']]
                    require(len(targets) == 1, '目标章没有唯一入口')
                    successors[n['id']].add(targets[0]['id'])
                else:
                    require(target['id'] in nodes, 'jump 目标节点缺失')
                    target_owner, target_node = nodes[target['id']]
                    require(target_node['kind'] == 'story' and not target_node['chapterStart'] and target_node['id'] != n['id'], 'jump 节点目标必须为非开始的剧情节点且不能为自己')
                    target_chapter = target_owner['id']
                    require(target_chapter == c['id'] or target_chapter in adjacent[c['id']], 'jump 跨越非直接后继章')
                    successors[n['id']].add(target['id'])
            # 完整样例要求所有可用出口有明确走向；无出线的章节结束可终止。
            if not n['chapterEnd']:
                require(all((n['id'], port) in edges_by_port for port in ports(n)), '完成稿有未连接的可用出口')
    start = next(n['id'] for n in chapters[0]['nodes'] if n['chapterStart'])
    reachable, pending = set(), [start]
    while pending:
        current = pending.pop()
        if current in reachable: continue
        reachable.add(current)
        pending.extend(successors[current] - reachable)
    require(reachable == set(nodes), '样例存在不可达节点（仅拓扑可达，不证明条件可满足）')
    require(any(n['chapterEnd'] and not successors[n['id']] for n in chapters[-1]['nodes']), '末章无终止节点')


def chapter_diff_checks(payload, baseline):
    require(payload['chapter']['id'] == baseline['id'], '章节身份变化')
    for collection, removed in [('nodes', 'removed_node_ids'), ('edges', 'removed_edge_ids')]:
        before = {item['id'] for item in baseline[collection]}
        after = {item['id'] for item in payload['chapter'][collection]}
        require(set(payload[removed]) == before - after, '遗漏对象与显式删除清单不一致')
    original = {n['id']: n['kind'] for n in baseline['nodes']}
    require(all(n['id'] not in original or n['kind'] == original[n['id']] for n in payload['chapter']['nodes']), '已有节点改型')


def source_shape_checks(schema, manifest):
    source = Path(manifest['source_file']).read_text()
    require(hashlib.sha256(source.encode()).hexdigest() == manifest['source_sha256'], '源类型快照变化')
    checked = 0
    for name, targets in manifest['interface_definitions'].items():
        match = re.search(r'export interface ' + name + r' \{(.*?)\n\}', source, re.S)
        require(match is not None, '缺少源 interface: ' + name)
        fields = dict((key, not bool(optional)) for key, optional in re.findall(r'^  (\w+)(\?)?:', match.group(1), re.M))
        for target in ([targets] if isinstance(targets, str) else targets):
            item = schema if target == '$' else resolve(schema, target)
            props = set(item['properties'])
            require(props <= set(fields), '提取字段不属于源接口 ' + name)
            require({k for k, mandatory in fields.items() if mandatory} <= props, '遗漏源接口必需字段 ' + name)
            checked += 1
    return checked


def main():
    registry = read('registry.json')
    checks, refs, strict = [], 0, 0
    schemas = {}
    for entry in registry['schemas']:
        path = ROOT / entry['file']
        require(hashlib.sha256(path.read_bytes()).hexdigest() == entry['sha256'], '注册哈希错误 ' + path.name)
        schema = json.loads(path.read_text())
        Draft202012Validator.check_schema(schema)
        converted = ensure_strict_json_schema(copy.deepcopy(schema))
        require(converted == schema, '严格处理静默改变了合同 ' + path.name)
        for part in walk(schema):
            if '$ref' in part:
                require(part['$ref'].startswith('#/'), '非自包含引用')
                resolve(schema, part['$ref'])
                refs += 1
            if part.get('type') == 'object':
                require(part.get('additionalProperties') is False, '非封闭对象')
                require(set(part['required']) == set(part['properties']), 'strict 字段不全')
        strict += 1
        schemas[entry['schema_id']] = schema
        checks.append(path.name)
    require(set(registry['bindings'].values()) <= set(schemas), '无法解析的绑定')
    example_paths = list((ROOT / 'examples').glob('*.example.json'))
    for path in example_paths:
        name = path.name.removesuffix('.example.json')
        Draft202012Validator(schemas[name]).validate(json.loads(path.read_text()))
    graph = read('examples/nexo_graph.example.json')
    graph_checks(graph)
    chapter = read('examples/chapter_graph.example.json')['payload']
    chapter_diff_checks(chapter, graph['chapters'][0])
    require(chapter['chapter'] == graph['chapters'][0], '章节样例与最终样例不一致')
    require(all(s in graph['scenes'] for s in chapter['shared_scenes']), '场次组装丢失')
    require(all(v in graph['variables'] for v in chapter['shared_variables']), '变量组装丢失')
    graph_schema = schemas['nexo_graph']
    require(all(schemas['chapter_graph']['$defs'].get(key) == value for key, value in graph_schema['$defs'].items()), 'Chapter 与 Project 共享定义不一致')
    count = source_shape_checks(graph_schema, read('source-manifest.json'))
    negatives = []
    def rejects(name, mutation, structural=False):
        value = copy.deepcopy(graph)
        mutation(value)
        try:
            if structural: Draft202012Validator(graph_schema).validate(value)
            else: graph_checks(value)
        except Exception as exc:
            from jsonschema.exceptions import ValidationError
            if not isinstance(exc, (ValueError, ValidationError)): raise
            negatives.append(name)
        else:
            raise AssertionError('错误样例未被拒绝: ' + name)
    rejects('禁止时间轴字段', lambda g: g['chapters'][0]['nodes'][0].update(timeline={}), True)
    rejects('禁止 API narrative 节点名', lambda g: g['chapters'][0]['nodes'][0].update(kind='narrative'), True)
    rejects('Variable.value 必须是领域模型字符串', lambda g: g['variables'][0].update(value=0), True)
    rejects('最终根禁止结果信封', lambda g: g.update(result_kind='ready'), True)
    rejects('正文不可换成外部索引', lambda g: g['chapters'][0]['nodes'][0].update(body={'start':0,'end':10}), True)
    rejects('互动位置不能是 null', lambda g: g['chapters'][0]['nodes'][0]['choiceGroups'][0].update(scriptInline=None), True)
    rejects('重复节点 ID', lambda g: g['chapters'][0]['nodes'][1].update(id='n:arrival'))
    rejects('悬空边', lambda g: g['chapters'][0]['edges'][0].update(target='missing'))
    rejects('互动出口不一致', lambda g: g['chapters'][0]['nodes'][0]['choiceGroups'][0]['choices'][0].update(targetNodeId='n:leave'))
    rejects('正文行号越界', lambda g: g['chapters'][0]['nodes'][0]['choiceGroups'][0]['scriptInline'].update(afterLine=99))
    rejects('非法变量字符串', lambda g: g['variables'][0].update(value='很多'))
    rejects('章节边跳过顺序约束', lambda g: g['chapterEdges'][1].update(target='end'))
    rejects('continue 不能保留跳转', lambda g: g['chapters'][0]['nodes'][0]['choiceGroups'][0]['choices'][0].update(action='continue'))
    rejects('jump 不能指向功能节点', lambda g: g['chapters'][0]['nodes'][-1].update(jumpTarget={'kind':'node','id':'n:check'}))
    rejects('jump 不能指向章开始节点', lambda g: g['chapters'][0]['nodes'][-1].update(jumpTarget={'kind':'node','id':'n:arrival'}))
    rejects('jump 不能指向自己', lambda g: g['chapters'][0]['nodes'][-1].update(jumpTarget={'kind':'node','id':'n:jump'}))
    bad = copy.deepcopy(chapter)
    bad['chapter']['nodes'].pop()
    try: chapter_diff_checks(bad, graph['chapters'][0])
    except ValueError: negatives.append('单章遗漏节点不能成为隐式删除')
    else: raise AssertionError('遗漏删除检查失败')
    good = copy.deepcopy(graph)
    good['chapters'][0]['nodes'][5]['qtes'][0].update(failureEnabled=False)
    # 保留无可视边的失败草稿目标；另用完成链使恢复节点仍可达。
    good['chapters'][0]['edges'] = [e for e in good['chapters'][0]['edges'] if e['id'] != 'e:failure']
    good['chapters'][0]['nodes'][5]['qtes'][0]['successTargetNodeId'] = 'n:recover'
    next(e for e in good['chapters'][0]['edges'] if e['id'] == 'e:success')['target'] = 'n:recover'
    Draft202012Validator(graph_schema).validate(good)
    graph_checks(good)
    checks_report = {
        'status':'passed', 'scope':'离线合同/样例检查；无外部调用', 'schema_count':len(checks),
        'schemas_checked':checks, 'local_sdk_strict_checks':strict, 'local_refs_resolved':refs,
        'source_interface_profiles_checked':count, 'source_check_limit':'检查字段归属与源接口必填项，并人工核对类型；未执行 TypeScript 编译。',
        'examples_checked':[p.name for p in example_paths],
        'graph_checks':'唯一身份、引用、入口、端口、选择/QTE双写一致、文字插入位置、顺序章节、变量字符串、基础可达性、终止、完整章节删除差分',
        'positive_regression':'禁用 QTE 失败允许保留合法草稿目标，不产生可视连线',
        'negative_cases_rejected':negatives,
        'not_verified':['真实模型 API','TypeScript 编译及浏览器渲染','完整 DSL 编译/执行/条件可满足性','项目权限、用户确认与历史版本','资产目录实际存在性','Harness SDK适配器/组装器/迁移器','Nexo API导入保存','发布与播放','HTML配置页面'],
    }
    (ROOT / 'validation-report.json').write_text(json.dumps(checks_report, ensure_ascii=False, indent=2)+'\n')
    print(json.dumps({k:checks_report[k] for k in ['status','schema_count','local_sdk_strict_checks','local_refs_resolved','source_interface_profiles_checked','examples_checked','negative_cases_rejected']}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
