"""Tests for plugins: Commons, Wikidata, and directory plugins."""
import importlib.util
from pathlib import Path
import pytest
from proproom.core import bootstrap
from proproom.core.paths import Home
from proproom.core.plugins import PluginManager
from proproom.sdk.context import PolicyContext


def test_plugin_manager_loads_builtins(tmp_path):
    home = Home(tmp_path / "lib", tmp_path, "env", {})
    bootstrap.boot(home)

    pm = PluginManager(home, "inst-test")
    pm.load_all()

    assert "commons" in pm.plugins
    assert "wikidata" in pm.plugins
    assert "assets_json" in pm.plugins
    assert "credits_basic" in pm.plugins
    assert "scenecut" in pm.plugins


def test_directory_plugin_loading(tmp_path):
    home = Home(tmp_path / "lib", tmp_path, "env", {})
    bootstrap.boot(home)

    p_dir = home.dir("plugins") / "myplugin"
    p_dir.mkdir(parents=True)
    (p_dir / "plugin.toml").write_text("""[plugin]
name = "myplugin"
version = "0.0.1"
sdk = ">=0.1,<2"
types = ["policy"]
""", encoding="utf-8")

    (p_dir / "__init__.py").write_text("""from proproom.sdk.types import PolicyDecision
class Plugin:
    def decide(self, ctx, asset):
        if asset.get("kind") == "map":
            return PolicyDecision(decision="deny", code="no_map", detail="no maps")
        return PolicyDecision(decision="allow")
""", encoding="utf-8")

    pm = PluginManager(home, "inst-test")
    pm.load_all()

    assert "myplugin" in pm.plugins
    inst = pm.plugins["myplugin"].instance
    ctx = PolicyContext(plugin="myplugin", log=None, config={}, policy_version=1)
    dec = inst.decide(ctx, {"kind": "map"})
    assert dec.decision == "deny"
