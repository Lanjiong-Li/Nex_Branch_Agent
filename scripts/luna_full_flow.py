"""Bounded real-provider acceptance run; approvals are simulated test user messages.

Run explicitly with .venv/bin/python scripts/live_smoke.py. This spends API credit.
It keeps its isolated PostgreSQL schema for debugging and never inserts model output.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from decimal import Decimal
import json
import os
import re
from pathlib import Path
import sys
import time
from uuid import uuid4

import httpx
import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo
from dotenv import dotenv_values
from openai import AsyncOpenAI

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from branch_agent.actions import ActionService
from branch_agent.configuration import ConfigService
from branch_agent.context import BudgetExceeded, input_budget
from branch_agent.engine import Engine
from branch_agent.graph import validate_output, quality_checks
from branch_agent.model_service import ModelService
from branch_agent.records import new_record
from branch_agent.storage import Store
from branch_agent.workflow import all_records, body, ref

MAX_COST = Decimal(os.getenv('LUNA_SMOKE_MAX_COST', '10.00'))
MAX_SECONDS = int(os.getenv('LUNA_SMOKE_MAX_SECONDS', '1800'))
if not Decimal('0') < MAX_COST <= Decimal('20') or not 0 < MAX_SECONDS <= 3600:
    raise ValueError('Luna smoke limits must stay within USD 20 and 3600 seconds')
MODEL = 'gpt-5.6-luna'
LABEL = '【自动化验收模拟用户：确认仅在本测试项目生效】'
SOURCE = '''第一章 一盏灯
暴雨里，灯塔值守员阿澄接到妹妹小岚的无线电：她的小船停在礁石外，船舱安全，却看不清进港方向。
阿澄发现信号灯的防水罩裂了。备用电池只够亮一次灯，修罩也要花时间。小岚说，若错过此刻的潮水，她会把船系在避风浮标旁，等天亮的救援船。
阿澄先擦干接线，把电池装好，立即点亮信号灯。他用手掌遮住裂口，坚持到妹妹看清航道。
小岚顺着灯光靠岸，把外衣披到哥哥肩上。灯熄了，兄妹一起把门关上。'''
REQUIREMENTS = '''请把刚导入的原创线性故事《一盏灯》完成全流程互动改编，最终交付Nexo Graph JSON。
范围严格只有一章，玩家身份明确为灯塔值守员阿澄。保留灯塔暴雨场景、阿澄和小岚的兄妹关系、妹妹船舱安全、备用电池只够亮一次灯这些原作事实。
允许改写行动顺序，恰好两种结局：及时照明引导妹妹当晚靠岸；先修防水罩而错过潮水，妹妹按约系泊避风浮标，次日被救援船安全接回。禁止死亡、超自然、阴谋或无依据的人物。
采用默认互动策略，只需一个有明确后果的关键选择。所有阶段正常生成和逐项确认；不要跳过分析、策划、正文、Graph或审核。不必预先确认未生成的候选。
这是很小的验收项目，正文保持短而完整，各中间设计只保留必要条目；章节划分只提出一章。'''


class SmokeLimit(RuntimeError):
    pass


def measured_cost(store, project):
    total = Decimal('0')
    for call in all_records(store, project, 'model_call'):
        value = call['usage'].get('reported_cost') or call['usage'].get('estimated_cost')
        if value:
            total += Decimal(value['amount'])
        elif call['state'] in ('unknown', 'succeeded'):
            raise SmokeLimit('unknown_usage_or_outcome')
    return total


def answered_confirmation_ambiguity(engine, queued):
    """Ignore only an older simulated confirmation question later formally confirmed."""
    if queued['blocked_reason'] != 'intent_ambiguous':
        return False
    project = queued['project_id']
    message = engine.store.get(queued['source_message_id'], project)
    text = body(engine.store, message)
    marker = '本次确认对应待办：'
    if not isinstance(text, str) or marker not in text:
        return False
    try:
        pending, _ = json.JSONDecoder().raw_decode(text.split(marker, 1)[1])
        targets = pending['targets']
        if pending['kind'] != 'confirmation' or not targets:
            return False
        confirmations = all_records(engine.store, project, 'confirmation')
        for target in targets:
            if not any(c['action'] == 'confirm' and c['created_at'] > message['created_at']
                       and c['subject'] == target['subject']
                       and all(selection in c['selections'] for selection in target['selections'])
                       and message['id'] not in c['source_message_ids'] for c in confirmations):
                return False
            version = engine.workflow.fixed_version(project, target['subject'])
            if engine.workflow.state(version)['confirmation_status'] != 'confirmed':
                return False
        return True
    except (ValueError, KeyError, TypeError):
        return False


class BoundedTransport(httpx.AsyncBaseTransport):
    """Real HTTPS with a pre-send project-wide conservative budget reservation."""
    def __init__(self, store, project):
        self.store, self.project = store, project
        self.inner = httpx.AsyncHTTPTransport()
        self.started = None
        self.requests = 0
        self.reservations = []
        self.safe_provider_errors = []

    async def handle_async_request(self, request):
        if self.started is None:
            self.started = time.monotonic()
        data = json.loads(request.content)
        if data.get('model') != MODEL or data.get('reasoning', {}).get('effort') != 'medium':
            raise SmokeLimit('unexpected_model_or_reasoning')
        running = [c for c in all_records(self.store, self.project, 'model_call') if c['state'] == 'running']
        if len(running) != 1:
            raise SmokeLimit('physical_request_not_uniquely_audited')
        snapshot = self.store.get(running[0]['context_snapshot_id'], self.project)
        # Twice the SDK estimate plus 2k framing tokens; maximum configured output.
        input_bound = snapshot['input_token_estimate'] * 2 + 2000
        output_bound = data['max_output_tokens']
        multiplier = Decimal('2') if input_bound > 272000 else Decimal('1')
        output_multiplier = Decimal('1.5') if input_bound > 272000 else Decimal('1')
        reserve = (Decimal(input_bound) * Decimal('0.20') * multiplier
                   + Decimal(output_bound) * Decimal('1.20') * output_multiplier) / 1000000
        spent = measured_cost(self.store, self.project)
        if spent + reserve > MAX_COST:
            raise SmokeLimit('global_cost_reservation_limit')
        self.requests += 1
        self.reservations.append({'request': self.requests, 'model_call_id': running[0]['id'],
                                 'spent_usd': str(spent), 'reserved_upper_estimate_usd': str(reserve)})
        print(json.dumps({'event': 'real_request', 'request': self.requests,
                          'spent_usd': str(spent), 'reserve_usd': str(reserve)}, ensure_ascii=False), flush=True)
        response = await self.inner.handle_async_request(request)
        if response.status_code >= 400:
            await response.aread()
            try:
                error = response.json().get('error', {})
            except (ValueError, AttributeError):
                error = {}
            safe = {'status': response.status_code, 'model_call_id': running[0]['id']}
            for field in ('code', 'type'):
                value = error.get(field) if isinstance(error, dict) else None
                safe[field] = value if isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9_.-]{1,80}', value) else None
            self.safe_provider_errors.append(safe)
            print(json.dumps({'event': 'provider_rejected', **safe}), flush=True)
        return response

    async def aclose(self):
        await self.inner.aclose()


class BoundaryLimitedModelService(ModelService):
    """Test budget checks at SDK boundaries; never cancel an in-flight HTTP request."""
    def __init__(self, store, client):
        super().__init__(store, client)
        self.started = time.monotonic()

    async def run(self, stage, task, run, session, config, materials, message, control=None):
        async def bounded_control():
            if time.monotonic() - self.started >= MAX_SECONDS:
                raise BudgetExceeded('active_time_limit', '自动化验收的本轮活跃时间已到，保存结果并暂停')
            # The HTTP transport reserves against this call's fixed snapshot.
            if measured_cost(self.store, task['project_id']) >= MAX_COST:
                raise BudgetExceeded('cost_limit', '自动化验收的累计预算不足以预留下一次调用，保存结果并暂停')
            return await control() if control else {}
        return await super().run(stage, task, run, session, config, materials, message, control=bounded_control)


def describe_state(engine, pid):
    state = engine.status(pid)
    return {'tasks': [{'id': t['id'], 'stage': t['scope']['stage'], 'state': t['state'],
                      'reason': t['pause_reason']} for t in state['tasks']],
            'pending_user_items': state['pending_user_items'],
            'queue': [{'state': q['state'], 'reason': q['blocked_reason']} for q in state['queue']],
            'latest_delivery': state['latest_delivery'],
            'workflow_scopes': [{'task_id': t['id'], 'stages': engine._task_data(t).get('stages')}
                                for t in state['tasks'] if engine._task_data(t).get('is_workflow')]}


def archived_summary_failure(engine, task, tasks):
    """A retried parent's failed summary is retained history, not a new stop."""
    data = engine._task_data(task)
    parent = next((t for t in tasks if t['id'] == task['parent_task_id']), None)
    return (task['state'] == 'failed' and data.get('parent_owned') and data.get('stage') == 'aux.summary'
            and parent is not None and parent['state'] not in ('paused', 'failed', 'stopped'))


def verified_stage_reference_answer(engine, pid):
    """Answer a test clarification from actual confirmed dependency records."""
    plan = engine.workflow.resolve(pid, 'adaptation_plan')
    links = body(engine.store, plan)['payload']['stage_artifact_refs']
    verified = {}
    for field, kind in (('game_events', 'game_event_view'), ('event_functions', 'event_function_map')):
        fixed = engine.workflow.fixed_version(pid, links[field])
        state = engine.workflow.state(fixed)
        artifact = engine.store.get(fixed['artifact_id'], pid)
        if (artifact['artifact_kind'] != kind or state['dependency_status'] != 'valid'
                or state['confirmation_status'] not in ('confirmed', 'not_required')):
            raise SmokeLimit('cannot_verify_stage_reference_clarification')
        verified[field] = links[field]
    return ('已核对固定方案 ' + json.dumps(ref(plan), ensure_ascii=False)
            + ' 的 stage_artifact_refs 与 Step5/Step6 有效产物：' + json.dumps(verified, ensure_ascii=False)
            + '。请以这些实际存在且已确认的固定引用为当前完成状态；notes/1 是早期生成时说明，'
              '不要求撤回已完成阶段，也不授权跳过任何尚未完成阶段。请仅继续当前批次。')


async def main():
    resume_path = os.getenv('LUNA_SMOKE_RESUME_RECEIPTS')
    prior = json.loads(Path(resume_path).read_text()) if resume_path else None
    schema = prior['schema'] if prior else 'luna_smoke_' + datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S_') + uuid4().hex[:8]
    base_dsn = os.getenv('BRANCH_AGENT_TEST_DSN', 'postgresql:///branch_agent_local')
    if not prior:
        with psycopg.connect(base_dsn, autocommit=True) as conn:
            conn.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(schema)))
    store = Store(make_conninfo(base_dsn, options=f'-c search_path={schema}'), ROOT / '.data/luna-smoke' / schema / 'blobs')
    store.migrate()
    project = store.get(prior['project_id'], prior['project_id']) if prior else store.put(new_record('project', None, owner_account_id='automated-luna-acceptance', title='Luna 全流程验收：一盏灯'))
    pid = project['id']
    conversation = store.get(prior['conversation_id'], pid) if prior else store.put(new_record('conversation', pid, title='Luna 自动化验收模拟用户'))
    cid = conversation['id']
    configs = ConfigService(store)
    if not prior:
        draft = configs.draft(pid, {'model': {'name': MODEL, 'reasoning_effort': 'medium', 'max_output_tokens': 32000},
            'task': {'max_active_seconds': MAX_SECONDS,
            'max_cost': {'amount': str(MAX_COST), 'currency': 'USD'}}, 'retry': {'max_retries': 0}})
        configs.publish(pid, draft['id'])
    elif any(c['state'] in ('pending', 'running', 'unknown') for c in all_records(store, pid, 'model_call')):
        raise SmokeLimit('cannot_resume_unresolved_model_call')
    measured_cost(store, pid)  # Unknown successful usage also blocks resumption.
    key = dotenv_values(ROOT / '.env').get('OPENAI_API_KEY') or os.getenv('OPENAI_API_KEY')
    if not key:
        raise SmokeLimit('missing_api_key')
    transport = BoundedTransport(store, pid)
    client = AsyncOpenAI(api_key=key, max_retries=0, timeout=180,
                        http_client=httpx.AsyncClient(transport=transport))
    service = BoundaryLimitedModelService(store, client)
    engine = Engine(store, service, configs)
    resumed_tasks = []
    budget_controls = []
    if prior:
        explicit_resume = set(json.loads(os.getenv('LUNA_SMOKE_RESUME_TASK_IDS', '[]')))
        paused = [t for t in all_records(store, pid, 'task') if t['state'] == 'paused'
                  or (t['state'] == 'stopped' and t['id'] in explicit_resume)]
        for task in paused:
            # Continuing a parent can restore its saved-result batch child.
            # Re-read rather than issue a duplicate control for that child.
            if store.get(task['id'], pid)['state'] not in ('paused', 'stopped'):
                continue
            # The explicit test ceiling authorizes a simulated UI budget extension.
            # Frozen ConfigVersions stay unchanged; the outer project cap still applies.
            root_task = store.get(task['budget_root_task_id'], pid)
            prior_limit = Decimal(root_task['budget']['max_cost']['amount'])
            additional = max(Decimal('0.01'), MAX_COST - prior_limit)
            engine.control_task(pid, task['id'], 'continue', additional_seconds=MAX_SECONDS, additional_cost=str(additional))
            budget_controls.append({'label': LABEL, 'task_id': task['id'], 'budget_root_task_id': root_task['id'],
                'previous_limit_usd': str(prior_limit), 'additional_cost_usd': str(additional),
                'project_acceptance_hard_limit_usd': str(MAX_COST)})
            resumed_tasks.append(task['id'])
    else:
        engine.import_source(pid, cid, SOURCE, '一盏灯（验收原创短篇）')
        engine.submit_message(pid, cid, LABEL + '\n' + REQUIREMENTS)
    print(json.dumps({'event': 'started', 'schema': schema, 'project_id': pid, 'conversation_id': cid}), flush=True)
    started = time.monotonic()
    service.started = started
    handled = set(prior.get('handled_pending_ids', [])) if prior else set()
    questions = 0
    status = 'blocked'
    reason = None
    graph = None
    idle = 0
    try:
        for step in range(500):
            remaining = MAX_SECONDS - (time.monotonic() - started)
            if remaining <= 0:
                raise SmokeLimit('global_active_time_limit')
            before = len(all_records(store, pid, 'runtime_event'))
            await engine.tick(pid, cid)
            state = engine.status(pid)
            blocked = [t for t in state['tasks'] if t['state'] in ('paused', 'failed', 'stopped')
                       and not archived_summary_failure(engine, t, state['tasks'])]
            print(json.dumps({'event': 'tick', 'tick': step,
                'calls': len(all_records(store, pid, 'model_call')),
                'active_seconds': round(time.monotonic() - started, 2),
                'pending': [{'kind': p['kind'], 'description': p['description']} for p in state['pending_user_items']],
                'blocked': [{'stage': t['scope']['stage'], 'reason': t['pause_reason']} for t in blocked]}, ensure_ascii=False), flush=True)
            if state['latest_delivery']:
                graph = body(store, engine.workflow.fixed_version(pid, state['latest_delivery']['artifact_ref']))
                validate_output('nexo_graph', graph)
                if len(graph['chapters']) != 1:
                    raise SmokeLimit('delivered_chapter_count_not_one')
                status = 'succeeded'
                break
            if state['tasks'] and all(t['state'] == 'succeeded' or archived_summary_failure(engine, t, state['tasks']) for t in state['tasks']) and not any(q['state'] == 'pending' for q in state['queue']):
                scopes = describe_state(engine, pid)['workflow_scopes']
                raise SmokeLimit('workflow_scope_exhausted_before_graph:' + json.dumps(scopes, ensure_ascii=False))
            if blocked:
                raise SmokeLimit('runtime_blocked:' + json.dumps([{'stage': t['scope']['stage'], 'reason': t['pause_reason']} for t in blocked], ensure_ascii=False))
            cards = ActionService(engine).list_cards(pid, cid)['cards']
            pending = next((card for card in cards if card['kind'] in ('confirmation', 'question', 'decision')), None)
            if pending:
                if pending['kind'] == 'confirmation':
                    action_id, values = 'confirm', {}
                else:
                    action_id = 'answer'
                    answer_field = next(field for action in pending['actions'] if action['id'] == 'answer'
                                        for field in action['fields'] if field['name'] == 'answer')
                    options = answer_field['options']
                    if options and options[0]['value'] != '__custom__':
                        values = {'answer': options[0]['value']}
                    else:
                        values = {'answer': '没有额外想法，请按已确认材料继续。'}
                    questions += 1
                result = ActionService(engine).submit(pid, cid, pending['id'], action_id,
                                                      pending['revision'], values)
                handled.add(pending['id'])
                print(json.dumps({'event': 'first_answer', 'card_id': pending['id'],
                                  'kind': pending['kind'], 'action_id': action_id,
                                  'values': values, 'receipt': result['status']}, ensure_ascii=False), flush=True)
            else:
                queued = [q for q in state['queue'] if q['state'] == 'blocked'
                          and not answered_confirmation_ambiguity(engine, q)]
                if queued:
                    raise SmokeLimit('queue_blocked:' + ','.join(str(q['blocked_reason']) for q in queued))
            after = len(all_records(store, pid, 'runtime_event'))
            idle = idle + 1 if after == before else 0
            if idle > 2:
                raise SmokeLimit('scheduler_no_progress')
        else:
            reason = 'scheduler_tick_limit'
    except BaseException as error:
        reason = type(error).__name__ + ': ' + str(error)[:2000]
    finally:
        await client.close()
        elapsed = time.monotonic() - started
        calls = all_records(store, pid, 'model_call')
        stats = {field: sum(c['usage'][field] or 0 for c in calls) for field in
                 ('input_tokens', 'output_tokens', 'cached_input_tokens', 'reasoning_tokens')}
        costs = [c['usage'].get('reported_cost') or c['usage'].get('estimated_cost') for c in calls]
        cost = sum((Decimal(c['amount']) for c in costs if c), Decimal('0'))
        unknown = [c['id'] for c in calls if c['state'] in ('running', 'unknown') or (c['state'] == 'succeeded' and not (c['usage'].get('estimated_cost') or c['usage'].get('reported_cost')))]
        final = describe_state(engine, pid)
        history = all_records(store, pid, 'history_record')
        messages = [{'id': h['id'], 'role': h['role'], 'content': body(store, h)} for h in history if h['visibility'] == 'conversation']
        receipts = {'status': status, 'reason': reason, 'schema': schema, 'project_id': pid,
            'conversation_id': cid, 'elapsed_seconds': round(elapsed, 2), 'model': MODEL,
            'reasoning_effort': 'medium', 'physical_requests': len(calls),
            'physical_requests_this_invocation': transport.requests, 'resumed_task_ids': resumed_tasks,
            'simulated_budget_controls': budget_controls, 'project_acceptance_hard_limit_usd': str(MAX_COST),
            'resumed_from_receipts': resume_path,
            'handled_pending_ids': sorted(handled),
            'tokens': stats, 'estimated_cost_usd': str(cost), 'unknown_call_ids': unknown,
            'model_calls': calls, 'state': final, 'simulated_conversation': messages,
            'summary_page_reuse_events': all_records(store, pid, 'runtime_event', event_name='session.summary_page_reused'),
            'reservations': transport.reservations, 'quality_checks': quality_checks(graph) if graph else None}
        receipts['safe_provider_errors'] = transport.safe_provider_errors
        artifacts = ROOT / 'artifacts'
        artifacts.mkdir(exist_ok=True)
        (artifacts / 'luna-smoke.receipts.json').write_text(json.dumps(receipts, ensure_ascii=False, indent=2))
        if status == 'succeeded':
            (artifacts / 'luna-smoke.nexo.json').write_text(json.dumps(graph, ensure_ascii=False, indent=2))
        report = f'''# 真实模型端到端验收

结果：**{status}**。{reason or '正式交付事件产生，Nexo Graph 已通过现有Schema检查。'}

- 模型：gpt-5.6-luna；reasoning：medium。
- 本工程累计真实 API 请求：{len(calls)}（本轮恢复/运行发出 {transport.requests}）；ModelCall：{len(calls)}。
- 输入 tokens：{stats['input_tokens']}；输出 tokens：{stats['output_tokens']}；推理 tokens：{stats['reasoning_tokens']}；缓存输入：{stats['cached_input_tokens']}。
- 已归档估算费用：USD {cost}；用量或结果未知请求：{len(unknown)}。
- 活跃验收时间：{elapsed:.2f} 秒；本次限制 USD {MAX_COST} / {MAX_SECONDS} 秒。
- 隔离 PostgreSQL schema：`{schema}`；数据库：`branch_agent_local`（保留现场）。
- Project：`{pid}`；Conversation：`{cid}`。

故事为本次验收原创《一盏灯》，一章、玩家阿澄、一个关键选择、两个安全结局。所有创作与审核产物必须来自真实ModelService / Agents SDK调用；脚本只导入原作、提交请求和模拟用户确认，不直接修改确认状态或注入模型产物。

所有确认消息明确标记为“自动化验收模拟用户”，**不代表实际人类审核批准**。这是单个短篇样本，不能证明长篇、并发或所有故障恢复均可用。费用为当前配置费率估算，不是供应商账单；发送前按保守输入估计与输出硬上限预留预算，未知结果会停止后续请求。

本轮恢复Task：{json.dumps(resumed_tasks, ensure_ascii=False)}。恢复仅调用正常control_task继续入口，复用原确认消息与固定配置；未重建项目或重写模型调用状态。时间在SDK安全边界检查，已发HTTP允许在其180秒timeout内完成，以免测试时钟人为制造未知结果。

详见 [验收回执](../artifacts/luna-smoke.receipts.json)，包括真实调用、状态、模拟会话与预算预留。{'[最终Nexo Graph](../artifacts/luna-smoke.nexo.json)' if status == 'succeeded' else '未交付最终Graph，未生成成功导出文件。'}
'''
        (ROOT / 'docs/live-luna-smoke.md').write_text(report)
        print(json.dumps({'event': 'finished', 'status': status, 'reason': reason,
            'schema': schema, 'calls': len(calls), 'estimated_cost_usd': str(cost),
            'elapsed_seconds': round(elapsed, 2)}, ensure_ascii=False), flush=True)
        store.close()


if __name__ == '__main__':
    asyncio.run(main())
