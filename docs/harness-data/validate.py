"""Validate stored-record contracts and synthetic fixtures; no API/model calls."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker

ROOT = Path(__file__).resolve().parent
schema = json.loads((ROOT / 'records.schema.json').read_text())
catalog = json.loads((ROOT / 'record-catalog.json').read_text())
records = json.loads((ROOT / 'examples/records.example.json').read_text())
runtime_root = ROOT.parent / 'runtime'
runtime_schema = json.loads((runtime_root / 'checkpoint-state.schema.json').read_text())
runtime_example = json.loads((runtime_root / 'examples/checkpoint-state.example.json').read_text())
Draft202012Validator.check_schema(schema)
Draft202012Validator.check_schema(runtime_schema)
validator = Draft202012Validator(schema, format_checker=FormatChecker())
runtime_validator = Draft202012Validator(runtime_schema, format_checker=FormatChecker())
runtime_validator.validate(runtime_example)
for record in records:
    validator.validate(record)
    if record['record_type'] == 'checkpoint' and record['cursor']['handler_version'] == 'runtime.v1':
        state = record['cursor']['state']
        runtime_validator.validate(state)
        allowance = state['turn_allowance']
        assert len(set(allowance['reserved_operation_ids'])) == len(allowance['reserved_operation_ids'])
        assert len(allowance['reserved_operation_ids']) <= allowance['used'] <= allowance['limit']

expected = {entry['record_type'] for entry in catalog['records']}
assert expected == {r['record_type'] for r in records}
assert {r['$ref'].split('/')[-1] for r in schema['oneOf']} == {
    entry['definition'] for entry in catalog['records']
}
assert len({r['id'] for r in records}) == len(records)
by_id = {r['id']: r for r in records}
versions = {}
for record in records:
    if record['record_type'] == 'artifact_version':
        versions[(record['artifact_id'], str(record['version']))] = record
    if record['record_type'] == 'decision':
        versions[(record['decision_id'], str(record['version']))] = record

def check_refs(value):
    if isinstance(value, dict):
        if set(value) == {'record_id', 'version', 'item_id', 'json_pointer'}:
            if value['version'] is None:
                assert value['record_id'] in by_id, value
            else:
                assert (value['record_id'], value['version']) in versions, value
        for child in value.values():
            check_refs(child)
    elif isinstance(value, list):
        for child in value:
            check_refs(child)

for record in records:
    check_refs(record)
    assert record['project_id'] in by_id
    if record['record_type'] == 'tool_call':
        origin = by_id[record['model_call_id']]
        assert origin['record_type'] == 'model_call'
        assert all(record[k] == origin[k] for k in ('project_id', 'task_id'))
        executor = by_id[record['run_id']]
        assert all(record[k] == executor[k] for k in ('project_id', 'task_id'))
        # This fixture covers same-Run tools. Cross-Run recovery additionally
        # requires the production continuation/lease/request checks in the spec.
        assert record['run_id'] == origin['run_id'], 'Add an explicit recovery-chain fixture before allowing this case'
    if record['record_type'] == 'model_call':
        assert by_id[record['run_id']]['execution_kind'] == 'runner'
    if record['record_type'] == 'artifact_state':
        version = by_id[record['artifact_version_id']]
        assert all(record[k] == version[k] for k in ('project_id', 'artifact_id', 'version'))
    if record['record_type'] == 'confirmation':
        assert all(by_id[x]['role'] == 'user' for x in record['source_message_ids'])

negative = []
def mutate(kind, mutation):
    candidate = deepcopy(next(r for r in records if r['record_type'] == kind))
    mutation(candidate)
    assert not validator.is_valid(candidate), kind
    negative.append(kind)

mutate('history_record', lambda r: r.update(undeclared_field=True))
mutate('history_record', lambda r: r.update(id='invalid-uuid'))
mutate('confirmation', lambda r: r['subject'].update(version=None))
mutate('confirmation', lambda r: r.update(basis='unchanged_scope'))
mutate('session_item', lambda r: r.update(history_ids=[], summary_ref=None))
mutate('tool_call', lambda r: r.pop('model_call_id'))
mutate('task', lambda r: r['budget']['max_cost'].update(amount=2.0))
mutate('run', lambda r: r.update(execution_kind='runner', max_turns=0))
mutate('run', lambda r: r.update(execution_kind='recovery', max_turns=1, model_turns_used=0))
mutate('run', lambda r: r.update(execution_kind='recovery', max_turns=0, model_turns_used=1))

recovery_run = deepcopy(next(r for r in records if r['record_type'] == 'run'))
recovery_run.update(execution_kind='recovery', agent_key='runtime_recovery', max_turns=0, model_turns_used=0)
validator.validate(recovery_run)

checkpoint = next(r for r in records if r['record_type'] == 'checkpoint')
assert checkpoint['cursor']['handler_version'] == 'runtime.v1'
assert checkpoint['cursor']['state'] == runtime_example
confirmation = next(r for r in records if r['record_type'] == 'confirmation')
waiting_example = deepcopy(runtime_example)
waiting_example['pending_user_items'] = [{
    'id': confirmation['id'],
    'task_id': checkpoint['task_id'],
    'kind': 'confirmation',
    'question_id': None,
    'description': '请确认已展示的固定版本。',
    'presented_message_ids': confirmation['source_message_ids'],
    'targets': [{'subject': confirmation['subject'], 'selections': confirmation['selections']}],
    'state': 'open',
    'answer_message_ids': [],
    'replaced_by_id': None,
}]
runtime_validator.validate(waiting_example)

def mutate_runtime(base, label, mutation):
    candidate = deepcopy(base)
    mutation(candidate)
    assert not runtime_validator.is_valid(candidate), label
    negative.append(label)

mutate_runtime(runtime_example, 'runtime:extra_field', lambda r: r.update(undeclared_field=True))
mutate_runtime(runtime_example, 'runtime:stop_without_basis', lambda r: r['stop'].update(requested=True))
mutate_runtime(waiting_example, 'runtime:unversioned_confirmation',
               lambda r: r['pending_user_items'][0]['targets'][0]['subject'].update(version=None))
mutate_runtime(waiting_example, 'runtime:resolved_without_answer',
               lambda r: r['pending_user_items'][0].update(state='resolved'))

source = (ROOT / 'examples/source.txt').read_bytes()
blob = next(r for r in records if r['record_type'] == 'blob')
assert len(source) == blob['byte_size']
assert hashlib.sha256(source).hexdigest() == blob['sha256']

output_root = ROOT.parent / 'output-schemas/v2'
registry = json.loads((output_root / 'registry.json').read_text())
for item in registry['schemas']:
    assert hashlib.sha256((output_root / item['file']).read_bytes()).hexdigest() == item['sha256']
assert runtime_schema['$defs']['EvidenceRef'] == schema['$defs']['EvidenceRef']
assert runtime_schema['$defs']['Selection'] == schema['$defs']['Selection']
print(json.dumps({
    'record_types': len(expected),
    'valid_examples': len(records),
    'invalid_examples_rejected': len(negative),
    'fixture_references': 'passed',
    'runtime_schema_and_examples': 'passed',
    'zero_model_recovery_run': 'passed',
    'blob_hash': 'passed',
    'existing_output_schema_hashes_verified': len(registry['schemas']),
}, ensure_ascii=False, indent=2))
