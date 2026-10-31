"""Tests for MCP server protocol handling."""
import json
import io
import pytest
from proproom.api import mcp
from proproom.core import bootstrap
from proproom.core.paths import Home


def test_mcp_protocol(tmp_path, monkeypatch):
    home = Home(tmp_path / "lib", tmp_path, "env", {})
    bootstrap.boot(home)
    monkeypatch.setenv("PROP_HOME", str(tmp_path / "lib"))

    # Test tools/list via MCP server function
    from proproom.api.registry import REGISTRY, OpContext
    booted = bootstrap.boot(home)
    ctx = OpContext(booted, actor="mcp")

    # Call capabilities
    cap_res = REGISTRY["capabilities"](ctx, {})
    assert "operations" in cap_res

    # Check default MCP operations
    default_tools = [op.name for op in REGISTRY.values() if op.mcp == "default"]
    assert "search" in default_tools
    assert "remote" in default_tools
    assert "fetch" in default_tools
    assert "link" in default_tools
    assert "credits" in default_tools
    assert "share" in default_tools
    assert "capabilities" in default_tools
