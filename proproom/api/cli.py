"""`prop` — CLI front-end generated from the operation registry. Output is JSON on stdout; errors are
the standard error object on stderr with exit code 2.

    prop init                      create / migrate the library and print where it is
    prop serve                     run propd (HTTP + share pages) on :8765
    prop mcp                       run MCP JSON-RPC server over stdio
    prop capabilities              same as the `capabilities` operation
    prop db status | migrate
    prop <operation> [args...] [--key value …]
"""
from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from ..core import bootstrap
from ..sdk.errors import ProproomError
from .registry import REGISTRY, OpContext


def _emit(obj: Any) -> None:
    json.dump(obj, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")


def _parse_args(cmd: str, rest: list[str]) -> dict[str, Any]:
    args: dict[str, Any] = {}
    positionals: list[str] = []

    it = iter(rest)
    for tok in it:
        if tok.startswith("--"):
            key = tok[2:].replace("-", "_")
            if key == "json":
                args.update(json.loads(next(it)))
                continue
            val = next(it, "true")
            try:
                args[key] = json.loads(val)
            except json.JSONDecodeError:
                args[key] = val
        else:
            positionals.append(tok)

    # Positional mapping based on command
    if cmd == "search" and positionals and "query" not in args:
        args["query"] = " ".join(positionals)
    elif cmd == "remote" and len(positionals) >= 2:
        args.setdefault("source", positionals[0])
        args.setdefault("query", " ".join(positionals[1:]))
    elif cmd in ("credits", "import_project") and positionals and "project" not in args:
        args["project"] = positionals[0]
    elif cmd in ("share_feedback", "share_revoke") and positionals and "share" not in args:
        args["share"] = positionals[0]
    elif cmd == "job" and len(positionals) >= 2:
        args.setdefault("action", positionals[0])
        args.setdefault("job_id", positionals[1])
    elif cmd == "add" and positionals and "paths" not in args:
        args["paths"] = positionals
    elif cmd == "pull" and positionals and "spec" not in args:
        args["spec"] = positionals[0]
    elif cmd in ("plugin_new", "plugin_test") and positionals and "name" not in args:
        args["name"] = positionals[0]

    return args


def main(argv: list[str] | None = None) -> None:
    argv = sys.argv[1:] if argv is None else argv
    p = argparse.ArgumentParser(prog="prop", description="Proproom — the prop room for your agents.")
    p.add_argument("command", help="init | serve | mcp | db | " + " | ".join(REGISTRY))
    p.add_argument("rest", nargs=argparse.REMAINDER)
    ns = p.parse_args(argv)

    cmd = ns.command.replace("-", "_")

    try:
        if cmd == "serve":
            from .http import serve  # noqa: PLC0415
            serve(ns.rest)
            return
        if cmd == "mcp":
            from .mcp import run_mcp_server  # noqa: PLC0415
            include_admin = "--admin" in ns.rest
            run_mcp_server(include_admin=include_admin)
            return

        booted = bootstrap.boot()
        ctx = OpContext(booted)

        if cmd == "init":
            _emit({
                "home": str(booted.home.root),
                "home_source": booted.home.source,
                "created": booted.created,
                "migrations_applied": booted.applied,
                "policy_version": booted.policy_version,
            })
        elif cmd == "db":
            sub = ns.rest[0] if ns.rest else "status"
            if sub == "migrate":
                _emit({"migrations_applied": booted.applied})
            elif sub == "status":
                _emit(REGISTRY["db_status"](ctx, {}))
            else:
                raise ProproomError("bad_argument", f"unknown db subcommand {sub!r}", hint="status | migrate")
        elif cmd in REGISTRY:
            _emit(REGISTRY[cmd](ctx, _parse_args(cmd, ns.rest)))
        else:
            raise ProproomError("unknown_operation", f"no operation named {ns.command!r}",
                                hint="run `prop capabilities` for the list")
    except ProproomError as e:
        json.dump({"error": e.to_dict()}, sys.stderr, ensure_ascii=False, indent=2)
        sys.stderr.write("\n")
        sys.exit(2)
