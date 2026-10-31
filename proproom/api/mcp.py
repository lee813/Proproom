"""MCP (Model Context Protocol) server over standard I/O (handover §8).

Exposes operations as MCP tools:
- default group: search, remote, fetch, link, credits, share, share_feedback, job, capabilities
- admin group (optional): db_status, verify, import_project, plugin_list, plugin_reload, plugin_new, plugin_test
"""
from __future__ import annotations

import json
import sys
from typing import Any

from .. import __version__
from ..core import bootstrap
from .registry import REGISTRY, OpContext


def _read_message() -> dict[str, Any] | None:
    line = sys.stdin.readline()
    if not line:
        return None
    line = line.strip()
    if not line:
        return _read_message()
    # Handle optional Content-Length header or raw JSON
    if line.startswith("Content-Length:"):
        length = int(line.split(":")[1].strip())
        sys.stdin.readline()  # blank line
        body = sys.stdin.read(length)
        return json.loads(body)
    return json.loads(line)


def _send_response(resp: dict[str, Any]) -> None:
    body = json.dumps(resp, ensure_ascii=False)
    sys.stdout.write(body + "\n")
    sys.stdout.flush()


def run_mcp_server(include_admin: bool = False) -> None:
    booted = bootstrap.boot()
    ctx = OpContext(booted, actor="mcp")

    while True:
        try:
            msg = _read_message()
            if msg is None:
                break
        except Exception:
            break

        msg_id = msg.get("id")
        method = msg.get("method")
        params = msg.get("params", {})

        if method == "initialize":
            _send_response({
                "jsonrpc": "2.0",
                "id": msg_id,
                "result": {
                    "protocolVersion": "2024-11-05",
                    "serverInfo": {"name": "proproom", "version": __version__},
                    "capabilities": {"tools": {}},
                },
            })
        elif method == "notifications/initialized":
            pass
        elif method == "tools/list":
            tools = []
            for op in REGISTRY.values():
                if op.mcp == "default" or (include_admin and op.mcp in ("default", "admin")):
                    tools.append({
                        "name": op.name,
                        "description": op.summary,
                        "inputSchema": op.schema()["input"],
                    })
            _send_response({
                "jsonrpc": "2.0",
                "id": msg_id,
                "result": {"tools": tools},
            })
        elif method == "tools/call":
            tool_name = params.get("name")
            tool_args = params.get("arguments", {})
            op = REGISTRY.get(tool_name)
            if not op:
                _send_response({
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "error": {"code": -32601, "message": f"Tool '{tool_name}' not found"},
                })
                continue
            try:
                result = op(ctx, tool_args)
                text_content = json.dumps(result, ensure_ascii=False, indent=2)
                _send_response({
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "result": {
                        "content": [{"type": "text", "text": text_content}],
                        "isError": False,
                    },
                })
            except Exception as e:
                _send_response({
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "result": {
                        "content": [{"type": "text", "text": f"Error: {e}"}],
                        "isError": True,
                    },
                })
        elif msg_id is not None:
            _send_response({
                "jsonrpc": "2.0",
                "id": msg_id,
                "error": {"code": -32601, "message": f"Method '{method}' not found"},
            })
