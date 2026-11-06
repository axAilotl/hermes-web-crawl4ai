from __future__ import annotations

import json
from unittest.mock import AsyncMock

import pytest

from hermes_web_crawl4ai import provider as provider_module
from hermes_web_crawl4ai.provider import (
    Crawl4AIWebSearchProvider,
    _normalize_document,
    _normalize_documents,
)


def test_normalize_document_prefers_fit_markdown() -> None:
    result = _normalize_document(
        {
            "url": "https://example.com/docs",
            "metadata": {"title": "Docs"},
            "markdown": {
                "fit_markdown": "Fit markdown",
                "raw_markdown": "Raw markdown",
            },
            "success": True,
        }
    )

    assert result["url"] == "https://example.com/docs"
    assert result["title"] == "Docs"
    assert result["content"] == "Fit markdown"
    assert result["raw_content"] == "Raw markdown"


@pytest.mark.parametrize("key", ["results", "result"])
def test_normalize_documents_accepts_known_list_envelopes(key: str) -> None:
    documents = _normalize_documents(
        {key: [{"url": "https://example.com", "markdown": "hello", "success": True}]}
    )
    assert documents == [
        {
            "url": "https://example.com",
            "title": "",
            "content": "hello",
            "raw_content": "hello",
            "metadata": {},
        }
    ]


def test_provider_is_extract_only(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = Crawl4AIWebSearchProvider({"api_url": "http://localhost:11235"}.get)

    assert provider.name == "crawl4ai"
    assert provider.supports_search() is False
    assert provider.supports_extract() is True
    assert provider.is_available() is True


def test_settings_are_read_dynamically_and_instances_are_independent(monkeypatch):
    monkeypatch.setattr(provider_module, "get_provider_env", lambda key: "")
    settings = {"api_url": "http://first.example:11235/"}
    first = Crawl4AIWebSearchProvider(settings.get)
    second = Crawl4AIWebSearchProvider({"api_url": "http://second.example:11235"}.get)
    assert first._api_url() == "http://first.example:11235"
    settings["api_url"] = "http://changed.example:11235"
    assert first._api_url() == "http://changed.example:11235"
    assert second._api_url() == "http://second.example:11235"


def test_normalize_document_prefers_redirect_destination_for_policy_checks() -> None:
    result = _normalize_document(
        {
            "url": "https://safe.example/start",
            "redirected_url": "http://127.0.0.1/private",
            "markdown": "content",
            "success": True,
        }
    )

    assert result["url"] == "http://127.0.0.1/private"


@pytest.mark.asyncio
async def test_extract_preserves_order_and_returns_per_url_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = Crawl4AIWebSearchProvider()
    monkeypatch.setattr(provider_module, "_target_error", AsyncMock(return_value=None))
    monkeypatch.setattr(
        provider,
        "_fetch_document",
        AsyncMock(
            side_effect=[
                {
                    "url": "https://one.example",
                    "title": "One",
                    "content": "first",
                    "raw_content": "first",
                    "metadata": {},
                },
                RuntimeError("boom"),
            ]
        ),
    )

    results = await provider.extract(["https://one.example", "https://two.example"])

    assert [item["url"] for item in results] == [
        "https://one.example",
        "https://two.example",
    ]
    assert results[0]["content"] == "first"
    assert "boom" in results[1]["error"]


@pytest.mark.asyncio
async def test_extract_contains_policy_check_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = Crawl4AIWebSearchProvider()
    monkeypatch.setattr(
        provider_module,
        "_target_error",
        AsyncMock(side_effect=RuntimeError("DNS failed")),
    )

    results = await provider.extract(["https://example.com"])

    assert results[0]["url"] == "https://example.com"
    assert "DNS failed" in results[0]["error"]


@pytest.mark.asyncio
async def test_crawl_tool_rejects_unsafe_seed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        provider_module,
        "_target_error",
        AsyncMock(return_value="Blocked: URL targets a private or internal network address"),
    )
    provider = Crawl4AIWebSearchProvider()
    post = AsyncMock()
    monkeypatch.setattr(provider, "_post_json", post)

    result = json.loads(await provider.crawl4ai_crawl_tool({"url": "http://127.0.0.1"}))

    assert result["success"] is False
    assert "private or internal" in result["error"]
    post.assert_not_awaited()


@pytest.mark.asyncio
async def test_crawl_tool_clamps_depth_pages_and_filters_results(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(provider_module, "_target_error", AsyncMock(return_value=None))
    provider = Crawl4AIWebSearchProvider()
    post = AsyncMock(
        return_value={
            "results": [
                {
                    "url": f"https://example.com/{index}",
                    "metadata": {"title": str(index)},
                    "markdown": f"page {index}",
                    "success": True,
                }
                for index in range(25)
            ]
        }
    )
    monkeypatch.setattr(provider, "_post_json", post)

    result = json.loads(
        await provider.crawl4ai_crawl_tool(
            {"url": "https://example.com/docs", "depth": "advanced", "max_pages": 999}
        )
    )

    payload = post.await_args.args[1]
    assert payload["browser_config"] == {
        "type": "BrowserConfig",
        "params": {"headless": True},
    }
    assert payload["crawler_config"]["type"] == "CrawlerRunConfig"
    strategy = payload["crawler_config"]["params"]["deep_crawl_strategy"]
    assert strategy["params"] == {
        "max_depth": 2,
        "max_pages": 20,
        "include_external": False,
    }
    assert len(result["results"]) == 20


@pytest.mark.asyncio
async def test_crawl_tool_rejects_cross_site_results(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(provider_module, "_target_error", AsyncMock(return_value=None))
    provider = Crawl4AIWebSearchProvider()
    monkeypatch.setattr(
        provider,
        "_post_json",
        AsyncMock(
            return_value={
                "results": [
                    {
                        "url": "https://unrelated.example/page",
                        "markdown": "should not be returned",
                        "success": True,
                    }
                ]
            }
        ),
    )

    result = json.loads(await provider.crawl4ai_crawl_tool({"url": "https://example.com"}))

    assert result["results"][0]["content"] == ""
    assert "left the seed site" in result["results"][0]["error"]
