"""Explicit, version-checked user actions; no model interprets these submissions."""
from copy import deepcopy
from decimal import Decimal, InvalidOperation
import hashlib

from .records import canonical_bytes, new_record, scope
from .workflow import WorkflowBlocked, STAGES, WHOLE, all_records, body, ref, update


def field(name, label, kind='text', required=True, options=None):
    return {'name': name, 'label': label, 'type': kind, 'required': required, 'options': options or []}


def action(identity, label, description, fields=None, disabled=None):
    return {'id': identity, 'label': label, 'description': description,
            'fields': fields or [], 'disabled_reason': disabled}


class ActionService:
    def __init__(self, engine):
        self.engine, self.store = engine, engine.store

    def _tasks(self, pid, cid):
        return all_records(self.store, pid, 'task', conversation_id=cid)

    def _tree(self, task, tasks):
        root = task
        by_id = {t['id']: t for t in tasks}
        while root['parent_task_id'] and root['parent_task_id'] in by_id:
            root = by_id[root['parent_task_id']]
        ids = {root['id']}
        while True:
            expanded = ids | {t['id'] for t in tasks if t['parent_task_id'] in ids}
            if expanded == ids:
                return root, [t for t in tasks if t['id'] in ids]
            ids = expanded

    def _calls(self, pid, tasks):
        ids = {t['id'] for t in tasks}
        return [c for c in all_records(self.store, pid, 'model_call') if c['task_id'] in ids]

    @staticmethod
    def _unknown_cost(call):
        details = (call.get('error') or {}).get('details', {})
        received = call['state'] == 'succeeded' or (call['state'] == 'failed' and details.get('known_outcome') is True
                    and details.get('terminal_status') in ('incomplete', 'failed', 'completed'))
        return received and not (call['usage'].get('reported_cost') or call['usage'].get('estimated_cost'))

    def _remote_block(self, tasks, calls):
        if any(c['state'] in ('pending', 'running', 'unknown') for c in calls):
            return '仍有远端结果未知或正在执行的调用，必须先核实，不能重复发起。'
        if any(t['state'] in ('running', 'stopping') and t['current_run_id'] for t in tasks):
            return '会话仍有执行中的任务，请等待停止完成。'
        return None

    def _source(self, pid):
        try:
            return self.engine.workflow.resolve(pid, 'source_text')
        except WorkflowBlocked:
            return None

    def _pending_validity(self, pid, cid, task, item):
        if task['state'] != 'waiting_user':
            return '任务当前不在等待输入状态。'
        for identity in item['presented_message_ids']:
            shown = self.store.get(identity, project_id=pid)
            if not shown or shown['role'] != 'assistant' or shown['conversation_id'] != cid:
                return '找不到对应的已展示问题，请刷新。'
        if not item['presented_message_ids']:
            return '该待办没有已展示范围，不能确认。'
        for target in item['targets']:
            try:
                version = self.engine.workflow.fixed_version(pid, target['subject'])
            except WorkflowBlocked:
                plan = self.engine._task_data(task).get('chapter_plan')
                if plan and target['subject']['record_id'] == plan['decision_id'] and target['subject']['version'] == str(plan['version']):
                    if plan.get('source_ref') != (ref(self._source(pid)) if self._source(pid) else None):
                        return '章节方案依赖的原作已变更，需要重新规划。'
                    continue
                return '待确认版本已不可用。'
            artifact = self.store.get(version['artifact_id'], project_id=pid)
            if artifact['latest_version'] != version['version'] or self.engine.workflow.state(version)['dependency_status'] != 'valid':
                return '此草稿已被新版本替代，或上游依赖已变化，请重新生成。'
        return None

    def _cards(self, pid, cid):
        cost_gates = self.engine.cost_gates_enabled
        conv = self.store.get(cid, project_id=pid)
        if not conv or conv['record_type'] != 'conversation':
            raise WorkflowBlocked('conversation_not_found')
        tasks = self._tasks(pid, cid)
        calls = self._calls(pid, tasks)
        gate = self.engine._control(pid, cid)
        source = self._source(pid)
        has_delivery = bool(all_records(self.store, pid, 'runtime_event', event_name='project.delivered'))
        remote_block = self._remote_block(tasks, calls)
        cards, config_cache = [], {}
        def add(card, snapshot, context):
            card['revision'] = hashlib.sha256(canonical_bytes({'card': card, 'state': snapshot})).hexdigest()
            cards.append((card, context))
        for task in tasks:
            data = self.engine._task_data(task)
            if (data.get('superseded_by_task_id') or data.get('invalid_stage_dispatch')
                    or data.get('resolved_by_manager_reconciliation')
                    or task['pause_reason'] == 'manager_prerequisite_pending'):
                continue
            for item in data.get('pending_user_items', []):
                if item['state'] != 'open':
                    continue
                disabled = self._pending_validity(pid, cid, task, item)
                if item['kind'] == 'confirmation':
                    actions = [action('confirm', '确认此版本', '仅确认卡片展示的固定版本和范围。', disabled=disabled),
                               action('request_changes', '提出修改', '保留旧稿，按填写的要求生成新稿。',
                                      [field('text', '修改要求', 'textarea')], disabled=disabled or remote_block)]
                else:
                    answers = item.get('suggested_answers', [])
                    fields = [field('answer', '你的选择或答复', 'select' if answers else 'textarea',
                              options=[{'value': a, 'label': a} for a in answers] +
                              ([{'value': '__custom__', 'label': '自行填写'}] if answers else []))]
                    if answers:
                        fields.append(field('text', '自行填写的答复', 'textarea', False))
                    actions = [action('answer', '提交答复', '只回答当前这一个问题；其他未答问题仍会保留。', fields, disabled)]
                scope_text = f"Step {task['scope']['stage']}" if task['scope']['stage'] else '全流程'
                if task['scope']['chapter_ids']:
                    scope_text += ' · 章节 ' + '、'.join(task['scope']['chapter_ids'])
                details = [{'label': '范围', 'value': scope_text}]
                if item.get('reason'):
                    details.append({'label': '提问原因', 'value': item['reason']})
                if item.get('blocking_scope'):
                    details.append({'label': '影响范围', 'value': item['blocking_scope']})
                targets = [deepcopy(t['subject']) for t in item['targets']]
                target_state = []
                for target in targets:
                    try:
                        v = self.engine.workflow.fixed_version(pid, target)
                        target_state += [self.engine.workflow.state(v), self.store.get(v['artifact_id'], project_id=pid)]
                    except WorkflowBlocked:
                        target_state.append(data.get('chapter_plan'))
                add({'id': 'pending:' + item['id'], 'kind': item['kind'],
                     'title': '确认产物' if item['kind'] == 'confirmation' else '需要你的答复',
                     'description': item['description'], 'agent_requested': bool(item.get('agent_requested')),
                     'details': details, 'targets': targets, 'actions': actions},
                    [{'id': task['id'], 'state': task['state']}, item, target_state,
                     ref(source) if source else None], {'task': task, 'pending': item})
            stale_wait = task['state'] == 'waiting_user' and any(p['state'] == 'open' and p['kind'] == 'confirmation'
                and self._pending_validity(pid, cid, task, p) for p in data.get('pending_user_items', []))
            if (task['state'] not in ('paused', 'stopped', 'failed') and not stale_wait) or data.get('parent_owned') or data.get('batch_owned'):
                continue
            descendants = {task['id']}
            while True:
                expanded = descendants | {t['id'] for t in tasks if t['parent_task_id'] in descendants}
                if expanded == descendants:
                    break
                descendants = expanded
            if task['pause_reason'] == 'child_blocked' and any(t['id'] != task['id'] and t['id'] in descendants
                and t['state'] in ('paused', 'stopped', 'failed') and not self.engine._task_data(t).get('parent_owned')
                and not self.engine._task_data(t).get('batch_owned') and not self.engine._task_data(t).get('superseded_by_task_id') for t in tasks):
                continue
            root, tree = self._tree(task, tasks)
            if root['id'] != task['id'] and root['state'] == 'succeeded':
                continue  # Closed workflows do not turn archived child failures into new work.
            tree_calls = self._calls(pid, tree)
            unknown = [c for c in tree_calls if self._unknown_cost(c)]
            stage = data.get('stage')
            scope_key = f'step{stage}' if stage in STAGES else 'step1'
            if scope_key not in config_cache:
                config_cache[scope_key] = self.engine.config_service.values(pid, scope_key)
            published, published_ids = config_cache[scope_key]
            oldcfg = self.store.get(data['config_version_id'], project_id=pid) if data.get('config_version_id') else None
            budget_fields = [field('max_active_seconds', '新任务有效执行时间上限（秒）', 'number')]
            budget_fields[0]['default'] = published['task']['max_active_seconds']
            if cost_gates:
                cost_field = field('max_cost_usd', '新任务费用上限（USD）', 'number')
                cost_field['default'] = (published['task']['max_cost'] or {}).get('amount')
                budget_fields.insert(0, cost_field)
            if cost_gates and unknown:
                budget_fields.insert(0, field('accept_unknown_cost', '我接受所列旧调用费用仍未知；它们不计入新任务独立预算', 'checkbox'))
            disabled = remote_block or (None if source else '请先导入原作。')
            rerun_disabled = disabled
            if stage not in STAGES:
                rerun_disabled = rerun_disabled or '此任务不是可独立重跑的创作阶段，请从 Step1 重新开始。'
            else:
                try:
                    self.engine.workflow.materials(pid, stage, data.get('chapter_id'))
                except WorkflowBlocked:
                    rerun_disabled = rerun_disabled or '该阶段的必需上游产物缺失或已失效，请从 Step1 重新开始。'
            details = [{'label': '任务', 'value': task['id']}, {'label': '将替代的原流程', 'value': root['id']},
                       {'label': '暂停原因', 'value': task['pause_reason'] or task['state']},
                       {'label': '当前原作', 'value': f"v{source['version']} ({source['artifact_id']})" if source else '未导入'},
                       {'label': '本次旧配置输出上限', 'value': str(oldcfg['values']['model']['max_output_tokens']) if oldcfg else '无阶段配置'},
                       {'label': '已发布配置输出上限', 'value': str(published['model']['max_output_tokens'])}]
            if cost_gates:
                details.append({'label': '旧调用费用未知', 'value': ', '.join(c['id'] for c in unknown) or '无'})
            try:
                views = self.engine.workflow.resolve(pid, 'source_views', effective=False)
                dependencies = [d for d in all_records(self.store, pid, 'dependency') if d['consumer_ref']['record_id'] == views['artifact_id'] and d['consumer_ref']['version'] == str(views['version'])]
                sources = [d['producer_ref']['version'] for d in dependencies if source and d['producer_ref']['record_id'] == source['artifact_id']]
                details.append({'label': '现有 Step1 分段', 'value': 'v' + str(views['version']) +
                    ('，依赖原作 v' + '、'.join(sources) if sources else '') + '，' +
                    ('上游已变化，需重新分段' if self.engine.workflow.state(views)['dependency_status'] != 'valid' else '来源仍有效')})
            except WorkflowBlocked:
                pass
            actions = [action('restart', '使用已发布配置从 Step1 重新开始',
                              '保留旧记录，替代此旧流程及其子任务；重新分段并逐阶段生成。' + ('新预算不包含未知旧费用。' if cost_gates else ''), budget_fields,
                              disabled or ('已有交付版本，请按具体阶段修订，或新建改编项目。' if has_delivery else None)),
                       action('rerun_published', '使用已发布配置重跑失败阶段',
                              '创建新任务和配置快照，复用仍有效的上游产物；旧任务保持归档。', budget_fields, rerun_disabled)]
            resume_disabled = remote_block or ('仍有费用未知，需先记录真实费用或明确接受后创建独立新任务。' if cost_gates and unknown else None)
            if task['state'] not in ('paused', 'stopped') or task['pause_reason'] in ('operation_uncertain', 'checkpoint_invalid', 'dependency_changed'):
                resume_disabled = resume_disabled or '此状态不能安全按旧配置继续；请选择重跑或重新开始。'
            prior_run = self.store.get(data['current_run_id'], project_id=pid) if data.get('current_run_id') else None
            if prior_run and not self.engine._dependencies_valid(prior_run):
                resume_disabled = resume_disabled or '原运行依赖的产物已失效，不能直接续跑；请重建所需上游阶段。'
            resume_fields = [field('additional_seconds', '附加执行秒数（留空为 0）', 'number', False)]
            if cost_gates:
                resume_fields.append(field('additional_cost', '附加费用 USD（留空为 0）', 'number', False))
            actions.insert(0, action('resume', '保留当前配置与进度继续',
                '沿用旧配置；附加执行时间留空为 0。' + ('费用按原预算及填写的追加额度执行。' if cost_gates else '测试版已暂停费用门禁。'),
                resume_fields, resume_disabled))
            if cost_gates and unknown:
                actions.append(action('report_usage', '填写有来源的实际费用', '只补充所选调用的实际账单费用，不自动继续任务。',
                    [field('model_call_id', '对应调用', 'select', options=[{'value': c['id'], 'label': c['id']} for c in unknown]),
                     field('reported_cost_usd', '实际费用（USD）', 'number'), field('source', '费用来源或账单依据', 'textarea')], remote_block))
            add({'id': 'task:' + task['id'], 'kind': 'recovery', 'title': '任务需要处理',
                 'description': '选择恢复方式，保留已有记录和进度。' + ('' if cost_gates else '测试版已暂停费用门禁，费用统计仍保留。'),
                 'details': details, 'targets': [ref(t) for t in tree], 'actions': actions},
                [task, data, tree, tree_calls, gate['holds'], ref(source) if source else None,
                 hashlib.sha256(canonical_bytes(published)).hexdigest(), published_ids],
                {'task': task, 'root': root, 'tree': tree, 'unknown_calls': unknown, 'source': source})
        for queued in all_records(self.store, pid, 'queued_request', conversation_id=cid):
            if queued['state'] not in ('pending', 'blocked'):
                continue
            unsafe = remote_block or ('已知终态调用的费用仍未知，请先填写费用或明确接受后新建任务。'
                                     if cost_gates and any(self._unknown_cost(c) for c in calls) else None)
            if queued['mode'] != 'queue' or queued['blocked_reason'] != 'queue_hold':
                unsafe = unsafe or '只有因队列暂停而阻塞的普通排队请求可以单项恢复。'
            add({'id': 'queue:' + queued['id'], 'kind': 'queue', 'title': '排队请求',
                 'description': body(self.store, self.store.get(queued['source_message_id'], project_id=pid)),
                 'details': [{'label': '状态', 'value': queued['state']}, {'label': '原因', 'value': queued['blocked_reason'] or '等待处理'}],
                 'targets': [ref(queued)], 'actions': [action('release_one', '仅恢复此排队请求',
                    '其他请求和全局暂停原因保持不变；新增暂停会使本次许可失效。', disabled=unsafe),
                    action('cancel', '取消此请求', '仅取消此请求，保留原消息和审计记录。')]},
                [queued, gate['holds'], [(c['id'], c['row_version']) for c in calls]], {'queue': queued})
        return cards

    def list_cards(self, pid, cid):
        return {'cards': [card for card, _ in self._cards(pid, cid)]}

    def submit(self, pid, cid, card_id, action_id, expected_revision, values):
        if not isinstance(values, dict):
            raise WorkflowBlocked('invalid_action_values')
        with self.store.transaction():
            self.store.advisory_lock(f'{pid}:materials')
            self.store.advisory_lock(f'{pid}:conversation:{cid}')
            self.store.advisory_lock('config:publish:' + pid)
            match = next(((c, ctx) for c, ctx in self._cards(pid, cid) if c['id'] == card_id), None)
            if not match:
                raise WorkflowBlocked('action_not_found')
            card, ctx = match
            if card['revision'] != expected_revision:
                raise WorkflowBlocked('action_stale')
            selected = next((a for a in card['actions'] if a['id'] == action_id), None)
            if not selected or selected['disabled_reason']:
                raise WorkflowBlocked('action_unavailable', {'reason': selected['disabled_reason'] if selected else '未知操作'})
            allowed = {f['name'] for f in selected['fields']}
            if set(values) - allowed:
                raise WorkflowBlocked('invalid_action_values')
            for f in selected['fields']:
                value = values.get(f['name'])
                if f['required'] and (value is None or value == '' or (f['type'] == 'checkbox' and value is not True)):
                    raise WorkflowBlocked('action_value_required', {'field': f['name']})
                if value is not None and f['type'] in ('text', 'textarea', 'select') and (not isinstance(value, str) or (f['required'] and not value.strip())):
                    raise WorkflowBlocked('invalid_action_values', {'field': f['name']})
                if f['type'] == 'select' and value not in [o['value'] for o in f['options']]:
                    raise WorkflowBlocked('invalid_action_values', {'field': f['name']})
            text = selected['label'] + '\n' + '\n'.join(f"{f['label']}：{values[f['name']]}" for f in selected['fields'] if f['name'] in values)
            if 'pending' in ctx:
                text = '对应待办：' + ctx['pending']['description'] + '\n' + text
            message = self.engine._message(pid, cid, text.strip(), role='user', task=ctx.get('task', {}).get('id'))
            if 'pending' in ctx:
                receipt = self._pending(pid, cid, ctx, action_id, values, message)
            elif 'queue' in ctx:
                receipt = self._queue(pid, cid, ctx['queue'], action_id, message)
            elif action_id == 'report_usage':
                receipt = self._report_usage(pid, ctx, values, message)
            elif action_id == 'resume':
                seconds = self._amount(0 if values.get('additional_seconds') in (None, '') else values['additional_seconds'], 'additional_seconds', zero=True)
                amount = self._amount(0 if values.get('additional_cost') in (None, '') else values['additional_cost'], 'additional_cost', zero=True)
                if seconds != seconds.to_integral_value():
                    raise WorkflowBlocked('invalid_action_values', {'field': 'additional_seconds'})
                if self.engine.cost_gates_enabled and ctx['task']['pause_reason'] == 'usage_uncertain':
                    # The card checks every relevant call is terminal with a known
                    # cost; the original Run error and billing evidence stay intact.
                    self.engine._event(pid, 'usage.uncertainty_resolved', {'task_id': ctx['task']['id'],
                        'source_message_id': message['id']}, conversation=cid, task=ctx['task']['id'], source=message['id'])
                    update(self.store, ctx['task'], pause_reason=None)
                self.engine.control_task(pid, ctx['task']['id'], 'continue', additional_seconds=int(seconds), additional_cost=str(amount))
                self._resume_waiting_ancestors(pid, cid, ctx['task'], message)
                root = ctx['root']
                if self.engine._task_data(root).get('manager_controlled'):
                    for task in self._tasks(pid, cid):
                        if task['parent_task_id'] != root['id'] or task['state'] != 'queued':
                            continue
                        data = self.engine._task_data(task)
                        if data.get('stage') in STAGES and not data.get('batch_owned'):
                            data['parent_owned'] = True
                            self.engine._save_task_data(task, data)
                    root = self.store.get(root['id'], project_id=pid)
                    self.engine._queue_manager_resume(root, message)
                receipt = {'status': 'queued', 'task_id': ctx['task']['id']}
            else:
                receipt = self._restart(pid, cid, ctx, action_id, values, message)
            event = self.engine._event(pid, 'action.applied', {'card_id': card_id, 'action_id': action_id,
                'expected_revision': expected_revision, 'values': deepcopy(values), 'source_message_id': message['id'], 'receipt': receipt},
                conversation=cid, task=receipt.get('task_id'), source=message['id'])
        self.engine._wake.set()
        return {**receipt, 'message_id': message['id'], 'event_id': event['id']}

    def _resume_waiting_ancestors(self, pid, cid, task, message):
        parent_id = task['parent_task_id']
        resumed = set()
        while parent_id:
            parent = self.store.get(parent_id, project_id=pid)
            children = all_records(self.store, pid, 'task', parent_task_id=parent_id)
            blocked = [t for t in children if t['state'] in ('paused', 'stopped', 'stopping', 'failed')
                       and not self.engine._task_data(t).get('superseded_by_task_id')]
            if parent['state'] != 'paused' or parent['pause_reason'] != 'child_blocked' or blocked:
                break
            self.engine._transition(parent, 'queued', source=message['id'])
            resumed.add(parent_id)
            parent_id = parent['parent_task_id']
        if resumed:
            gate = self.engine._control(pid, cid)
            released = [h for h in gate['holds'] if h['task_id'] in resumed and h['reason_code'] == 'child_blocked']
            gate['holds'] = [h for h in gate['holds'] if h not in released]
            gate['row_version'] += 1
            self.engine._save_projection(pid, 'conversation_control', cid, gate)
            self.engine._event(pid, 'queue.gate_changed', {'released_holds': released, 'holds': gate['holds'],
                'reason': 'explicit_child_resumed', 'source_message_id': message['id']}, conversation=cid, task=task['id'], source=message['id'])

    def _pending(self, pid, cid, ctx, action_id, values, message):
        task, item = ctx['task'], ctx['pending']
        data = self.engine._task_data(task)
        if action_id == 'confirm':
            plan = data.get('chapter_plan')
            if plan and any(t['subject']['record_id'] == plan['decision_id'] for t in item['targets']):
                request = {'target_ref': item['targets'][0]['subject'], 'requested_confirmation_paths': [''], 'source_message_ids': [message['id']]}
                return self.engine._confirm_chapters(task, plan, request, message, item['targets'], None)
            for target in item['targets']:
                request = {'target_ref': target['subject'], 'requested_confirmation_paths': [s['json_pointer'] for s in target['selections']],
                           'source_message_ids': [message['id']]}
                confirmation, version, complete = self.engine.workflow.confirm(pid, request, message, item['targets'])
                self.engine._event(pid, 'confirmation.recorded', {'confirmation_id': confirmation['id'], 'subject': ref(version), 'complete': complete},
                                   conversation=cid, task=task['id'], source=message['id'])
                if not complete:
                    raise WorkflowBlocked('confirmation_scope_incomplete')
                if data.get('stage') in STAGES:
                    self.engine.workflow.writeback_plan(pid, version, data['stage'])
            if not item['targets']:
                raise WorkflowBlocked('confirmation_scope_required')
        elif action_id == 'request_changes':
            return self._request_changes(pid, cid, task, item, values['text'], message)
        else:
            answer = values['answer']
            if answer == '__custom__':
                answer = values.get('text', '')
                if not isinstance(answer, str) or not answer.strip():
                    raise WorkflowBlocked('action_value_required', {'field': 'text'})
            data['request'] = data.get('request', '') + f"\n问题 {item.get('question_id') or item['id']}：{item['description']}\n用户明确答复（{message['id']}）：{answer}"
        for pending in data['pending_user_items']:
            if pending['id'] == item['id']:
                pending.update(state='resolved', answer_message_ids=[message['id']])
        remaining = any(p['state'] == 'open' for p in data['pending_user_items'])
        if not remaining and data.get('batch_owned') and data.get('current_run_id'):
            data.setdefault('answered_needs_input_run_ids', []).append(data['current_run_id'])
            data.pop('resume_saved_result', None)
        self.engine._save_task_data(task, data)
        if not remaining:
            if data.get('coordinator') and not data.get('chapter_planning'):
                # The clarified input becomes an exact new user request. The old
                # ambiguous request and all its questions remain in the archive.
                old_queue = self.store.get(data['queue_id'], project_id=pid)
                update(self.store, old_queue, state='cancelled', resolved_at=self.store.now(), blocked_reason=None)
                clarified = self.store.put(new_record('queued_request', pid, conversation_id=cid,
                    source_message_id=message['id'], mode='queue', target_run_id=None, sequence=message['sequence'],
                    scope=scope(description='根据明确答复处理先前请求'), state='pending', adopted_run_id=None,
                    adopted_context_snapshot_id=None, created_task_id=None, resolved_at=None, blocked_reason=None))
                presented = self.engine._projection(pid, 'presentations', cid, targets=[])
                self.engine._save_projection(pid, 'queue_context', clarified['id'], {
                    'presented': deepcopy(presented['targets']), 'request_hash': hashlib.sha256(canonical_bytes([old_queue['id'], message['id']])).hexdigest(),
                    'clarification_context': data.get('request', ''), 'original_queue_id': old_queue['id'],
                    'continuation_parent_task_id': task['id'], 'continuation_config_version_id': data.get('config_version_id')})
                self.engine._event(pid, 'coordinator.clarified', {'previous_queue_id': old_queue['id'],
                    'answer_message_ids': [i for p in data['pending_user_items'] for i in p['answer_message_ids']],
                    'new_source_message_id': message['id'], 'new_queue_id': clarified['id']}, conversation=cid, task=task['id'], source=message['id'])
                self.engine._transition(task, 'succeeded', source=message['id'])
            else:
                parent = self.store.get(task['parent_task_id'], project_id=pid) if task['parent_task_id'] else None
                if action_id != 'confirm' and parent and self.engine._task_data(parent).get('manager_controlled'):
                    data['parent_owned'] = True
                    self.engine._save_task_data(task, data)
                self.engine._transition(task, 'succeeded' if action_id == 'confirm' else 'queued', source=message['id'])
                if parent and self.engine._task_data(parent).get('manager_controlled'):
                    self.engine._queue_manager_resume(parent, message)
            if data.get('batch_owned') and task['parent_task_id']:
                parent = self.store.get(task['parent_task_id'], project_id=pid)
                pdata = self.engine._task_data(parent)
                if parent['state'] == 'waiting_user' and pdata.get('waiting_batch_task_id') == task['id']:
                    pdata.pop('waiting_batch_task_id')
                    self.engine._save_task_data(parent, pdata)
                    self.engine._transition(parent, 'queued', source=message['id'])
        return {'status': 'waiting_user' if remaining else 'confirmed' if action_id == 'confirm' else 'queued', 'task_id': task['id']}

    def _request_changes(self, pid, cid, task, item, text, message, *, schedule_manager_resume=True):
        data = self.engine._task_data(task)
        if data.get('chapter_plan') and data.get('is_workflow'):
            data['request'] = data.get('request', '') + '\n用户要求调整章节：' + text
            data['chapter_plan_source_message_id'] = message['id']
            data.pop('chapter_plan', None)
            data['chapter_plan_requested'] = False
            for p in data['pending_user_items']:
                if p['id'] == item['id']:
                    p.update(state='resolved', answer_message_ids=[message['id']])
            self.engine._save_task_data(task, data)
            self.engine._transition(task, 'queued', source=message['id'])
            if schedule_manager_resume and data.get('manager_controlled'):
                self.engine._queue_manager_resume(task, message)
            return {'status': 'queued', 'task_id': task['id']}
        if data.get('stage') not in STAGES or data.get('batch_owned'):
            raise WorkflowBlocked('action_unavailable', {'reason': '当前范围不能直接生成修订稿。'})
        parent = self.store.get(task['parent_task_id'], project_id=pid) if task['parent_task_id'] else None
        replacement = self.engine._new_task(pid, cid, message, 'modify', data['stage'], data.get('chapter_id'), parent=parent,
            request=f"针对固定版本 {item['targets']}，按用户明确要求修改：{text}")
        if not parent:
            replacement = update(self.store, replacement, budget_root_task_id=task['budget_root_task_id'])
        self._freeze(replacement, data['stage'])
        if parent and self.engine._task_data(parent).get('manager_controlled'):
            replacement_data = self.engine._task_data(replacement)
            replacement_data['parent_owned'] = True
            self.engine._save_task_data(replacement, replacement_data)
        self._supersede(pid, cid, [task], replacement, message)
        if schedule_manager_resume and parent and self.engine._task_data(parent).get('manager_controlled'):
            self.engine._queue_manager_resume(parent, message)
        return {'status': 'queued', 'task_id': replacement['id']}

    def _freeze(self, task, stage):
        config = self.engine.config_service.resolve(task['project_id'], f'step{stage}')
        data = self.engine._task_data(task)
        data['config_version_id'] = config['id']
        self.engine._save_task_data(task, data)

    def _supersede(self, pid, cid, tasks, replacement, message):
        ids = {t['id'] for t in tasks}
        for task in tasks:
            data = self.engine._task_data(task)
            data['superseded_by_task_id'] = replacement['id']
            for item in data.get('pending_user_items', []):
                if item['state'] == 'open':
                    item.update(state='cancelled', answer_message_ids=[message['id']])
            self.engine._save_task_data(task, data)
            if task['state'] not in ('succeeded', 'failed', 'stopped'):
                # This is replacement, not a stop request: do not create new stop holds.
                update(self.store, task, state='stopped', current_run_id=None)
        gate = self.engine._control(pid, cid)
        released = [h for h in gate['holds'] if h['task_id'] in ids]
        gate['holds'] = [h for h in gate['holds'] if h['task_id'] not in ids]
        gate['row_version'] += 1
        self.engine._save_projection(pid, 'conversation_control', cid, gate)
        self.engine._event(pid, 'workflow.superseded', {'old_task_ids': sorted(ids), 'replacement_task_id': replacement['id'],
            'released_holds': released, 'remaining_holds': gate['holds'], 'source_message_id': message['id']},
            conversation=cid, task=replacement['id'], source=message['id'])

    @staticmethod
    def _amount(value, name, *, zero=False):
        try:
            if isinstance(value, bool):
                raise InvalidOperation
            amount = Decimal(str(value))
            if not amount.is_finite() or amount < 0 or (not zero and amount == 0):
                raise InvalidOperation
            return amount
        except (InvalidOperation, ValueError, TypeError):
            raise WorkflowBlocked('invalid_action_values', {'field': name}) from None

    def _restart(self, pid, cid, ctx, action_id, values, message):
        old, root = ctx['task'], ctx['root']
        cost = self._amount(values.get('max_cost_usd'), 'max_cost_usd') if self.engine.cost_gates_enabled else None
        seconds = self._amount(values.get('max_active_seconds'), 'max_active_seconds')
        if seconds != seconds.to_integral_value():
            raise WorkflowBlocked('invalid_action_values', {'field': 'max_active_seconds'})
        olddata = self.engine._task_data(old)
        rootdata = self.engine._task_data(root)
        fresh = action_id == 'restart'
        stage = 1 if fresh else olddata['stage']
        request = ('从当前原作 Step1 重新开始完整改编。' if fresh else f'使用新配置重跑 Step{stage} 并继续原授权流程。')
        request += '\n原始创作要求：' + rootdata.get('request', '')
        newroot = self.engine._new_task(pid, cid, message, 'generate', is_workflow=True,
            stages=list(range(1,12)) if fresh else rootdata.get('stages', [stage]),
            chapter_ids=[] if fresh else deepcopy(rootdata.get('chapter_ids', [])),
            request=request, fresh_start=fresh, source_ref=ref(ctx['source']), replaces_task_id=root['id'],
            manager_controlled=bool(rootdata.get('manager_controlled')),
            recovery_stage=stage if rootdata.get('manager_controlled') else None)
        limits = deepcopy(newroot['budget'])
        limits.update(max_cost={'amount': str(cost), 'currency': 'USD'} if cost is not None else None,
                      max_active_seconds=int(seconds), disabled_limits=[] if self.engine.cost_gates_enabled else ['cost'])
        newroot = update(self.store, newroot, budget=limits)
        self._supersede(pid, cid, ctx['tree'], newroot, message)
        if rootdata.get('manager_controlled'):
            self.engine._queue_manager_resume(newroot, message)
            child = None
        else:
            child = self.engine._dispatch(newroot, stage, None if fresh else olddata.get('chapter_id'), request=request)
            self._freeze(child, stage)
        if self.engine.cost_gates_enabled and ctx['unknown_calls']:
            self.engine._event(pid, 'usage.unknown_accepted', {'model_call_refs': [ref(c) for c in ctx['unknown_calls']],
                'old_budget_root_task_id': root['budget_root_task_id'], 'new_budget_root_task_id': newroot['id'],
                'new_budget': limits, 'source_message_id': message['id']}, conversation=cid, task=newroot['id'], source=message['id'])
        return {'status': 'queued', 'task_id': newroot['id'], 'stage_task_id': child['id'] if child else None}

    def _report_usage(self, pid, ctx, values, message):
        call = next((c for c in ctx['unknown_calls'] if c['id'] == values['model_call_id']), None)
        if not call:
            raise WorkflowBlocked('action_stale')
        amount = self._amount(values['reported_cost_usd'], 'reported_cost_usd', zero=True)
        measured = deepcopy(call['usage'])
        measured['reported_cost'] = {'amount': str(amount), 'currency': 'USD'}
        update(self.store, call, usage=measured)
        self.engine._event(pid, 'usage.reported', {'model_call_ref': ref(call), 'reported_cost': measured['reported_cost'],
            'source': values['source'], 'source_message_id': message['id']},
            conversation=message['conversation_id'], task=call['task_id'], source=message['id'])
        return {'status': 'recorded', 'task_id': ctx['task']['id'], 'model_call_id': call['id']}

    def _queue(self, pid, cid, queued, action_id, message):
        if action_id == 'cancel':
            update(self.store, queued, state='cancelled', resolved_at=self.store.now(), blocked_reason=None)
            return {'status': 'cancelled', 'queue_id': queued['id']}
        gate = self.engine._control(pid, cid)
        data = self.engine._projection(pid, 'queue_context', queued['id'])
        data['single_release'] = {'basis_event_ids': sorted(h['basis_event_id'] for h in gate['holds']),
                                  'source_message_id': message['id'], 'queue_id': queued['id']}
        self.engine._save_projection(pid, 'queue_context', queued['id'], data)
        update(self.store, queued, state='pending', blocked_reason=None)
        return {'status': 'pending', 'queue_id': queued['id']}
