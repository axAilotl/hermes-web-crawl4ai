"""Hermes registration entry point for the Crawl4AI plugin."""

from __future__ import annotations

from .provider import Crawl4AIWebSearchProvider, _api_url, crawl4ai_crawl_tool
from .schemas import CRAWL4AI_CRAWL_SCHEMA


def register(ctx) -> None:
    """Register the extract provider and opt-in plugin-local crawl tool."""
    ctx.register_web_search_provider(Crawl4AIWebSearchProvider())
    ctx.register_tool(
        name="crawl4ai_crawl",
        toolset="crawl4ai",
        schema=CRAWL4AI_CRAWL_SCHEMA,
        handler=crawl4ai_crawl_tool,
        check_fn=lambda: bool(_api_url()),
        is_async=True,
        description="Bounded deep crawling through a self-hosted Crawl4AI service.",
        emoji="🕸️",
    )


__all__ = ["register"]
