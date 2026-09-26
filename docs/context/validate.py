"""Check context design defaults against published output schemas, without API calls."""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent
config = json.loads((ROOT / 'stage-materials.json').read_text())
defaults = json.loads((ROOT / 'defaults.json').read_text())
catalog_path = ROOT / config['schema_catalog']
catalog = json.loads(catalog_path.read_text())
schemas = {
    entry['schema_id']: json.loads((catalog_path.parent / entry['file']).read_text())
    for entry in catalog['schemas']
}


def alternatives(node, root, seen=frozenset()):
    if '$ref' in node:
        ref = node['$ref']
        assert ref.startswith('#/'), ref
        if ref in seen:
            return []
        target = root
        for part in ref[2:].split('/'):
            target = target[part.replace('~1', '/').replace('~0', '~')]
        return alternatives(target, root, seen | {ref})
    if 'anyOf' in node or 'oneOf' in node:
        return [branch for choice in node.get('anyOf', node.get('oneOf', []))
                for branch in alternatives(choice, root, seen)]
    return [node]


def path_exists(root, pointer):
    if pointer == '':
        return True
    if not pointer.startswith('/'):
        return False
    current = [root]
    for encoded in pointer[1:].split('/'):
        part = encoded.replace('~1', '/').replace('~0', '~')
        following = []
        for node in current:
            for branch in alternatives(node, root):
                if part == '*' and branch.get('type') == 'array':
                    following.append(branch['items'])
                elif part in branch.get('properties', {}):
                    following.append(branch['properties'][part])
        current = following
        if not current:
            return False
    return True


profiles = config['profiles']
assert len({profile['stage'] for profile in profiles}) == len(profiles)
assert {f'step{i}' for i in range(1, 12)} <= {p['stage'] for p in profiles}


def validate_step1_profile(profile, all_config):
    """Step1 cannot turn its full source into indexed, optional, or batched input."""
    assert profile['include_common_materials'] is True
    sources = [m for m in profile['materials']
               if m['source'] == {'builtin': 'runtime.full_source'}]
    assert len(sources) == 1, 'Step1 requires exactly one full-source material'
    full_source = sources[0]
    assert set(full_source['selectors']) == {
        '/source_ref', '/text', '/start_utf16', '/end_utf16',
    }
    assert full_source['scope_filter'] == 'full_source'
    assert full_source['load'] == 'auto' and full_source['required'] is True
    assert full_source['detail'] == 'full'
    assert full_source['coverage'] == 'complete_scope'
    for material in all_config['common_materials'] + profile['materials']:
        assert material['source'].get('builtin') not in {
            'runtime.source_block', 'runtime.batch_state',
        }, 'Step1 must not replace or supplement its source with batch input'
        assert material['scope_filter'] not in {'current_batch', 'current_source_block'}
    projection = all_config['builtin_sources']['runtime.full_source']
    assert set(projection['fields']) == {
        'source_ref', 'text', 'start_utf16', 'end_utf16',
    }
    assert projection['range'] == {
        'start_utf16': 'zero_or_window_start', 'end_utf16': 'source_length_or_window_end',
    }
    assert projection['truncate'] is False
    assert 'current_source_block' not in all_config['scope_filters']


def validate_step1_parameters(parameters):
    assert set(parameters) == {'trigger_tokens', 'window_tokens'}
    assert type(parameters['trigger_tokens']) is int and parameters['trigger_tokens'] > 0
    assert type(parameters['window_tokens']) is int and parameters['window_tokens'] > 0


step1 = next(profile for profile in profiles if profile['stage'] == 'step1')
validate_step1_profile(step1, config)
validate_step1_parameters(defaults['context']['step1_source'])

# Stage defaults must resolve without raising budgets for unrelated stages.
assert set(defaults['stage_overrides']) <= {p['stage'] for p in profiles}
for stage in ['base', *defaults['stage_overrides']]:
    override = defaults['stage_overrides'].get(stage, {})
    context_values = {**defaults['context'], **override.get('context', {})}
    model_values = {**defaults['model'], **override.get('model', {})}
    for value in (context_values['input_token_cap'], model_values['max_output_tokens']):
        assert type(value) is int and value > 0, (stage, value)
    margin = context_values['safety_margin_tokens']
    assert type(margin) is int and margin >= 0, (stage, margin)
    if stage == 'step1':
        validate_step1_parameters(context_values['step1_source'])
        assert context_values['step1_source']['window_tokens'] < context_values['input_token_cap'], \
            'Step1 window must leave input space for instructions and other required materials'
# Model capability checks and actual assembled-input counts are runtime requirements,
# not established by this offline defaults/path validator.

business_paths = builtin_paths = 0
for group in [config['common_materials']] + [p['materials'] for p in profiles]:
    assert len({m['id'] for m in group}) == len(group)
    for material in group:
        source = material['source']
        assert len(source) == 1 and set(source) <= {'schema_id', 'builtin'}
        assert material['scope_filter'] in config['scope_filters']
        assert material['load'] in {'auto', 'on_demand'}
        assert isinstance(material['required'], bool)
        assert material['detail'] in {'full', 'index', 'ref'}
        assert material['coverage'] in {'complete_scope', 'context_only', 'evidence_only'}
        assert type(material['priority']) is int
        assert material['selectors'] and len(material['selectors']) == len(set(material['selectors']))
        for pointer in material['selectors']:
            if 'schema_id' in source:
                # Internal plain-text artifacts intentionally have no Agents
                # SDK output_type JSON Schema.  Their only valid selector is
                # the whole text value.
                if source['schema_id'] in {'work_summary', 'source_global_analysis'}:
                    assert pointer == '', (material['id'], pointer)
                else:
                    assert path_exists(schemas[source['schema_id']], pointer), (material['id'], pointer)
                business_paths += 1
            else:
                fields = config['builtin_sources'][source['builtin']]['fields']
                assert pointer.startswith('/') and pointer[1:] in fields, (material['id'], pointer)
                builtin_paths += 1

batch = defaults['context']['batching']
assert 0 < batch['target_tokens'] <= batch['hard_max_tokens']
assert batch['neighbor_tokens_each_side'] >= 0 and batch['carryover_token_cap'] >= 0
assert batch['max_items'] > 0
assert 'source_concurrency' not in batch, 'Step1 does not use batching controls'
assert 0.1 <= batch['output_headroom_ratio'] <= 0.5
assert batch['boundary_order'] and len(batch['boundary_order']) == len(set(batch['boundary_order']))
assert set(batch['boundary_order']) <= {'chapter', 'scene', 'paragraph', 'sentence'}
assert defaults['context']['version_mapping']['dependency_scope'] in {'selected_with_guards', 'item', 'artifact'}

# Catch missing fields at publication rather than silently dropping required data.
assert not path_exists(schemas['chapter_design'], '/payload/linear_body/unknown_field')
assert not path_exists(schemas['nexo_graph'], '/payload/chapters')
assert path_exists(schemas['nexo_graph'], '/chapters/*/nodes/*/body')

# Check that editable defaults cannot silently restore invalid window settings.
for invalid in [
    {'trigger_tokens': 0, 'window_tokens': 300000},
    {'trigger_tokens': True, 'window_tokens': 300000},
    {'trigger_tokens': 500000, 'window_tokens': 0},
    {'trigger_tokens': 500000, 'window_tokens': True},
]:
    try:
        validate_step1_parameters(invalid)
    except AssertionError:
        pass
    else:
        raise AssertionError(f'Invalid Step1 parameters accepted: {invalid}')

print(json.dumps({
    'profiles': len(profiles),
    'business_field_paths': business_paths,
    'builtin_field_paths': builtin_paths,
    'registered_filters': len(config['scope_filters']),
    'step1_source_window_and_parameter_checks': 'passed',
    'stage_token_defaults': 'passed',
    'defaults_and_path_checks': 'passed',
    'scope': 'offline specification validation; no runtime or model execution',
}, ensure_ascii=False, indent=2))
