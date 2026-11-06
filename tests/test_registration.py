from __future__ import annotations

from hermes_web_crawl4ai import register


class FakeContext:
    def __init__(self) -> None:
        self.providers = []
        self.tools = []

    def get_config(self, key, default=None):
        return default

    def register_web_search_provider(self, provider) -> None:
        self.providers.append(provider)

    def register_tool(self, **kwargs) -> None:
        self.tools.append(kwargs)


def test_registers_provider_and_plugin_local_crawl_tool() -> None:
    context = FakeContext()

    register(context)

    assert [provider.name for provider in context.providers] == ["crawl4ai"]
    assert [tool["name"] for tool in context.tools] == ["crawl4ai_crawl"]
    tool = context.tools[0]
    assert tool["toolset"] == "crawl4ai"
    assert tool["is_async"] is True
    assert callable(tool["check_fn"])
