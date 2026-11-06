"""Hermes registration entry point for the Crawl4AI plugin."""

from __future__ import annotations

from .provider import Crawl4AIWebSearchProvider
from .schemas import CRAWL4AI_CRAWL_SCHEMA


def register(ctx) -> None:
    """Register the extract provider and opt-in plugin-local crawl tool."""
    provider = Crawl4AIWebSearchProvider(ctx.get_config)
    ctx.register_web_search_provider(provider)
    ctx.register_tool(
        name="crawl4ai_crawl",
        toolset="crawl4ai",
        schema=CRAWL4AI_CRAWL_SCHEMA,
        handler=provider.crawl4ai_crawl_tool,
        check_fn=provider.is_available,
        is_async=True,
        description="Bounded deep crawling through a self-hosted Crawl4AI service.",
        emoji="🕸️",
    )


__all__ = ["register"]
