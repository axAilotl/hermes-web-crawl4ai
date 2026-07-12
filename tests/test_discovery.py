from __future__ import annotations

from pathlib import Path

import yaml


def test_real_hermes_directory_discovery(tmp_path, monkeypatch) -> None:
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
                        "hermes-web-crawl4ai": {
                            "config": {"api_url": "http://localhost:11235"}
                        }
                    },
                },
                "web": {"extract_backend": "crawl4ai"},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))

    providers_before = dict(web_search_registry._providers)
    try:
        manager = PluginManager()
        manager.discover_and_load()

        loaded = manager._plugins["hermes-web-crawl4ai"]
        assert loaded.enabled is True
        assert loaded.error is None
        assert loaded.tools_registered == ["crawl4ai_crawl"]
        assert web_search_registry.get_provider("crawl4ai") is not None
        assert registry.get_entry("crawl4ai_crawl") is not None
    finally:
        registry.deregister("crawl4ai_crawl")
        web_search_registry._providers.clear()
        web_search_registry._providers.update(providers_before)
