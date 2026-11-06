"""Run after installing the built wheel, from outside the source import path."""

import importlib
import os
import tempfile
from pathlib import Path

import yaml
from agent import web_search_registry
from hermes_cli.plugins import PluginManager

PLUGIN = "hermes-web-crawl4ai"
module = importlib.import_module("hermes_web_crawl4ai")
assert not Path(module.__file__).resolve().is_relative_to(Path(__file__).resolve().parents[1])
with tempfile.TemporaryDirectory() as temporary:
    os.environ["HERMES_HOME"] = temporary
    (Path(temporary) / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "plugins": {
                    "enabled": [PLUGIN],
                    "entries": {PLUGIN: {"settings": {"api_url": "http://localhost:12345"}}},
                },
                "web": {"extract_backend": "crawl4ai"},
            }
        ),
        encoding="utf-8",
    )
    manager = PluginManager()
    try:
        manager.discover_and_load()
        loaded = manager._plugins[PLUGIN]
        assert loaded.enabled and not loaded.error, loaded.error
        provider = web_search_registry.get_active_extract_provider()
        assert provider.name == "crawl4ai" and provider.is_available()
        print(f"Installed wheel discovered and selected: {PLUGIN}")
    finally:
        manager.unload()
