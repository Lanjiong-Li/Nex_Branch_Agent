"""Small, read-only Brave LLM Context client for Agents' web search tool.

The API key is read only from BRAVE_SEARCH_API_KEY. Provider response bodies and
transport exceptions are deliberately never included in errors returned to agents.
"""
from __future__ import annotations

import json
import os
import re
from urllib.parse import urlsplit

import httpx


BRAVE_LLM_CONTEXT_URL = "https://api.search.brave.com/res/v1/llm/context"
MAX_RESULTS = 5
MAX_SNIPPETS_PER_RESULT = 2
MAX_SNIPPET_CHARS = 500
MAX_TOTAL_SNIPPET_CHARS = 3_000
MAX_TITLE_CHARS = 160
MAX_URL_CHARS = 1_000
MAX_RESULT_JSON_CHARS = 8_000
SEARCH_LANG_ALIASES = {
    "zh": "zh-hans", "zh-cn": "zh-hans", "zh-sg": "zh-hans",
    "zh-tw": "zh-hant", "zh-hk": "zh-hant", "ja": "jp",
}


class BraveSearchError(RuntimeError):
    """A safe, user-facing failure with a stable code for the tool wrapper."""

    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


def _bounded_text(value: object, limit: int) -> tuple[str, bool]:
    if not isinstance(value, str):
        return "", False
    value = value.strip()
    return value[:limit], len(value) > limit


def _safe_url(value: object) -> str | None:
    if not isinstance(value, str) or len(value) > MAX_URL_CHARS or any(ord(char) < 32 for char in value):
        return None
    try:
        parsed = urlsplit(value)
        if parsed.scheme not in {"https", "http"} or not parsed.hostname or parsed.username or parsed.password:
            return None
    except ValueError:
        return None
    return value


def _positive_int(value: int, name: str, maximum: int, minimum: int = 1) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise BraveSearchError("invalid_parameter", f"{name} 必须在 {minimum}–{maximum} 之间")
    return value


def _parameters(
    query: str,
    *,
    count: int,
    maximum_number_of_urls: int,
    maximum_number_of_tokens: int,
    safesearch: str,
    freshness: str | None,
    search_lang: str | None,
) -> dict[str, str | int]:
    if not isinstance(query, str) or not query.strip():
        raise BraveSearchError("invalid_query", "搜索词不能为空")
    query = query.strip()
    if len(query) > 600 or len(query.split()) > 75:
        raise BraveSearchError("invalid_query", "搜索词最多 600 字符、75 个词")
    if search_lang is None and re.search(r"[\u3400-\u9fff]", query):
        search_lang = "zh-hans"
    if isinstance(search_lang, str):
        search_lang = SEARCH_LANG_ALIASES.get(search_lang.lower(), search_lang.lower())
    if safesearch not in {"off", "moderate", "strict"}:
        raise BraveSearchError("invalid_parameter", "safesearch 必须为 off、moderate 或 strict")
    params: dict[str, str | int] = {
        "q": query,
        "count": _positive_int(count, "count", 10),
        "maximum_number_of_urls": _positive_int(maximum_number_of_urls, "maximum_number_of_urls", MAX_RESULTS),
        "maximum_number_of_tokens": _positive_int(maximum_number_of_tokens, "maximum_number_of_tokens", 4_096, 1_024),
        "safesearch": safesearch,
    }
    if freshness is not None:
        if freshness not in {"pd", "pw", "pm", "py"} and not re.fullmatch(
            r"\d{4}-\d{2}-\d{2}to\d{4}-\d{2}-\d{2}", freshness
        ):
            raise BraveSearchError("invalid_parameter", "freshness 格式无效")
        params["freshness"] = freshness
    if search_lang is not None:
        if not isinstance(search_lang, str) or not re.fullmatch(r"[a-zA-Z]{2,}(?:-[a-zA-Z0-9]{2,})?", search_lang):
            raise BraveSearchError("invalid_parameter", "search_lang 格式无效")
        params["search_lang"] = search_lang
    return params


def _normalize_response(payload: object, maximum_number_of_urls: int) -> dict:
    if not isinstance(payload, dict) or not isinstance(payload.get("grounding"), dict):
        raise BraveSearchError("invalid_response", "Brave Search 返回格式无效")
    generic = payload["grounding"].get("generic", [])
    sources = payload.get("sources", {})
    if not isinstance(generic, list) or not isinstance(sources, dict):
        raise BraveSearchError("invalid_response", "Brave Search 返回格式无效")

    results: list[dict] = []
    seen_urls: set[str] = set()
    remaining_chars = MAX_TOTAL_SNIPPET_CHARS
    truncated = False
    for item in generic:
        if len(results) >= maximum_number_of_urls:
            truncated = True
            break
        if not isinstance(item, dict):
            continue
        url = _safe_url(item.get("url"))
        if not url or url in seen_urls:
            continue
        metadata = sources.get(url, {})
        if not isinstance(metadata, dict):
            metadata = {}
        title, title_truncated = _bounded_text(item.get("title") or metadata.get("title"), MAX_TITLE_CHARS)
        snippets_raw = item.get("snippets", [])
        if not isinstance(snippets_raw, list):
            snippets_raw = []
        snippets: list[str] = []
        for snippet in snippets_raw:
            if len(snippets) >= MAX_SNIPPETS_PER_RESULT:
                truncated = True
                break
            text, snippet_truncated = _bounded_text(snippet, min(MAX_SNIPPET_CHARS, remaining_chars))
            truncated = truncated or snippet_truncated
            if text:
                snippets.append(text)
                remaining_chars -= len(text)
            if remaining_chars == 0:
                truncated = True
                break
        age = metadata.get("age")
        published_at = _bounded_text(age[1], 64)[0] if isinstance(age, list) and len(age) > 1 else None
        relative_age = _bounded_text(age[2], 64)[0] if isinstance(age, list) and len(age) > 2 else None
        results.append({
            "url": url,
            "title": title,
            "snippets": snippets,
            "published_at": published_at,
            "relative_age": relative_age,
        })
        truncated = truncated or title_truncated
        seen_urls.add(url)
        if remaining_chars == 0:
            break
    while results and len(json.dumps(results, ensure_ascii=False)) > MAX_RESULT_JSON_CHARS - 100:
        results.pop()
        truncated = True
    return {
        "status": "ok" if results else "no_results",
        "results": results,
        "result_count": len(results),
        "truncated": truncated,
    }


class BraveSearchClient:
    """Single-request search; an injected AsyncClient is owned by its caller."""

    def __init__(self, http_client: httpx.AsyncClient | None = None, *, timeout_seconds: float = 30.0):
        self.http_client = http_client
        self.timeout_seconds = timeout_seconds

    async def search(
        self,
        query: str,
        *,
        count: int = 5,
        maximum_number_of_urls: int = 5,
        maximum_number_of_tokens: int = 2_048,
        safesearch: str = "moderate",
        freshness: str | None = None,
        search_lang: str | None = None,
    ) -> dict:
        params = _parameters(
            query,
            count=count,
            maximum_number_of_urls=maximum_number_of_urls,
            maximum_number_of_tokens=maximum_number_of_tokens,
            safesearch=safesearch,
            freshness=freshness,
            search_lang=search_lang,
        )
        key = os.getenv("BRAVE_SEARCH_API_KEY", "").strip()
        if not key:
            raise BraveSearchError("not_configured", "Brave Search API Key 尚未配置")

        async def request(client: httpx.AsyncClient) -> httpx.Response:
            return await client.get(
                BRAVE_LLM_CONTEXT_URL,
                params=params,
                headers={"Accept": "application/json", "X-Subscription-Token": key},
                timeout=self.timeout_seconds,
            )

        try:
            if self.http_client is None:
                async with httpx.AsyncClient() as client:
                    response = await request(client)
            else:
                response = await request(self.http_client)
        except httpx.TimeoutException:
            raise BraveSearchError("timeout", "Brave Search 请求超时") from None
        except httpx.RequestError:
            raise BraveSearchError("network_error", "Brave Search 网络请求失败") from None

        if response.status_code == 429:
            raise BraveSearchError("rate_limited", "Brave Search 调用频率已达上限")
        if response.status_code == 401:
            raise BraveSearchError("unauthorized", "Brave Search API Key 无效")
        if response.status_code == 403:
            raise BraveSearchError("forbidden", "Brave Search 当前套餐无权访问此接口")
        if response.status_code == 422:
            raise BraveSearchError("invalid_parameter", "Brave Search 不支持指定的搜索参数或语言代码")
        if response.status_code >= 400:
            raise BraveSearchError("upstream_error", f"Brave Search 请求失败（HTTP {response.status_code}）")
        try:
            payload = response.json()
        except ValueError:
            raise BraveSearchError("invalid_response", "Brave Search 返回格式无效") from None
        return _normalize_response(payload, maximum_number_of_urls)
