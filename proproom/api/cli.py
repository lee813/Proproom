"""`prop` — CLI front-end generated from the operation registry. Output is JSON on stdout; errors are
the standard error object on stderr with exit code 2.

    prop init                      create / migrate the library and print where it is
    prop serve                     run propd (HTTP + share pages) on :8765
    prop capabilities              same as the `capabilities` operation
    prop db status | migrate
    prop <operation> [--json '{...}'] [--key value …]
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


def _parse_kv(rest: list[str]) -> dict[str, Any]:
    args: dict[str, Any] = {}
    it = iter(rest)
    for tok in it:
        if not tok.startswith("--"):
            raise ProproomError("bad_argument", f"unexpected argument {tok!r}", hint="use --key value or --json")
        key = tok[2:].replace("-", "_")
        if key == "json":
            args.update(json.loads(next(it)))
            continue
        val = next(it, "true")
        try:
            args[key] = json.loads(val)
        except json.JSONDecodeError:
            args[key] = val
    return args


def main(argv: list[str] | None = None) -> None:
    argv = sys.argv[1:] if argv is None else argv
    p = argparse.ArgumentParser(prog="prop", description="Proproom — the prop room for your agents.")
    p.add_argument("command", help="init | serve | db | " + " | ".join(REGISTRY))
    p.add_argument("rest", nargs=argparse.REMAINDER)
    ns = p.parse_args(argv)

    try:
        if ns.command == "serve":
            from .http import serve  # noqa: PLC0415 - keep CLI start-up light
            serve(ns.rest)
            return
        booted = bootstrap.boot()
        ctx = OpContext(booted)
        if ns.command == "init":
            _emit({"home": str(booted.home.root), "home_source": booted.home.source, "created": booted.created,
                   "migrations_applied": booted.applied, "policy_version": booted.policy_version})
        elif ns.command == "db":
            sub = ns.rest[0] if ns.rest else "status"
            if sub == "migrate":
                _emit({"migrations_applied": booted.applied})
            elif sub == "status":
                _emit(REGISTRY["db_status"](ctx, {}))
            else:
                raise ProproomError("bad_argument", f"unknown db subcommand {sub!r}", hint="status | migrate")
        elif ns.command in REGISTRY:
            _emit(REGISTRY[ns.command](ctx, _parse_kv(ns.rest)))
        else:
            raise ProproomError("unknown_operation", f"no operation named {ns.command!r}",
                                hint="run `prop capabilities` for the list")
    except ProproomError as e:
        json.dump({"error": e.to_dict()}, sys.stderr, ensure_ascii=False, indent=2)
        sys.stderr.write("\n")
        sys.exit(2)
