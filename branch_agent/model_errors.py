"""Safe classifications derived from provider fields, never provider message text."""
from __future__ import annotations


def http_failure(exc: Exception) -> dict | None:
    """Classify explicit HTTP rejections by status and allowlisted error codes."""
    status = getattr(exc, 'status_code', None)
    if type(status) is not int:
        return None
    body = getattr(exc, 'body', None)
    body = body if isinstance(body, dict) else {}
    error = body.get('error', body)
    error = error if isinstance(error, dict) else {}
    provider_code = error.get('code')
    provider_type = error.get('type')
    code = type(exc).__name__
    message = '供应商明确拒绝本次请求，具体原因尚未识别；请查看受控运行记录。'
    retryable = status in (429, 500, 502, 503, 504)
    details = {'http_status': status, 'known_outcome': True, 'exception_class': code}
    if status == 401:
        code, message = 'authentication_failed', 'API 凭据认证失败；请检查此项目使用的 API Key 是否有效。'
    elif status == 403:
        code, message = 'permission_denied', '供应商禁止本次访问；请检查项目、模型权限及服务可用范围。'
    elif status == 429 and provider_code in ('insufficient_quota', 'credit_balance_exhausted'):
        code, message, retryable = 'quota_exhausted', 'API 账户额度或余额不足；请检查计费设置后继续。', False
    elif status == 429 and (provider_code == 'rate_limit_exceeded' or provider_type == 'rate_limit_error'):
        code, message = 'rate_limit_exceeded', '请求触及供应商速率限制；请稍后重试或降低并发。'
    elif status == 404 and provider_code == 'model_not_found':
        code, message = 'model_not_found', '指定模型不存在或当前项目无权访问；请检查模型名称和权限。'
    elif status == 400 and provider_code == 'context_length_exceeded':
        code, message = 'context_length_exceeded', '请求超过模型上下文长度限制；请减少输入或调整上下文预算。'
    elif status == 400 and provider_code in ('invalid_json_schema', 'invalid_schema', 'invalid_function_parameters'):
        code, message = 'invalid_output_schema', '供应商拒绝了结构化输出或工具参数 Schema；请检查对应 Schema 定义。'
    if provider_code in ('invalid_api_key', 'insufficient_quota', 'credit_balance_exhausted',
                         'rate_limit_exceeded', 'model_not_found', 'context_length_exceeded',
                         'invalid_json_schema', 'invalid_schema', 'invalid_function_parameters'):
        details['provider_error_code'] = provider_code
    return {'code': code, 'message': message + f'（HTTP {status}）',
            'retryable': retryable, 'details': details}


def terminal_failure(response: dict, event_type: str | None = None) -> dict | None:
    status = response.get('status')
    details = {'known_outcome': status in ('completed', 'incomplete', 'failed'),
               'terminal_status': status if status in ('completed', 'incomplete', 'failed', 'queued', 'in_progress', 'cancelled') else None,
               'terminal_event': event_type or ('response.' + status if status in ('completed', 'incomplete', 'failed') else None)}
    def error(code, message):
        return {'code': code, 'message': message, 'retryable': False, 'details': details}
    if status == 'incomplete':
        reason = (response.get('incomplete_details') or {}).get('reason')
        details['incomplete_reason'] = reason if reason in ('max_output_tokens', 'content_filter') else 'unrecognized'
        if reason == 'max_output_tokens':
            return error('output_limit_exceeded', '模型已返回，但因达到输出 token 上限而截断；该响应未作为有效产物。')
        if reason == 'content_filter':
            return error('response_incomplete', '供应商因内容策略返回未完成响应；该响应未作为有效产物。')
        return error('response_incomplete', '供应商已返回未完成的响应；该响应未作为有效产物。')
    if status == 'failed':
        code = (response.get('error') or {}).get('code')
        allowed = {'server_error', 'rate_limit_exceeded', 'invalid_prompt', 'vector_store_timeout',
                   'invalid_image', 'invalid_image_format', 'invalid_base64_image', 'image_too_large',
                   'image_too_small', 'image_parse_error', 'image_content_policy_violation', 'invalid_image_url',
                   'image_file_too_large', 'unsupported_image_media_type', 'empty_image_file', 'failed_to_download_image',
                   'image_file_not_found'}
        details['provider_error_code'] = code if code in allowed else 'unrecognized'
        suffix = f'（{code}）' if code in allowed else ''
        return error('response_failed', f'供应商已明确返回失败终态{suffix}；该响应未作为有效产物。')
    if status != 'completed':
        return error('response_not_terminal', '已收到响应，但未确认完成终态；该响应未作为有效产物。')
    output = response.get('output')
    if not isinstance(output, list):
        return error('ModelBehaviorError', '模型响应的输出结构不符合协议；请按当前输出 Schema 和工具定义修正。')
    if any(part.get('type') == 'refusal' for item in output if isinstance(item, dict)
           for part in (item.get('content') or []) if isinstance(part, dict)):
        return error('model_refusal', '模型已明确拒绝本次请求；拒答未作为有效产物。')
    return None
