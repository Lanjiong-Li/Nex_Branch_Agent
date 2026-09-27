"""Brave search uses only a mocked HTTP transport and a fake test credential."""
import json
import httpx
import pytest

from branch_agent.brave_search import BraveSearchClient, BraveSearchError


@pytest.mark.asyncio
async def test_search_uses_llm_context_endpoint_and_bounded_source_results(monkeypatch):
    monkeypatch.setenv("BRAVE_SEARCH_API_KEY", "fake-test-key")
    requests = []
    long_snippet = "甲" * 900

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json={
            "grounding": {"generic": [
                {"url": "https://example.org/one", "title": "第一页", "snippets": [long_snippet, "第二段", "第三段"]},
                {"url": "https://example.org/two", "title": "第二页", "snippets": ["简短内容"]},
                {"url": "https://example.org/three", "title": "第三页", "snippets": ["不应返回"]},
            ]},
            "sources": {
                "https://example.org/one": {"age": ["date", "2026-09-27", "today", "2026-09-27T00:00:00Z"]},
            },
        })

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http_client:
        result = await BraveSearchClient(http_client).search(
            "最新故事研究", maximum_number_of_urls=2, freshness="pw", search_lang="zh"
        )

    assert len(requests) == 1
    request = requests[0]
    assert request.method == "GET"
    assert request.url.host == "api.search.brave.com"
    assert request.url.path == "/res/v1/llm/context"
    assert request.headers["X-Subscription-Token"] == "fake-test-key"
    assert request.url.params["q"] == "最新故事研究"
    assert request.url.params["count"] == "5"
    assert request.url.params["maximum_number_of_urls"] == "2"
    assert request.url.params["maximum_number_of_tokens"] == "2048"
    assert request.url.params["safesearch"] == "moderate"
    assert request.url.params["freshness"] == "pw"
    assert request.url.params["search_lang"] == "zh-hans"
    assert result["status"] == "ok" and result["result_count"] == 2
    assert result["truncated"] is True
    assert result["results"][0] == {
        "url": "https://example.org/one",
        "title": "第一页",
        "snippets": ["甲" * 500, "第二段"],
        "published_at": "2026-09-27",
        "relative_age": "today",
    }
    assert "fake-test-key" not in str(result)


@pytest.mark.asyncio
async def test_missing_key_and_empty_results_are_distinct(monkeypatch):
    monkeypatch.delenv("BRAVE_SEARCH_API_KEY", raising=False)
    called = False

    def respond(request):
        nonlocal called
        called = True
        return httpx.Response(200, json={"grounding": {"generic": []}, "sources": {}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http_client:
        search = BraveSearchClient(http_client)
        with pytest.raises(BraveSearchError) as error:
            await search.search("query")
        assert error.value.code == "not_configured"
        assert called is False
        monkeypatch.setenv("BRAVE_SEARCH_API_KEY", "fake-test-key")
        assert await search.search("query") == {
            "status": "no_results", "results": [], "result_count": 0, "truncated": False
        }


@pytest.mark.asyncio
@pytest.mark.parametrize("http_status,expected_code", [
    (401, "unauthorized"), (403, "forbidden"),
    (429, "rate_limited"), (422, "invalid_parameter"), (500, "upstream_error"),
])
async def test_provider_errors_do_not_echo_body_or_key(monkeypatch, http_status, expected_code):
    monkeypatch.setenv("BRAVE_SEARCH_API_KEY", "fake-test-key")
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(http_status, text="provider response containing fake-test-key")

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http_client:
        with pytest.raises(BraveSearchError) as error:
            await BraveSearchClient(http_client).search("query")
    assert len(requests) == 1  # Paid requests are never retried automatically.
    assert error.value.code == expected_code
    assert "fake-test-key" not in str(error.value)
    assert "provider response" not in str(error.value)


@pytest.mark.asyncio
async def test_timeout_is_reported_without_transport_details(monkeypatch):
    monkeypatch.setenv("BRAVE_SEARCH_API_KEY", "fake-test-key")

    def respond(request):
        raise httpx.ReadTimeout("fake-test-key", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http_client:
        with pytest.raises(BraveSearchError) as error:
            await BraveSearchClient(http_client).search("query")
    assert error.value.code == "timeout"
    assert "fake-test-key" not in str(error.value)


@pytest.mark.asyncio
async def test_invalid_input_is_rejected_before_request(monkeypatch):
    monkeypatch.setenv("BRAVE_SEARCH_API_KEY", "fake-test-key")
    called = False

    def respond(request):
        nonlocal called
        called = True
        return httpx.Response(200, json={"grounding": {"generic": []}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http_client:
        search = BraveSearchClient(http_client)
        for query, kwargs, code in [
            ("", {}, "invalid_query"),
            ("a" * 601, {}, "invalid_query"),
            (" ".join(["word"] * 76), {}, "invalid_query"),
            ("ok", {"maximum_number_of_urls": 50}, "invalid_parameter"),
            ("ok", {"freshness": "not-a-date"}, "invalid_parameter"),
        ]:
            with pytest.raises(BraveSearchError) as error:
                await search.search(query, **kwargs)
            assert error.value.code == code
    assert called is False


@pytest.mark.asyncio
async def test_malformed_response_and_unsafe_urls_are_not_passed_through(monkeypatch):
    monkeypatch.setenv("BRAVE_SEARCH_API_KEY", "fake-test-key")

    def malformed(request):
        return httpx.Response(200, json={"unexpected": "shape"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(malformed)) as http_client:
        with pytest.raises(BraveSearchError) as error:
            await BraveSearchClient(http_client).search("query")
    assert error.value.code == "invalid_response"

    def unsafe(request):
        return httpx.Response(200, json={"grounding": {"generic": [
            {"url": "javascript:alert(1)", "title": "bad", "snippets": ["bad"]},
            {"url": "https://valid.example/page", "title": "good", "snippets": ["useful"]},
        ]}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(unsafe)) as http_client:
        result = await BraveSearchClient(http_client).search("query")
    assert [item["url"] for item in result["results"]] == ["https://valid.example/page"]


@pytest.mark.asyncio
async def test_chinese_query_defaults_to_chinese_and_result_stays_within_session_budget(monkeypatch):
    monkeypatch.setenv("BRAVE_SEARCH_API_KEY", "fake-test-key")
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json={"grounding": {"generic": [
            {"url": f"https://example.org/{index}/" + "a" * 950,
             "title": "标题" * 200,
             "snippets": ["长段落" * 400, "补充" * 400]}
            for index in range(5)
        ]}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http_client:
        result = await BraveSearchClient(http_client).search("中文事件资料")

    assert requests[0].url.params["search_lang"] == "zh-hans"
    assert len(json.dumps(result, ensure_ascii=False)) <= 8_000
    assert result["truncated"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("given,expected", [("zh", "zh-hans"), ("zh-CN", "zh-hans"),
                                           ("zh-TW", "zh-hant"), ("ja", "jp")])
async def test_common_language_aliases_use_brave_codes(monkeypatch, given, expected):
    monkeypatch.setenv("BRAVE_SEARCH_API_KEY", "fake-test-key")
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json={"grounding": {"generic": []}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http_client:
        await BraveSearchClient(http_client).search("query", search_lang=given)
    assert requests[0].url.params["search_lang"] == expected
