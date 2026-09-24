"""Read-only search against persisted records; no provider calls or business projects."""
import json
import os
from uuid import uuid4

from agents.tool_context import ToolContext
import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo
import pytest

from branch_agent.context import tokens
from branch_agent.model_service import ReadTools
from branch_agent.records import new_record
from branch_agent.storage import Store


@pytest.fixture
def read_case(tmp_path):
    dsn = os.getenv('BRANCH_AGENT_TEST_DSN', 'postgresql:///branch_agent_local')
    namespace = 'read_tool_test_' + uuid4().hex
    with psycopg.connect(dsn, autocommit=True) as connection:
        connection.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(namespace)))
    store = Store(make_conninfo(dsn, options='-c search_path=' + namespace), tmp_path / 'blobs')
    store.migrate()
    project = store.put(new_record('project', None, owner_account_id='read-tool-test', title='Read tool test'))
    pid = project['id']
    conversation = store.put(new_record('conversation', pid, title='History'))
    config = {'model': {'name': 'gpt-5.6-sol'}, 'retrieval': {'top_k': 8, 'snippet_tokens': 24, 'neighbor_messages': 0},
              'tools': {'read_token_cap': 4000, 'enabled': ['search_records', 'read_record']}}
    def history(sequence, content, *, conversation_id=None, project_id=None):
        return store.put(new_record('history_record', project_id or pid,
            conversation_id=conversation_id or conversation['id'], sequence=sequence,
            role='user' if sequence % 2 else 'assistant', content={'storage': 'inline_text', 'text': content}))
    try:
        yield store, pid, conversation, config, history
    finally:
        store.close()
        with psycopg.connect(dsn, autocommit=True) as connection:
            connection.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(namespace)))


async def invoke(reader, name='search_records', **arguments):
    tool = next(tool for tool in reader.functions() if tool.name == name)
    raw = json.dumps(arguments)
    context = ToolContext(context=None, tool_name=name, tool_call_id='test-read-call', tool_arguments=raw)
    return await tool.on_invoke_tool(context, raw)


@pytest.mark.asyncio
async def test_history_snippet_uses_configured_tokens_and_exact_source_range(read_case):
    store, pid, _, config, history = read_case
    text = '早先的人物讨论。' * 100 + '灯塔回信' + '后续的剧情内容。' * 100
    row = history(1, text)
    reader = ReadTools(store, pid, config)
    result = json.loads(await invoke(reader, query='灯塔回信', record_type='history_record'))
    item = result['items'][0]
    assert '灯塔回信' in item['snippet']
    assert tokens(item['snippet']) <= 24
    assert item['record_ref']['record_id'] == row['id']
    assert item['truncated'] is True
    assert item['range']['unit'] == 'unicode_codepoints'
    assert text[item['range']['start']:item['range']['end']] == item['snippet']
    config['retrieval']['snippet_tokens'] = 80
    longer = json.loads(await invoke(reader, query='灯塔回信', record_type='history_record'))['items'][0]
    assert len(longer['snippet']) > len(item['snippet'])
    assert tokens(longer['snippet']) <= 80


@pytest.mark.asyncio
async def test_neighbors_are_configured_same_conversation_messages_with_fixed_refs(read_case):
    store, pid, conversation, config, history = read_case
    config['retrieval']['neighbor_messages'] = 2
    rows = [history(i, '唯一匹配词' if i == 4 else f'正常对话第{i}条') for i in range(1, 8)]
    other = store.put(new_record('conversation', pid, title='Other conversation'))
    history(1, '相邻但不属于同一会话', conversation_id=other['id'])
    foreign = store.put(new_record('project', None, owner_account_id='someone-else', title='Foreign'))
    foreign_conversation = store.put(new_record('conversation', foreign['id'], title='Foreign history'))
    history(1, '唯一匹配词', project_id=foreign['id'], conversation_id=foreign_conversation['id'])
    reader = ReadTools(store, pid, config)
    result = json.loads(await invoke(reader, query='唯一匹配词', record_type='history_record'))
    assert {item['record_ref']['record_id'] for item in result['items']} == {row['id'] for row in rows[1:6]}
    for item in result['items']:
        assert item['conversation_id'] == conversation['id']
        assert item['role'] in ('user', 'assistant')
        _, content = reader.resolve(item['record_ref'])
        assert content[item['range']['start']:item['range']['end']] == item['snippet']
        if item['sequence'] != 4:
            assert item['match'] is False
            assert item['neighbor_of']['record_id'] == rows[3]['id']


@pytest.mark.asyncio
async def test_artifact_hits_keep_actual_version_and_do_not_gain_history_neighbors(read_case):
    store, pid, _, config, history = read_case
    config['retrieval']['neighbor_messages'] = 10
    artifact = store.put(new_record('artifact', pid, artifact_kind='test_search'))
    store.put(new_record('artifact_version', pid, artifact_id=artifact['id'], version=1, origin='program',
                         content={'storage': 'inline_text', 'text': '旧版本的产物关键词'}))
    store.put(new_record('artifact_version', pid, artifact_id=artifact['id'], version=2, parent_version=1, origin='program',
                         content={'storage': 'inline_text', 'text': '新版本不含搜索词'}))
    history(1, '产物关键词出现在其他记录类型')
    reader = ReadTools(store, pid, config)
    result = json.loads(await invoke(reader, query='产物关键词', record_type='artifact_version'))
    assert len(result['items']) == 1
    item = result['items'][0]
    assert item['record_ref']['record_id'] == artifact['id'] and item['record_ref']['version'] == '1'
    assert not item.get('neighbor_of')
    assert reader.resolve(item['record_ref'])[1] == '旧版本的产物关键词'


def test_owned_item_ids_resolve_without_counting_evidence_foreign_keys(read_case):
    from branch_agent.workflow import Workflow, WorkflowBlocked
    store,pid,_,config,_=read_case
    artifact=store.put(new_record('artifact',pid,artifact_kind='identity_fixture'))
    target={'record_id':artifact['id'],'version':'1','item_id':None,'json_pointer':None}
    change={'change_id':'change-choice','description':'把行动顺序改为玩家选择'}
    preservation={'item_id':'preserve-safety','category':'fact','content':'保留两个人物均安全',
                  'suggested_retention':'must_keep','rationale':'用户已指定','evidence_refs':[]}
    annotation={'annotation_id':'annotation-choice','event_id':'event-referenced',
                'function':'为关键选择提供明确后果'}
    content={'changes':[change],'preservation_items':[preservation],'annotations':[annotation],
             'refs':[{**target,'item_id':'change-choice'}, {**target,'item_id':'preserve-safety'}]}
    store.put(new_record('artifact_version',pid,artifact_id=artifact['id'],version=1,origin='program',
                        content={'storage':'inline_json','value':content}))
    reader=ReadTools(store,pid,config);workflow=Workflow(store)
    for identity,expected in [('change-choice',change),('preserve-safety',preservation),('annotation-choice',annotation)]:
        evidence={**target,'item_id':identity}
        assert reader.resolve(evidence)[1]==expected
        workflow.validate_evidence(pid,{'evidence_refs':[evidence]})
    for evidence in ({**target,'item_id':'invented'},
                     {**target,'item_id':'change-choice','json_pointer':'/preservation_items/0'},
                     {**target,'item_id':'event-referenced'}):
        with pytest.raises(ValueError):reader.resolve(evidence)
        with pytest.raises(WorkflowBlocked,match='evidence_item'):workflow.validate_evidence(pid,{'evidence_refs':[evidence]})


@pytest.mark.asyncio
async def test_budget_pagination_preserves_hits_and_neighbors_without_duplicates(read_case):
    store, pid, _, config, history = read_case
    config['retrieval'].update(top_k=1, snippet_tokens=200, neighbor_messages=2)
    config['tools']['read_token_cap'] = 600
    rows = [history(i, ('检索命中' if i in (5, 9) else '普通内容') + '这是较长的中文原始对话。' * 100)
            for i in range(1, 13)]
    reader = ReadTools(store, pid, config)
    cursor = None
    found = []
    cursors = set()
    for _ in range(20):
        raw = await invoke(reader, query='检索命中', record_type='history_record', cursor=cursor)
        assert tokens(raw) <= 600
        result = json.loads(raw)
        assert result['items']
        assert sum(item['match'] for item in result['items']) <= 1
        found.extend(result['items'])
        cursor = result['next_cursor']
        if cursor is None:
            break
        assert cursor not in cursors
        cursors.add(cursor)
    else:
        pytest.fail('search cursor never completed')
    ids = [item['record_ref']['record_id'] for item in found]
    assert len(ids) == len(set(ids))
    assert set(ids) == {row['id'] for row in rows[2:11]}
    assert {item['sequence'] for item in found if item['match']} == {5, 9}
    assert len(cursors) >= 2


@pytest.mark.asyncio
@pytest.mark.parametrize('cursor', ['-1', 'not-a-cursor', '9999'])
async def test_invalid_search_cursor_is_rejected_instead_of_slicing_records(read_case, cursor):
    store, pid, _, config, history = read_case
    history(1, '可查找的唯一记录')
    result = await invoke(ReadTools(store, pid, config), query='唯一记录', record_type='history_record', cursor=cursor)
    assert '分页' in result and '游标' in result
    assert '"status": "ok"' not in result


@pytest.mark.asyncio
@pytest.mark.parametrize('selection_count', [1, 120])
async def test_get_artifact_bounds_large_state_without_upgrading_partial_confirmation(read_case, selection_count):
    store, pid, _, config, _ = read_case
    config['tools'].update(read_token_cap=600, enabled=['get_artifact', 'read_record'])
    artifact = store.put(new_record('artifact', pid, artifact_kind='large_state_test'))
    content = '正文含有引号"、反斜杠\\和换行\n🌍。' * 90
    version = store.put(new_record('artifact_version', pid, artifact_id=artifact['id'], origin='program',
                                   content={'storage': 'inline_text', 'text': content}))
    checks = [store.put(new_record('runtime_event', pid, sequence=i + 1, event_name='test.checked', payload={}))
              for i in range(40)]
    selections = [{'item_id': None, 'json_pointer': f'/items/{index}'} for index in range(selection_count)]
    state = store.put(new_record('artifact_state', pid, artifact_id=artifact['id'], artifact_version_id=version['id'],
        confirmation_status='partial', dependency_status='valid', quality_status='passed',
        effective_selections=selections, check_record_ids=[row['id'] for row in checks]))
    reader = ReadTools(store, pid, config)
    chunks = [];cursor = None
    for _ in range(30):
        raw = await invoke(reader, 'get_artifact', artifact_kind='large_state_test', artifact_id=artifact['id'],
                           version='1', cursor=cursor)
        assert tokens(raw) <= 600
        result = json.loads(raw)
        actual = result['state']
        assert actual['confirmation_status'] == 'partial'
        assert actual['dependency_status'] == 'valid' and actual['quality_status'] == 'passed'
        assert result['state_ref']['record_id'] == state['id']
        if selection_count == 1:
            assert actual['effective_selections'] == selections
            assert actual['effective_selections_complete'] is True
        else:
            assert actual['effective_selections'] is None
            assert actual['effective_selections_complete'] is False
            assert actual['effective_selection_count'] == 120
        assert result['data']
        chunks.append(result['data'])
        cursor = result['next_cursor']
        if cursor is None:break
    else:pytest.fail('artifact cursor did not finish')
    assert ''.join(chunks) == content
    # The bounded projection still exposes the original state and all confirmation scope.
    parts = [];cursor = None
    for _ in range(40):
        raw = await invoke(reader, 'read_record', record_id=state['id'], json_pointer='/effective_selections', cursor=cursor)
        assert tokens(raw) <= 600
        page = json.loads(raw);parts.append(page['data']);cursor = page['next_cursor']
        if cursor is None:break
    else:pytest.fail('state scope cursor did not finish')
    assert json.loads(''.join(parts)) == selections
    assert reader.resolve(result['state_ref'])[1]['check_record_ids'] == [row['id'] for row in checks]
    state_hit = reader.search('partial', 'artifact_state')['items'][0]
    assert state_hit['record_ref']['record_id'] == state['id']
    assert reader.resolve(state_hit['record_ref'])[0]['record_type'] == 'artifact_state'
