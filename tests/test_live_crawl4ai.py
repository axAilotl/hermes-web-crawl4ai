"""Opt-in contract tests against a real Crawl4AI REST service."""

from __future__ import annotations

import json
import os

import pytest

from hermes_web_crawl4ai.provider import Crawl4AIWebSearchProvider

pytestmark = pytest.mark.skipif(
    not os.getenv("CRAWL4AI_TEST_URL"),
    reason="set CRAWL4AI_TEST_URL to run real-service tests",
)


@pytest.fixture
def provider():
    return Crawl4AIWebSearchProvider({"api_url": os.environ["CRAWL4AI_TEST_URL"]}.get)


@pytest.mark.asyncio
async def test_live_markdown_and_html_extract(provider) -> None:

    markdown = await provider.extract(["https://example.com"], format="markdown")
    html = await provider.extract(["https://example.com"], format="html")

    assert markdown[0].get("error") is None
    assert markdown[0]["content"]
    assert html[0].get("error") is None
    assert "Example Domain" in html[0]["content"]


@pytest.mark.asyncio
async def test_live_bounded_same_site_crawl(provider) -> None:
    result = json.loads(
        await provider.crawl4ai_crawl_tool(
            {"url": "https://example.com", "depth": "basic", "max_pages": 2}
        )
    )

    assert "error" not in result
    assert len(result["results"]) <= 2
    assert all(item["url"].startswith("https://example.com") for item in result["results"])
