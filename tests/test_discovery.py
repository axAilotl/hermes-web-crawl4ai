from __future__ import annotations

from pathlib import Path

import pytest
import yaml


@pytest.mark.parametrize("settings_key", ["settings", "config"])
def test_real_hermes_directory_discovery(tmp_path, monkeypatch, settings_key) -> None:
    """Exercise the real manifest scan, namespaced import, and registrations."""
    from agent import web_search_registry
    from hermes_cli.plugins import PluginManager
    from tools.registry import registry

    repo_root = Path(__file__).resolve().parents[1]
    hermes_home = tmp_path / "hermes"
    plugins_dir = hermes_home / "plugins"
    plugins_dir.mkdir(parents=True)
    (plugins_dir / "hermes-web-crawl4ai").symlink_to(repo_root, target_is_directory=True)
    (hermes_home / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "plugins": {
                    "enabled": ["hermes-web-crawl4ai"],
                    "entries": {
                        "hermes-web-crawl4ai": {settings_key: {"api_url": "http://localhost:11235"}}
                    },
                },
                "web": {"extract_backend": "crawl4ai"},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))

    manager = PluginManager()
    try:
        manager.discover_and_load()

        loaded = manager._plugins["hermes-web-crawl4ai"]
        assert loaded.enabled is True
        assert loaded.error is None
        assert loaded.tools_registered == ["crawl4ai_crawl"]
        provider = web_search_registry.get_provider("crawl4ai")
        assert provider.is_available()
        assert provider._api_url() == "http://localhost:11235"
        assert web_search_registry.get_active_extract_provider() is provider
        assert registry.get_entry("crawl4ai_crawl") is not None
    finally:
        manager.unload()
