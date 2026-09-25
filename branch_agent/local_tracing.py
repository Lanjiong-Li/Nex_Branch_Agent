"""Keep Agents SDK process spans in the project database, without provider export.

Only allowlisted timing, names and token counts are persisted here. The existing
model_call/context_snapshot/tool_call audit remains the source for input/output.
"""
from __future__ import annotations

import logging
from threading import Lock

from agents.tracing import set_trace_processors
from psycopg.types.json import Jsonb

LOG = logging.getLogger(__name__)


class LocalTraceProcessor:
    def __init__(self):
        self._lock = Lock()
        self._targets = {}

    def register(self, trace_id, store, project_id, run_id):
        with self._lock:
            self._targets[trace_id] = (store, project_id, run_id)

    def unregister(self, trace_id):
        with self._lock:
            self._targets.pop(trace_id, None)

    def on_trace_start(self, trace):
        pass

    def on_trace_end(self, trace):
        pass

    def on_span_start(self, span):
        self._save(span)

    def on_span_end(self, span):
        self._save(span)

    def _save(self, span):
        with self._lock:
            target = self._targets.get(span.trace_id)
        if target is None:
            return
        store, project_id, run_id = target
        data = span.span_data
        kind = str(getattr(data, 'type', 'unknown'))[:48]
        name = (f"第 {getattr(data, 'turn')} 轮" if kind == 'turn' else
                '模型调用' if kind == 'response' else
                getattr(data, 'name', None) or getattr(data, 'model', None) or kind)
        metadata = {}
        if kind in ('generation', 'response', 'turn', 'task'):
            model = getattr(data, 'model', None)
            if model:
                metadata['model'] = str(model)[:120]
            usage = getattr(data, 'usage', None)
            if isinstance(usage, dict):
                metadata['usage'] = {key: value for key, value in usage.items()
                    if key in ('input_tokens', 'output_tokens', 'total_tokens') and type(value) is int}
        elif kind == 'handoff':
            metadata = {key: str(value)[:120] for key in ('from_agent', 'to_agent')
                        if (value := getattr(data, key, None)) is not None}
        elif kind == 'guardrail':
            metadata['triggered'] = bool(getattr(data, 'triggered', False))
        error = getattr(span, 'error', None)
        error_code = str(error.get('type') or 'error')[:120] if isinstance(error, dict) else None
        try:
            store._connection().execute('''
                INSERT INTO sdk_trace_spans
                (project_id,run_id,trace_id,span_id,parent_id,kind,name,started_at,ended_at,error_code,metadata)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (project_id,span_id) DO UPDATE SET
                    ended_at=EXCLUDED.ended_at,error_code=EXCLUDED.error_code,metadata=EXCLUDED.metadata
                ''', (project_id, run_id, span.trace_id, span.span_id, span.parent_id,
                      kind, str(name)[:200], span.started_at, span.ended_at, error_code, Jsonb(metadata)))
        except Exception:
            # Observability must never break an Agent Run.
            LOG.exception('Unable to persist local SDK span')

    def shutdown(self):
        pass

    def force_flush(self):
        pass


_processor = LocalTraceProcessor()
_installed = False
_install_lock = Lock()


def register_run(store, project_id, run_id):
    global _installed
    with _install_lock:
        if not _installed:
            # Replace the SDK default exporter: traces must remain on this server.
            set_trace_processors([_processor])
            _installed = True
    trace_id = 'trace_' + run_id.replace('-', '')
    _processor.register(trace_id, store, project_id, run_id)
    return trace_id


def unregister_run(trace_id):
    _processor.unregister(trace_id)
