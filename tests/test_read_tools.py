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
from branch_agent.workflow import Workflow, ref


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
              'tools': {'read_token_cap': 4000, 'enabled': ['list_records', 'read_record']}}
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


async def invoke(reader, name='list_records', **arguments):
    tool = next(tool for tool in reader.functions() if tool.name == name)
    raw = json.dumps(arguments)
    context = ToolContext(context=None, tool_name=name, tool_call_id='test-read-call', tool_arguments=raw)
    return await tool.on_invoke_tool(context, raw)


def source_event_case(store, project, original, *, title='相遇', event_id='GEV-1', quotes=None):
    workflow = Workflow(store)
    with store.transaction():
        source = workflow.save(project, 'source_text', original, effective=True)
    source_ref = ref(source)
    quotes = quotes or [original]
    anchors = []
    for quote in quotes:
        position = original.index(quote)
        start = len(original[:position].encode('utf-16-le')) // 2
        end = start + len(quote.encode('utf-16-le')) // 2
        anchors.append({'source_ref': source_ref, 'start_utf16': start, 'end_utf16': end,
                        'exact_quote': quote, 'prefix': None, 'suffix': None})
    output = {'result_kind': 'ready', 'payload': {
        'source_ref': source_ref,
        'global_events': [{'event_id': event_id, 'title': title, 'summary': '事件概要',
                           'narrative_order': 1, 'story_time': None, 'character_ids': [],
                           'source_anchors': anchors}],
        'covered_source_anchors': anchors, 'remaining_source_anchors': []},
        'questions': [], 'evidence_refs': [source_ref], 'notes': []}
    with store.transaction():
        view = workflow.save(project, 'source_global_events', output, stage=1,
                             inputs=[source_ref], effective=True)
    return workflow, source, view


@pytest.mark.asyncio
async def test_show_event_source_presents_markdown_outside_model_tool_result(read_case):
    store, pid, _, config, _ = read_case
    workflow, source, view = source_event_case(store, pid, '前文。甲🌍乙。后文。',
                                              quotes=['甲🌍乙。', '后文。'])
    presented = []
    reader = ReadTools(store, pid, config, present_event_source=lambda markdown, metadata:
        (presented.append((markdown, metadata)) or {'message_id': 'visible-message'}))
    assert 'show_event_source' in {tool.name for tool in reader.functions()}
    assert 'show_event_source' not in {tool.name for tool in ReadTools(store, pid, config).functions()}
    receipt = json.loads(await invoke(reader, 'show_event_source', event_query='相遇'))
    assert receipt['status'] == 'ok' and receipt['message_id'] == 'visible-message'
    assert receipt['event_ref']['record_id'] == view['artifact_id']
    assert receipt['source_ref'] == ref(source)
    assert '甲🌍乙。' not in json.dumps(receipt, ensure_ascii=False)
    markdown, metadata = presented[0]
    assert '甲🌍乙。' in markdown and '后文。' in markdown
    assert 'UTF-16 [3, 8)' in markdown
    assert metadata['event_id'] == 'GEV-1'
    workflow.save(pid, 'source_text', '被替换的新原作。', effective=True)
    old = reader.event_source_page('GEV-1', view['artifact_id'], '1')
    assert '甲🌍乙。' in old['markdown']
    assert '被替换' not in old['markdown']


@pytest.mark.asyncio
async def test_show_event_source_paging_and_title_ambiguity(read_case):
    store, pid, _, config, _ = read_case
    _, _, view = source_event_case(store, pid, '🌍甲乙丙丁戊己。后段。',
                                   quotes=['🌍甲乙丙丁戊己。', '后段。'])
    presented = []
    reader = ReadTools(store, pid, config, present_event_source=lambda markdown, metadata:
        (presented.append((markdown, metadata)) or {'message_id': str(len(presented))}))
    reader.SOURCE_EXCERPT_PAGE_CODEPOINTS = 4
    first = json.loads(await invoke(reader, 'show_event_source', event_query='GEV-1'))
    assert first['excerpt_codepoints'] == 4 and first['next_cursor'] == '0:4'
    assert '🌍甲乙丙' in presented[0][0]
    second = json.loads(await invoke(reader, 'show_event_source', event_query='相遇',
                                     artifact_id=view['artifact_id'], version='1',
                                     cursor=first['next_cursor']))
    assert second['next_cursor'] == '1:0'
    assert 'UTF-16 [5, 9)' in presented[1][0]
    invalid = await invoke(reader, 'show_event_source', event_query='相遇', cursor=first['next_cursor'])
    assert '续页必须使用' in invalid

    value = store.get(view['id'], pid)['content']['value']
    value['payload']['global_events'].append({**value['payload']['global_events'][0],
                                             'event_id': 'GEV-2', 'title': '再次相遇'})
    with store.transaction():
        revised = Workflow(store).save(pid, 'source_global_events', value, stage=1,
                                       inputs=[value['payload']['source_ref']], effective=True)
    assert revised['version'] == 2
    ambiguous = json.loads(await invoke(reader, 'show_event_source', event_query='遇'))
    assert ambiguous['status'] == 'ambiguous_event'
    assert {item['event_id'] for item in ambiguous['candidates']} == {'GEV-1', 'GEV-2'}
    assert len(presented) == 2
    selected = json.loads(await invoke(reader, 'show_event_source', event_query='GEV-1'))
    assert selected['status'] == 'ok' and selected['event_id'] == 'GEV-1'
    assert len(presented) == 3
    missing = json.loads(await invoke(reader, 'show_event_source', event_query='不存在的事件'))
    assert missing['status'] == 'event_not_found'


def test_show_event_source_keeps_original_markdown_literal(read_case):
    store, pid, _, config, _ = read_case
    original = '# 第一章\n```\n**原作里的星号**\n````'
    source_event_case(store, pid, original)
    reader = ReadTools(store, pid, config)
    page = reader.event_source_page('GEV-1')
    assert page['status'] == 'ok'
    assert '\n`````\n' + original + '\n`````' in page['markdown']


@pytest.mark.asyncio
async def test_history_snippet_uses_configured_tokens_and_exact_source_range(read_case):
    store, pid, _, config, history = read_case
    text = '早先的人物讨论。' * 100 + '灯塔回信' + '后续的剧情内容。' * 100
    row = history(1, text)
    reader = ReadTools(store, pid, config)
    result = json.loads(await invoke(reader, query='灯塔回信', record_type='history_record'))
    item = result['items'][0]
    assert '灯塔回信' in item['snippet']
    assert tokens(item['snippet'], config['model']['name']) <= 24
    assert item['record_ref']['record_id'] == row['id']
    assert item['truncated'] is True
    assert item['range']['unit'] == 'unicode_codepoints'
    assert text[item['range']['start']:item['range']['end']] == item['snippet']
    config['retrieval']['snippet_tokens'] = 80
    longer = json.loads(await invoke(reader, query='灯塔回信', record_type='history_record'))['items'][0]
    assert len(longer['snippet']) > len(item['snippet'])
    assert tokens(longer['snippet'], config['model']['name']) <= 80


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
        assert tokens(raw, config['model']['name']) <= 600
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
async def test_read_record_bounds_large_artifact_state_without_upgrading_partial_confirmation(read_case, selection_count):
    store, pid, _, config, _ = read_case
    config['tools'].update(read_token_cap=600, enabled=['list_records', 'read_record'])
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
        raw = await invoke(reader, 'read_record', record_id=artifact['id'],
                           version='1', cursor=cursor)
        assert tokens(raw, config['model']['name']) <= 600
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
        assert tokens(raw, config['model']['name']) <= 600
        page = json.loads(raw);parts.append(page['data']);cursor = page['next_cursor']
        if cursor is None:break
    else:pytest.fail('state scope cursor did not finish')
    assert json.loads(''.join(parts)) == selections
    assert reader.resolve(result['state_ref'])[1]['check_record_ids'] == [row['id'] for row in checks]
    state_hit = json.loads(await invoke(reader, query='partial', record_type='artifact_state'))['items'][0]
    assert state_hit['record_ref']['record_id'] == state['id']
    assert reader.resolve(state_hit['record_ref'])[0]['record_type'] == 'artifact_state'


@pytest.mark.asyncio
async def test_list_records_discovers_artifacts_and_read_record_resolves_effective_or_draft(read_case):
    store,pid,_,config,history=read_case
    artifact=store.put(new_record('artifact',pid,artifact_kind='adaptation_plan',
                                  scope={'stage':4,'chapter_ids':[],'branch_ids':[],'target_refs':[],'description':'方案'}))
    old=store.put(new_record('artifact_version',pid,artifact_id=artifact['id'],version=1,origin='program',
                             content={'storage':'inline_text','text':'已确认方案'}))
    draft_version=store.put(new_record('artifact_version',pid,artifact_id=artifact['id'],version=2,parent_version=1,origin='program',
                                       content={'storage':'inline_text','text':'未确认新稿'}))
    state=store.put(new_record('artifact_state',pid,artifact_id=artifact['id'],artifact_version_id=old['id'],
                               confirmation_status='confirmed',dependency_status='valid',quality_status='passed'))
    store.put(new_record('artifact_state',pid,artifact_id=artifact['id'],artifact_version_id=draft_version['id'],version=2,
                         confirmation_status='unconfirmed',dependency_status='valid',quality_status='unchecked'))
    artifact=store.update({**artifact,'latest_version':2,'current_effective_version':1},artifact['row_version'])
    reader=ReadTools(store,pid,config)
    listing=json.loads(await invoke(reader))
    assert len(listing['items'])==1
    item=listing['items'][0]
    assert item['artifact_kind']=='adaptation_plan'
    assert item['record_ref']['record_id']==artifact['id']
    assert item['latest_version']==2 and item['current_effective_version']==1
    assert item['markdown_download_url'].endswith(f'/artifacts/{artifact["id"]}/versions/2/download.md')
    assert item['effective_markdown_download_url'].endswith(f'/artifacts/{artifact["id"]}/versions/1/download.md')
    assert item['confirmation_status']=='unconfirmed'
    assert item['scope']['stage']==4
    assert '未确认新稿' not in json.dumps(listing,ensure_ascii=False)
    assert json.loads(await invoke(reader,stage=5))['status']=='no_matches'
    effective=json.loads(await invoke(reader,'read_record',artifact_kind='adaptation_plan'))
    assert effective['data']=='已确认方案' and effective['source_ref']['version']=='1'
    assert effective['state_ref']['record_id']==state['id']
    draft=json.loads(await invoke(reader,'read_record',record_id=artifact['id'],version='latest_draft'))
    assert draft['data']=='未确认新稿' and draft['source_ref']['version']=='2'
    assert draft['state']['confirmation_status']=='unconfirmed'


def test_legacy_snapshot_tool_names_map_to_new_read_tools(read_case):
    store,pid,_,config,_=read_case
    config['tools']['enabled']=['get_artifact','search_records','read_record']
    assert {tool.name for tool in ReadTools(store,pid,config).functions()}=={'list_records','read_record'}
