from __future__ import annotations

import shutil
from pathlib import Path

import pytest
import yaml


@pytest.mark.parametrize("settings_key", ["settings", "config"])
@pytest.mark.parametrize("existing_first", [True, False])
def test_real_hermes_directory_discovery(
    tmp_path, monkeypatch, settings_key, existing_first
) -> None:
    """Exercise the real manifest scan, namespaced import, and registrations."""
    from agent import web_search_registry
    from hermes_cli.plugins import PluginManager
    from tools.registry import registry

    repo_root = Path(__file__).resolve().parents[1]
    hermes_home = tmp_path / "hermes"
    plugins_dir = hermes_home / "plugins"
    plugins_dir.mkdir(parents=True)
    shutil.copytree(
        repo_root,
        plugins_dir / "hermes-web-crawl4ai",
        ignore=shutil.ignore_patterns(
            ".git", ".venv", "__pycache__", ".pytest_cache", ".ruff_cache"
        ),
    )
    (hermes_home / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "plugins": {
                    "enabled": ["hermes-web-crawl4ai"],
                    "entries": {
                        "hermes-web-crawl4ai": {settings_key: {"api_url": "http://localhost:11235"}}
                    },
                },
                "web": {"extract_backend": "crawl4ai-bounded"},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))

    from hermes_web_crawl4ai.provider import Crawl4AIWebSearchProvider

    class ExistingCatalogProvider(Crawl4AIWebSearchProvider):
        @property
        def name(self):
            return "crawl4ai"

    existing = ExistingCatalogProvider({"api_url": "http://localhost:9999"}.get)
    previous = web_search_registry.snapshot_registration(existing.name)
    if existing_first:
        web_search_registry.register_provider(existing)

    manager = PluginManager()
    try:
        manager.discover_and_load()
        if not existing_first:
            web_search_registry.register_provider(existing)

        loaded = manager._plugins["hermes-web-crawl4ai"]
        assert loaded.enabled is True
        assert loaded.error is None
        assert loaded.tools_registered == ["crawl4ai_crawl"]
        provider = web_search_registry.get_provider("crawl4ai-bounded")
        assert web_search_registry.get_provider("crawl4ai") is existing
        assert provider.is_available()
        assert provider._api_url() == "http://localhost:11235"
        assert web_search_registry.get_active_extract_provider() is provider
        assert registry.get_entry("crawl4ai_crawl") is not None
    finally:
        manager.unload()
        web_search_registry.restore_registration(existing.name, existing, previous)
