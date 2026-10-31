"""Operation registry (handover §8): one list of typed operations; CLI, HTTP and MCP are generated from it.

Each operation declares input/output JSON Schema, whether it has side effects (=> accepts idempotency_key),
and which MCP tool group exposes it. Unimplemented operations are declared so `capabilities` shows the full
contract; calling them returns the standard `not_implemented` error.
"""
from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from importlib import resources
from typing import Any, Callable, Literal

from .. import __version__
from ..core import db, hardware
from ..core.bootstrap import Booted
from ..sdk import SDK_VERSION
from ..sdk.errors import not_implemented

Handler = Callable[["OpContext", dict[str, Any]], dict[str, Any]]


@dataclass
class OpContext:
    booted: Booted
    actor: str = "cli"


@dataclass
class Operation:
    name: str
    summary: str
    input: dict[str, Any] = field(default_factory=lambda: obj())
    output: dict[str, Any] = field(default_factory=lambda: obj())
    side_effects: bool = False
    mcp: Literal["default", "admin", None] = None
    handler: Handler | None = None

    @property
    def implemented(self) -> bool:
        return self.handler is not None

    def schema(self) -> dict[str, Any]:
        inp = self.input
        if self.side_effects:
            inp = {**inp, "properties": {**inp.get("properties", {}), "idempotency_key": {"type": "string"}}}
        return {"name": self.name, "summary": self.summary, "input": inp, "output": self.output,
                "side_effects": self.side_effects, "mcp": self.mcp, "implemented": self.implemented}

    def __call__(self, ctx: OpContext, args: dict[str, Any]) -> dict[str, Any]:
        if self.handler is None:
            raise not_implemented(self.name)
        return self.handler(ctx, args)


def obj(required: list[str] | None = None, **props: Any) -> dict[str, Any]:
    s: dict[str, Any] = {"type": "object", "properties": props, "additionalProperties": False}
    if required:
        s["required"] = required
    return s


S = {"type": "string"}
I = {"type": "integer"}
B = {"type": "boolean"}
SL = {"type": "array", "items": S}
IL = {"type": "array", "items": I}
JOB = obj(job_id=S, state=S, progress={"type": "object"})


# ───────────────────────── handlers ─────────────────────────
def _builtin_plugins() -> list[dict[str, Any]]:
    out = []
    root = resources.files("proproom").joinpath("plugins", "builtin")
    for d in sorted(root.iterdir(), key=lambda p: p.name):
        manifest = d.joinpath("plugin.toml")
        if manifest.is_file():
            m = tomllib.loads(manifest.read_text())["plugin"]
            out.append({"name": m["name"], "version": m["version"], "types": m["types"], "origin": "builtin",
                        "status": m.get("status", "stub")})
    return out


def _capabilities(ctx: OpContext, args: dict[str, Any]) -> dict[str, Any]:
    b = ctx.booted
    conn = db.connect(b.home.db, readonly=True)
    try:
        inst = {r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM instance")}
        spaces = [dict(r) for r in conn.execute(
            "SELECT id, model, revision, dim, metric, vec_table, active FROM embedding_space")]
        vec = conn.execute("SELECT vec_version()").fetchone()[0]
        sqlite_version = conn.execute("SELECT sqlite_version()").fetchone()[0]
    finally:
        conn.close()
    public_url = b.config["server"]["public_url"] or None
    warnings = [] if public_url else ["public_url_unknown"]
    return {
        "version": __version__, "sdk": SDK_VERSION,
        "instance_id": inst.get("instance_id"), "schema_version": int(inst.get("schema_version", 0)),
        "policy_version": b.policy_version,
        "home": str(b.home.root), "home_source": b.home.source,
        "public_url": public_url, "warnings": warnings,
        "sqlite": sqlite_version, "sqlite_vec": vec,
        "hardware": hardware.detect(),
        "models": {"embedding": {"configured": b.config["embedding"]["model"], "loaded": False, "spaces": spaces}},
        "sources": [],   # populated once source plugins load
        "plugins": _builtin_plugins(),
        "operations": [op.schema() for op in REGISTRY.values()] if args.get("schemas", True) else sorted(REGISTRY),
    }


def _db_status(ctx: OpContext, args: dict[str, Any]) -> dict[str, Any]:
    conn = db.connect(ctx.booted.home.db, readonly=True)
    try:
        # skip FTS5 / vec0 shadow tables (fts_latin_data, vec_s1_chunks, …)
        virtual = [r[0] for r in conn.execute("SELECT name FROM sqlite_schema WHERE sql LIKE 'CREATE VIRTUAL%'")]
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_schema WHERE type IN ('table','view') AND name NOT LIKE 'sqlite_%' ORDER BY name")
            if not any(r[0].startswith(v + "_") for v in virtual)]
        counts = {t: conn.execute(f'SELECT count(*) FROM "{t}"').fetchone()[0]
                  for t in ("asset", "blob", "representation", "provenance", "usage", "policy")}
        return {"path": str(ctx.booted.home.db), "user_version": db.user_version(conn),
                "latest": db.latest_version(), "journal_mode": conn.execute("PRAGMA journal_mode").fetchone()[0],
                "integrity": conn.execute("PRAGMA quick_check").fetchone()[0], "counts": counts,
                "tables": tables}
    finally:
        conn.close()


# ───────────────────────── registry ─────────────────────────
_OPS = [
    Operation("capabilities", "Sources, modalities, model state, plugins and every operation's schema.",
              input=obj(schemas=B), mcp="default", handler=_capabilities),
    Operation("db_status", "Schema version, integrity check and row counts.", mcp="admin", handler=_db_status),
    Operation("search", "Hybrid local search (vector + FTS, RRF). Returns a selection_id and optional sheet.",
              input=obj(["query"], query=S, kind=SL, license=S, anchor=S, entity=S, fits=S, min_edge=I,
                        exclude_used_in=S, include_quotation=B, sheet=B, explain=B, k=I, cursor=S),
              output=obj(selection_id=S, items={"type": "array"}, score_type=S, next_cursor=S, sheet=S),
              mcp="default"),
    Operation("remote", "Search a remote source; candidates only, nothing is stored.",
              input=obj(["source", "query"], source=S, query=S, kind=SL, sheet=B, cursor=S),
              output=obj(selection_id=S, items={"type": "array"}, next_cursor=S), mcp="default"),
    Operation("fetch", "Resolve -> rights check -> download -> ingest one remote candidate.",
              input=obj(selection_id=S, pick=IL, source=S, external_id=S), side_effects=True, mcp="default"),
    Operation("fetch_many", "Batch fetch; per-item success/failure.", input=obj(items={"type": "array"}),
              side_effects=True),
    Operation("add", "Ingest local files (own work / manual).", input=obj(["paths"], paths=SL, basis=S),
              side_effects=True),
    Operation("link", "Deliver picks to a project; --plan previews, then execute by plan id.",
              input=obj(selection_id=S, pick=IL, assets=SL, project=S, plan=B, plan_id=S, link_mode=S, cut=S),
              side_effects=True, mcp="default"),
    Operation("link_many", "Batch link; per-item success/failure.", input=obj(items={"type": "array"}),
              side_effects=True),
    Operation("credits", "Attribution for a project, grouped by basis, from delivery-time snapshots.",
              input=obj(["project"], project=S, exporter=S), mcp="default"),
    Operation("share", "Create a share link for assets or a selection.",
              input=obj(assets=SL, selection_id=S, title=S, note=S, expires=S),
              output=obj(token=S, url=S, warnings=SL), side_effects=True, mcp="default"),
    Operation("share_feedback", "Read want/reject verdicts and comments from a share.",
              input=obj(["share"], share=S), mcp="default"),
    Operation("share_revoke", "Revoke a share link.", input=obj(["share"], share=S), side_effects=True),
    Operation("job", "get / cancel / pause / resume a long-running job.",
              input=obj(["action", "job_id"], action={"enum": ["get", "cancel", "pause", "resume"]}, job_id=S),
              output=JOB, mcp="default"),
    Operation("pull", "Bulk pull from a pull.yaml as a background job with a byte budget.",
              input=obj(["spec"], spec=S, budget=S), output=JOB, side_effects=True),
    Operation("verify", "Recompute blob sha256 and report mismatches and the projects sharing them.",
              output=JOB, mcp="admin"),
    Operation("import_project", "Import a project's existing assets.json and back-fill usage.",
              input=obj(["project"], project=S), output=JOB, side_effects=True, mcp="admin"),
    Operation("plugin_list", "Loaded plugins with version and hash.", mcp="admin"),
    Operation("plugin_reload", "Reload directory plugins; running jobs keep the old instance.",
              side_effects=True, mcp="admin"),
    Operation("plugin_new", "Scaffold a plugin with tests.", input=obj(["name", "types"], name=S, types=SL),
              mcp="admin"),
    Operation("plugin_test", "Offline contract test with recorded HTTP fixtures.", input=obj(["name"], name=S),
              mcp="admin"),
]

REGISTRY: dict[str, Operation] = {op.name: op for op in _OPS}
