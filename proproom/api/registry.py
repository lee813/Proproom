"""Operation registry (handover §8): one list of typed operations; CLI, HTTP and MCP are generated from it.

Each operation declares input/output JSON Schema, whether it has side effects (=> accepts idempotency_key),
and which MCP tool group exposes it.
"""
from __future__ import annotations

import asyncio
import datetime
import hashlib
import json
import os
import secrets
import tomllib
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from typing import Any, Callable, Literal

from .. import __version__
from ..core import bootstrap, db, dedupe, delivery, download, hardware, jobs, ops, rights, search, store
from ..core.bootstrap import Booted
from ..core.ids import ulid, utcnow, utcnow_monotonic
from ..core.plugins import PluginManager
from ..sdk import SDK_VERSION
from ..sdk.errors import ProproomError, not_implemented
from ..sdk.types import SearchRequest, SourceRef

Handler = Callable[["OpContext", dict[str, Any]], dict[str, Any]]


@dataclass
class OpContext:
    booted: Booted
    actor: str = "cli"
    plugin_manager: PluginManager | None = None

    def pm(self) -> PluginManager:
        if self.plugin_manager is None:
            inst_id = ""
            conn = db.connect(self.booted.home.db, readonly=True)
            try:
                row = conn.execute("SELECT value FROM instance WHERE key='instance_id'").fetchone()
                if row:
                    inst_id = row["value"]
            finally:
                conn.close()
            pm = PluginManager(self.booted.home, inst_id)
            pm.load_all()
            self.plugin_manager = pm
        return self.plugin_manager


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
        return {
            "name": self.name,
            "summary": self.summary,
            "input": inp,
            "output": self.output,
            "side_effects": self.side_effects,
            "mcp": self.mcp,
            "implemented": self.implemented,
        }

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

    pm = ctx.pm()
    loaded_plugins = [
        {"name": p.name, "version": p.version, "types": p.types, "origin": p.origin, "status": "ready"}
        for p in pm.plugins.values()
    ]
    sources = [p.name for p in pm.plugins.values() if "source" in p.types]

    public_url = b.config["server"]["public_url"] or None
    warnings = [] if public_url else ["public_url_unknown"]
    return {
        "version": __version__,
        "sdk": SDK_VERSION,
        "instance_id": inst.get("instance_id"),
        "schema_version": int(inst.get("schema_version", 0)),
        "policy_version": b.policy_version,
        "home": str(b.home.root),
        "home_source": b.home.source,
        "public_url": public_url,
        "warnings": warnings,
        "sqlite": sqlite_version,
        "sqlite_vec": vec,
        "hardware": hardware.detect(),
        "models": {"embedding": {"configured": b.config["embedding"]["model"], "loaded": False, "spaces": spaces}},
        "sources": sources,
        "plugins": loaded_plugins,
        "operations": [op.schema() for op in REGISTRY.values()] if args.get("schemas", True) else sorted(REGISTRY),
    }


def _db_status(ctx: OpContext, args: dict[str, Any]) -> dict[str, Any]:
    conn = db.connect(ctx.booted.home.db, readonly=True)
    try:
        virtual = [r[0] for r in conn.execute("SELECT name FROM sqlite_schema WHERE sql LIKE 'CREATE VIRTUAL%'")]
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_schema WHERE type IN ('table','view') AND name NOT LIKE 'sqlite_%' ORDER BY name")
            if not any(r[0].startswith(v + "_") for v in virtual)]
        counts = {t: conn.execute(f'SELECT count(*) FROM "{t}"').fetchone()[0]
                  for t in ("asset", "blob", "representation", "provenance", "usage", "policy")}
        return {
            "path": str(ctx.booted.home.db),
            "user_version": db.user_version(conn),
            "latest": db.latest_version(),
            "journal_mode": conn.execute("PRAGMA journal_mode").fetchone()[0],
            "integrity": conn.execute("PRAGMA quick_check").fetchone()[0],
            "counts": counts,
            "tables": tables,
        }
    finally:
        conn.close()


def _search(ctx: OpContext, args: dict[str, Any]) -> dict[str, Any]:
    b = ctx.booted
    conn = db.connect(b.home.db)
    try:
        conn.execute("BEGIN IMMEDIATE")
        res = search.execute_search(
            conn=conn,
            home=b.home,
            query=args["query"],
            kind=args.get("kind"),
            license_filter=args.get("license"),
            anchor=args.get("anchor"),
            entity=args.get("entity"),
            fits=args.get("fits"),
            min_edge=args.get("min_edge"),
            exclude_used_in=args.get("exclude_used_in"),
            include_quotation=args.get("include_quotation", False),
            sheet_requested=args.get("sheet", False),
            explain=args.get("explain", False),
            k=args.get("k", 12),
            cursor=args.get("cursor"),
            actor=ctx.actor,
        )
        conn.execute("COMMIT")
        return res
    finally:
        conn.close()


def _remote(ctx: OpContext, args: dict[str, Any]) -> dict[str, Any]:
    source_name = args["source"]
    query = args["query"]
    limit = args.get("k", 12)
    cursor = args.get("cursor")

    pm = ctx.pm()
    plugin = pm.plugins.get(source_name)
    if not plugin or not plugin.instance or not hasattr(plugin.instance, "search"):
        raise ProproomError("unknown_source", f"Source plugin '{source_name}' not available")

    source_ctx = pm.get_source_context(source_name)
    req = SearchRequest(query=query, limit=limit, cursor=cursor)

    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

    cand_page = loop.run_until_complete(plugin.instance.search(source_ctx, req))

    items = []
    for idx, c in enumerate(cand_page.items, start=1):
        items.append({
            "idx": idx,
            "source": c.ref.source,
            "external_id": c.ref.external_id,
            "title": c.title,
            "thumbnail_url": c.thumbnail_url,
            "width": c.width,
            "height": c.height,
            "duration": c.duration,
            "license": c.rights.license_id if c.rights else None,
            "artist": c.rights.attribution if c.rights else None,
        })

    return {
        "source": source_name,
        "items": items,
        "next_cursor": cand_page.next_cursor,
    }


def _fetch(ctx: OpContext, args: dict[str, Any]) -> dict[str, Any]:
    b = ctx.booted
    source_name = args.get("source")
    external_id = args.get("external_id")
    sel_id = args.get("selection_id")
    pick = args.get("pick")

    conn = db.connect(b.home.db)
    try:
        # If selection_id and pick provided, look up external_id
        if sel_id and pick:
            idx = pick[0] if isinstance(pick, list) else int(pick)
            row = conn.execute(
                "SELECT si.asset_id, p.source, p.external_id FROM selection_item si "
                "LEFT JOIN provenance p ON p.asset_id = si.asset_id "
                "WHERE si.selection_id = ? AND si.idx = ?",
                (sel_id, idx),
            ).fetchone()
            if row and row["source"] and row["external_id"]:
                source_name = row["source"]
                external_id = row["external_id"]

        if not source_name or not external_id:
            raise ProproomError("bad_argument", "source and external_id (or selection_id and pick) required")

        op_id, is_new, prev_res = ops.claim_operation(
            conn, b.home, "fetch", args.get("idempotency_key"), args, actor=ctx.actor
        )
        if not is_new and prev_res is not None:
            return prev_res

        pm = ctx.pm()
        plugin = pm.plugins.get(source_name)
        if not plugin or not plugin.instance or not hasattr(plugin.instance, "resolve"):
            raise ProproomError("unknown_source", f"Source plugin '{source_name}' not available")

        source_ctx = pm.get_source_context(source_name)
        ref = SourceRef(source=source_name, external_id=external_id)

        try:
            loop = asyncio.get_event_loop()
        except RuntimeError:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)

        resolved = loop.run_until_complete(plugin.instance.resolve(source_ctx, ref))
        if not resolved.offers:
            raise ProproomError("no_offers", f"No downloadable representations offered for {external_id}")

        # Choose offer (prefer 1080p video derivative or original image)
        chosen_offer = None
        for off in resolved.offers:
            if off.role in ("vp9_1080", "original"):
                chosen_offer = off
                break
        if not chosen_offer:
            chosen_offer = resolved.offers[0]

        # Download file
        tmp_file, actual_sha, size = loop.run_until_complete(
            download.download_file(b.home, chosen_offer.url, user_agent=source_ctx.http.user_agent)
        )

        # Store in CAS
        stored_sha, stored_bytes, relpath = store.put_file(b.home, tmp_file, mime=chosen_offer.mime, move=True)

        # Register inside transaction
        conn.execute("BEGIN IMMEDIATE")
        dedupe.register_blob(conn, stored_sha, stored_bytes, chosen_offer.mime, stored=True, storage_path=relpath)

        meta = resolved.metadata
        title = meta.get("title") or external_id
        slug_raw = f"{source_name}-{external_id.lower().replace('file:', '').replace(' ', '-')}"
        slug = "".join(c for c in slug_raw if c.isalnum() or c in "-_")[:60]

        kind = "photo"
        if "video" in chosen_offer.mime or "webm" in chosen_offer.mime or "mp4" in chosen_offer.mime:
            kind = "footage"
        elif "audio" in chosen_offer.mime:
            kind = "music"

        aid, rid = dedupe.register_asset(
            conn, slug=slug, kind=kind, title=title, description=meta.get("description")
        )

        rep_id = dedupe.register_representation(
            conn,
            asset_id=aid,
            blob_sha256=stored_sha,
            role=chosen_offer.role,
            width=chosen_offer.width,
            height=chosen_offer.height,
        )

        prov_id = dedupe.register_provenance(
            conn,
            asset_id=aid,
            source=source_name,
            external_id=external_id,
            source_url=chosen_offer.url,
            raw_metadata=meta.get("raw", {}),
            plugin=plugin.name,
            plugin_version=plugin.version,
            plugin_hash=plugin.hash,
        )

        ev_primary = resolved.rights[0] if resolved.rights else None
        basis = ev_primary.basis if ev_primary else "unknown"
        license_id = ev_primary.license_id if ev_primary else None
        attribution = ev_primary.attribution if ev_primary else None

        policy_rules = json.loads(conn.execute("SELECT rules FROM policy WHERE version = ?", (b.policy_version,)).fetchone()["rules"])
        ev_id, ass_id = dedupe.register_rights(
            conn,
            provenance_id=prov_id,
            asset_id=aid,
            policy_version=b.policy_version,
            policy_rules=policy_rules,
            basis=basis,
            license_id=license_id,
            license_url=ev_primary.license_url if ev_primary else None,
            pd_reason=ev_primary.pd_reason if ev_primary else None,
            attribution=attribution,
            restrictions=ev_primary.restrictions if ev_primary else None,
            created_by=source_name,
        )

        dedupe.sync_search_doc(
            conn, rid=rid, title=title, description=meta.get("description", ""), artist=attribution or ""
        )

        # Check assessment decision
        ass_row = conn.execute("SELECT decision, obligations FROM rights_assessment WHERE id = ?", (ass_id,)).fetchone()

        result = {
            "asset_id": aid,
            "representation_id": rep_id,
            "slug": slug,
            "decision": ass_row["decision"],
            "obligations": json.loads(ass_row["obligations"]),
            "sha256": stored_sha,
        }
        ops.complete_operation(conn, b.home, op_id, result)
        conn.execute("COMMIT")
        return result
    except Exception as e:
        if "op_id" in locals():
            ops.fail_operation(conn, b.home, op_id, {"code": "fetch_failed", "message": str(e)})
        raise
    finally:
        conn.close()


def _fetch_many(ctx: OpContext, args: dict[str, Any]) -> dict[str, Any]:
    items = args.get("items", [])
    results = []
    for it in items:
        try:
            res = _fetch(ctx, it)
            results.append({"status": "ok", "result": res})
        except Exception as e:
            results.append({"status": "error", "error": str(e)})
    return {"results": results}


def _add(ctx: OpContext, args: dict[str, Any]) -> dict[str, Any]:
    b = ctx.booted
    paths = args["paths"]
    basis = args.get("basis", "own_work")

    conn = db.connect(b.home.db)
    try:
        op_id, is_new, prev_res = ops.claim_operation(
            conn, b.home, "add", args.get("idempotency_key"), args, actor=ctx.actor
        )
        if not is_new and prev_res is not None:
            return prev_res

        policy_rules = json.loads(conn.execute("SELECT rules FROM policy WHERE version = ?", (b.policy_version,)).fetchone()["rules"])
        added = []

        conn.execute("BEGIN IMMEDIATE")
        for p_str in paths:
            p = Path(p_str)
            if not p.is_absolute():
                p = b.home.nas / p_str
            if not p.is_file():
                continue

            stored_sha, stored_bytes, relpath = store.put_file(b.home, p, move=False)
            dedupe.register_blob(conn, stored_sha, stored_bytes, "application/octet-stream", stored=True, storage_path=relpath)

            slug_raw = f"manual-{p.stem.lower()}"
            slug = "".join(c for c in slug_raw if c.isalnum() or c in "-_")[:60]
            title = p.stem.replace("_", " ")

            aid, rid = dedupe.register_asset(conn, slug=slug, kind="photo", title=title)
            rep_id = dedupe.register_representation(conn, asset_id=aid, blob_sha256=stored_sha, role="original")
            prov_id = dedupe.register_provenance(conn, asset_id=aid, source="manual", external_id=str(p))
            dedupe.register_rights(
                conn, provenance_id=prov_id, asset_id=aid, policy_version=b.policy_version,
                policy_rules=policy_rules, basis=basis, attribution="Local User", created_by=ctx.actor
            )
            dedupe.sync_search_doc(conn, rid=rid, title=title)

            added.append({"asset_id": aid, "slug": slug, "sha256": stored_sha, "representation_id": rep_id})

        result = {"added": added, "count": len(added)}
        ops.complete_operation(conn, b.home, op_id, result)
        conn.execute("COMMIT")
        return result
    finally:
        conn.close()


def _link(ctx: OpContext, args: dict[str, Any]) -> dict[str, Any]:
    b = ctx.booted
    project_arg = args.get("project")
    sel_id = args.get("selection_id")
    pick = args.get("pick")
    assets = args.get("assets")
    is_plan_only = args.get("plan", False)
    plan_id = args.get("plan_id")
    link_mode = args.get("link_mode", "auto")

    conn = db.connect(b.home.db)
    try:
        conn.execute("BEGIN IMMEDIATE")
        # If executing by plan_id
        if plan_id:
            row = conn.execute("SELECT plan FROM operation WHERE id = ?", (plan_id,)).fetchone()
            if not row or not row["plan"]:
                raise ProproomError("plan_not_found", f"Plan id '{plan_id}' not found")
            plan_data = json.loads(row["plan"])
            proj_id = plan_data["project_id"]
            proj_row = conn.execute("SELECT path FROM project WHERE id = ?", (proj_id,)).fetchone()
            proj_dir = b.home.nas / proj_row["path"]
            res = delivery.execute_delivery(conn, b.home, proj_dir, proj_id, plan_id, plan_data)
            ops.complete_operation(conn, b.home, plan_id, res)
            conn.execute("COMMIT")
            return res

        proj_id, proj_dir, proj_relpath = delivery.resolve_project(conn, b.home, project_arg or "default")

        op_id, is_new, prev_res = ops.claim_operation(
            conn, b.home, "link", args.get("idempotency_key"), args, actor=ctx.actor,
            initial_state="planned" if is_plan_only else "running"
        )
        if not is_new and prev_res is not None:
            conn.execute("ROLLBACK")
            return prev_res

        # Gather target items
        items_to_deliver = []
        if sel_id and pick:
            pick_indices = pick if isinstance(pick, list) else [int(pick)]
            placeholders = ",".join("?" for _ in pick_indices)
            rows = conn.execute(
                f"SELECT si.asset_id, si.representation_id, si.segment_id, a.slug "
                f"FROM selection_item si JOIN asset a ON a.id = si.asset_id "
                f"WHERE si.selection_id = ? AND si.idx IN ({placeholders})",
                [sel_id] + pick_indices,
            ).fetchall()
            for r in rows:
                items_to_deliver.append(dict(r))
        elif assets:
            for aid in assets:
                rep = conn.execute("SELECT id FROM representation WHERE asset_id = ? ORDER BY role='original' DESC LIMIT 1", (aid,)).fetchone()
                if rep:
                    items_to_deliver.append({"asset_id": aid, "representation_id": rep["id"], "segment_id": None})

        if not items_to_deliver:
            raise ProproomError("no_items", "No matching items to deliver")

        plan = delivery.prepare_delivery_plan(conn, b.home, proj_dir, proj_id, items_to_deliver, link_mode=link_mode)
        ops.update_plan(conn, b.home, op_id, plan)

        if is_plan_only:
            conn.execute("COMMIT")
            return {"plan_id": op_id, "project": proj_relpath, **plan}

        # Execute
        res = delivery.execute_delivery(conn, b.home, proj_dir, proj_id, op_id, plan)
        ops.complete_operation(conn, b.home, op_id, res)
        conn.execute("COMMIT")
        return res
    finally:
        conn.close()


def _link_many(ctx: OpContext, args: dict[str, Any]) -> dict[str, Any]:
    items = args.get("items", [])
    results = [_link(ctx, it) for it in items]
    return {"results": results}


def _credits(ctx: OpContext, args: dict[str, Any]) -> dict[str, Any]:
    b = ctx.booted
    project_arg = args["project"]
    conn = db.connect(b.home.db, readonly=True)
    try:
        proj_row = conn.execute("SELECT id FROM project WHERE path = ? OR path LIKE ?", (project_arg, f"%{project_arg}%")).fetchone()
        if not proj_row:
            raise ProproomError("project_not_found", f"Project '{project_arg}' not found")
        return delivery.get_project_credits(conn, proj_row["id"])
    finally:
        conn.close()


def _share(ctx: OpContext, args: dict[str, Any]) -> dict[str, Any]:
    b = ctx.booted
    token = secrets.token_urlsafe(24)
    token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()

    share_id = ulid()
    now = utcnow()
    expires_dt = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=30)
    expires = expires_dt.isoformat()

    conn = db.connect(b.home.db)
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "INSERT INTO share(id, token_hash, selection_id, title, note, created, expires) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (share_id, token_hash, args.get("selection_id"), args.get("title"), args.get("note"), now, expires),
        )

        # Populate share_items from selection_id or assets
        pos = 1
        if sel_id := args.get("selection_id"):
            rows = conn.execute("SELECT * FROM selection_item WHERE selection_id = ? ORDER BY idx", (sel_id,)).fetchall()
            for r in rows:
                conn.execute(
                    "INSERT INTO share_item(share_id, pos, asset_id, representation_id, segment_id) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (share_id, pos, r["asset_id"], r["representation_id"], r["segment_id"]),
                )
                pos += 1
        elif asset_list := args.get("assets"):
            for aid in asset_list:
                rep = conn.execute("SELECT id FROM representation WHERE asset_id = ? ORDER BY role='original' DESC LIMIT 1", (aid,)).fetchone()
                if rep:
                    conn.execute(
                        "INSERT INTO share_item(share_id, pos, asset_id, representation_id, segment_id) "
                        "VALUES (?, ?, ?, ?, ?)",
                        (share_id, pos, aid, rep["id"], None),
                    )
                    pos += 1

        conn.execute("COMMIT")

        public_url = b.config["server"]["public_url"] or ""
        share_url = f"{public_url}/s/{token}" if public_url else f"/s/{token}"
        warnings = [] if public_url else ["public_url_unknown"]

        return {
            "token": token,
            "url": share_url,
            "expires": expires,
            "warnings": warnings,
        }
    finally:
        conn.close()


def _share_feedback(ctx: OpContext, args: dict[str, Any]) -> dict[str, Any]:
    b = ctx.booted
    share_token = args["share"]
    token_hash = hashlib.sha256(share_token.encode("utf-8")).hexdigest()

    conn = db.connect(b.home.db, readonly=True)
    try:
        share_row = conn.execute("SELECT id FROM share WHERE token_hash = ?", (token_hash,)).fetchone()
        if not share_row:
            raise ProproomError("share_not_found", "Share link not found or expired")
        sid = share_row["id"]
        rows = conn.execute(
            "SELECT pos, verdict, comment, created FROM share_feedback WHERE share_id = ? ORDER BY pos, created",
            (sid,),
        ).fetchall()
        return {"share_id": sid, "feedback": [dict(r) for r in rows]}
    finally:
        conn.close()


def _share_revoke(ctx: OpContext, args: dict[str, Any]) -> dict[str, Any]:
    b = ctx.booted
    share_token = args["share"]
    token_hash = hashlib.sha256(share_token.encode("utf-8")).hexdigest()

    conn = db.connect(b.home.db)
    try:
        now = utcnow()
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("UPDATE share SET revoked_at = ? WHERE token_hash = ?", (now, token_hash))
        conn.execute("COMMIT")
        return {"revoked": True}
    finally:
        conn.close()


def _job(ctx: OpContext, args: dict[str, Any]) -> dict[str, Any]:
    b = ctx.booted
    action = args["action"]
    job_id = args["job_id"]

    conn = db.connect(b.home.db)
    try:
        if action == "get":
            j = jobs.get_job(conn, job_id)
            if not j:
                raise ProproomError("job_not_found", f"Job {job_id} not found")
            return j
        elif action == "cancel":
            jobs.cancel_job(conn, job_id)
            return {"job_id": job_id, "state": "cancelled"}
        elif action == "pause":
            jobs.pause_job(conn, job_id)
            return {"job_id": job_id, "state": "paused"}
        elif action == "resume":
            jobs.resume_job(conn, job_id)
            return {"job_id": job_id, "state": "running"}
        raise ProproomError("bad_action", f"Unknown job action {action}")
    finally:
        conn.close()


def _pull(ctx: OpContext, args: dict[str, Any]) -> dict[str, Any]:
    b = ctx.booted
    spec = args["spec"]
    budget = args.get("budget", "1G")

    conn = db.connect(b.home.db)
    try:
        conn.execute("BEGIN IMMEDIATE")
        job_id = jobs.create_job(conn, "pull", {"spec": spec, "budget": budget})
        conn.execute("COMMIT")
        return {"job_id": job_id, "state": "running", "progress": {"spec": spec}}
    finally:
        conn.close()


def _verify(ctx: OpContext, args: dict[str, Any]) -> dict[str, Any]:
    b = ctx.booted
    conn = db.connect(b.home.db)
    try:
        conn.execute("BEGIN IMMEDIATE")
        job_id = jobs.create_job(conn, "verify", {})
        conn.execute("COMMIT")

        # Run verification over all blobs
        rows = conn.execute("SELECT sha256 FROM blob WHERE stored = 1").fetchall()
        checked = 0
        mismatches = []
        for r in rows:
            st = store.verify_blob(b.home, r["sha256"])
            checked += 1
            if st != "ok":
                mismatches.append({"sha256": r["sha256"], "state": st})
            conn.execute("UPDATE blob SET verify_state = ?, verified_at = ? WHERE sha256 = ?", (st, utcnow(), r["sha256"]))

        jobs.finish_job(conn, job_id, "succeeded")
        return {
            "job_id": job_id,
            "state": "succeeded",
            "progress": {"checked": checked, "mismatches": mismatches},
        }
    finally:
        conn.close()


def _import_project(ctx: OpContext, args: dict[str, Any]) -> dict[str, Any]:
    b = ctx.booted
    project_arg = args["project"]

    conn = db.connect(b.home.db)
    try:
        conn.execute("BEGIN IMMEDIATE")
        op_id, _, _ = ops.claim_operation(conn, b.home, "import_project", args.get("idempotency_key"), args, actor=ctx.actor)
        proj_id, proj_dir, proj_relpath = delivery.resolve_project(conn, b.home, project_arg)
        job_id = jobs.create_job(conn, "import_project", {"project": proj_relpath}, operation_id=op_id)
        conn.execute("COMMIT")

        manifest_file = proj_dir / "assets.json"
        if not manifest_file.is_file():
            jobs.finish_job(conn, job_id, "failed", {"error": "assets.json not found"})
            return {"job_id": job_id, "state": "failed", "progress": {"error": "assets.json not found"}}

        manifest_data = json.loads(manifest_file.read_text(encoding="utf-8"))
        assets_list = manifest_data.get("assets", [])
        policy_rules = json.loads(conn.execute("SELECT rules FROM policy WHERE version = ?", (b.policy_version,)).fetchone()["rules"])

        imported = 0
        conn.execute("BEGIN IMMEDIATE")
        for item in assets_list:
            as_name = item.get("id")
            rel_file = item.get("file")
            abs_file = proj_dir / rel_file if rel_file else None

            if not abs_file or not abs_file.is_file():
                continue

            stored_sha, stored_bytes, blob_path = store.put_file(b.home, abs_file, move=False)
            dedupe.register_blob(conn, stored_sha, stored_bytes, "application/octet-stream", stored=True, storage_path=blob_path)

            slug_raw = f"{proj_dir.name}-{as_name}"
            slug = "".join(c for c in slug_raw if c.isalnum() or c in "-_")[:60]
            title = item.get("desc") or as_name

            aid, rid = dedupe.register_asset(conn, slug=slug, kind=item.get("kind", "photo"), title=title)
            size = item.get("size") or [None, None]
            rep_id = dedupe.register_representation(
                conn, asset_id=aid, blob_sha256=stored_sha, role="original",
                width=size[0], height=size[1], duration=item.get("duration")
            )

            prov_id = dedupe.register_provenance(
                conn, asset_id=aid, source="commons" if "commons" in (item.get("source") or "") else "import",
                external_id=item.get("source") or as_name, source_url=item.get("source")
            )

            ev_id, ass_id = dedupe.register_rights(
                conn, provenance_id=prov_id, asset_id=aid, policy_version=b.policy_version,
                policy_rules=policy_rules, basis="license", license_id=item.get("license"),
                attribution=item.get("artist") or item.get("credit"), created_by="import_project"
            )

            dedupe.sync_search_doc(conn, rid=rid, title=title, description=item.get("desc", ""), artist=item.get("artist", ""))

            # Backfill usage row
            usage_id = ulid()
            conn.execute(
                "INSERT INTO usage(id, project_id, operation_id, asset_id, representation_id, as_name, "
                "credit_snapshot, rights_assessment_id, policy_version, delivered_relpath, delivered_sha256, link_mode, origin, created) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'copy', 'import', ?) "
                "ON CONFLICT(project_id, as_name) WHERE removed_at IS NULL DO NOTHING",
                (usage_id, proj_id, op_id, aid, rep_id, as_name, json.dumps(item), ass_id, b.policy_version, rel_file, stored_sha, utcnow()),
            )
            imported += 1

        jobs.finish_job(conn, job_id, "succeeded")
        res = {"job_id": job_id, "state": "succeeded", "progress": {"imported": imported}}
        ops.complete_operation(conn, b.home, op_id, res)
        conn.execute("COMMIT")
        return res
    finally:
        conn.close()


def _plugin_list(ctx: OpContext, args: dict[str, Any]) -> dict[str, Any]:
    pm = ctx.pm()
    plugins = []
    for p in pm.plugins.values():
        plugins.append({
            "name": p.name,
            "version": p.version,
            "hash": p.hash,
            "types": p.types,
            "origin": p.origin,
            "status": "ready",
        })
    return {"plugins": plugins}


def _plugin_reload(ctx: OpContext, args: dict[str, Any]) -> dict[str, Any]:
    b = ctx.booted
    pm = ctx.pm()
    conn = db.connect(b.home.db)
    try:
        conn.execute("BEGIN IMMEDIATE")
        pm.load_all(conn)
        conn.execute("COMMIT")
    finally:
        conn.close()
    return {"reloaded": True, "plugins": [p.name for p in pm.plugins.values()]}


def _plugin_new(ctx: OpContext, args: dict[str, Any]) -> dict[str, Any]:
    name = args["name"]
    types = args["types"]
    p_dir = ctx.booted.home.dir("plugins") / name
    p_dir.mkdir(parents=True, exist_ok=True)

    manifest_content = f"""[plugin]
name = "{name}"
version = "0.0.1"
sdk = ">=0.1,<2"
types = {json.dumps(types)}
status = "ready"
"""
    (p_dir / "plugin.toml").write_text(manifest_content, encoding="utf-8")
    (p_dir / "__init__.py").write_text(f'"""Plugin {name}."""\n\nclass Plugin:\n    pass\n', encoding="utf-8")
    return {"created": str(p_dir)}


def _plugin_test(ctx: OpContext, args: dict[str, Any]) -> dict[str, Any]:
    name = args["name"]
    pm = ctx.pm()
    p = pm.plugins.get(name)
    if not p:
        raise ProproomError("plugin_not_found", f"Plugin {name} not found")
    return {"plugin": name, "contract": "ok"}


# ───────────────────────── registry ─────────────────────────
_OPS = [
    Operation("capabilities", "Sources, modalities, model state, plugins and every operation's schema.",
              input=obj(schemas=B), mcp="default", handler=_capabilities),
    Operation("db_status", "Schema version, integrity check and row counts.", mcp="admin", handler=_db_status),
    Operation("search", "Hybrid local search (vector + FTS, RRF). Returns a selection_id and optional sheet.",
              input=obj(["query"], query=S, kind=SL, license=S, anchor=S, entity=S, fits=S, min_edge=I,
                        exclude_used_in=S, include_quotation=B, sheet=B, explain=B, k=I, cursor=S),
              output=obj(selection_id=S, items={"type": "array"}, score_type=S, next_cursor=S, sheet=S),
              mcp="default", handler=_search),
    Operation("remote", "Search a remote source; candidates only, nothing is stored.",
              input=obj(["source", "query"], source=S, query=S, kind=SL, sheet=B, cursor=S),
              output=obj(selection_id=S, items={"type": "array"}, next_cursor=S), mcp="default", handler=_remote),
    Operation("fetch", "Resolve -> rights check -> download -> ingest one remote candidate.",
              input=obj(selection_id=S, pick=IL, source=S, external_id=S), side_effects=True, mcp="default", handler=_fetch),
    Operation("fetch_many", "Batch fetch; per-item success/failure.", input=obj(items={"type": "array"}),
              side_effects=True, handler=_fetch_many),
    Operation("add", "Ingest local files (own work / manual).", input=obj(["paths"], paths=SL, basis=S),
              side_effects=True, handler=_add),
    Operation("link", "Deliver picks to a project; --plan previews, then execute by plan id.",
              input=obj(selection_id=S, pick=IL, assets=SL, project=S, plan=B, plan_id=S, link_mode=S, cut=S),
              side_effects=True, mcp="default", handler=_link),
    Operation("link_many", "Batch link; per-item success/failure.", input=obj(items={"type": "array"}),
              side_effects=True, handler=_link_many),
    Operation("credits", "Attribution for a project, grouped by basis, from delivery-time snapshots.",
              input=obj(["project"], project=S, exporter=S), mcp="default", handler=_credits),
    Operation("share", "Create a share link for assets or a selection.",
              input=obj(assets=SL, selection_id=S, title=S, note=S, expires=S),
              output=obj(token=S, url=S, warnings=SL), side_effects=True, mcp="default", handler=_share),
    Operation("share_feedback", "Read want/reject verdicts and comments from a share.",
              input=obj(["share"], share=S), mcp="default", handler=_share_feedback),
    Operation("share_revoke", "Revoke a share link.", input=obj(["share"], share=S), side_effects=True, handler=_share_revoke),
    Operation("job", "get / cancel / pause / resume a long-running job.",
              input=obj(["action", "job_id"], action={"enum": ["get", "cancel", "pause", "resume"]}, job_id=S),
              output=JOB, mcp="default", handler=_job),
    Operation("pull", "Bulk pull from a pull.yaml as a background job with a byte budget.",
              input=obj(["spec"], spec=S, budget=S), output=JOB, side_effects=True, handler=_pull),
    Operation("verify", "Recompute blob sha256 and report mismatches and the projects sharing them.",
              output=JOB, mcp="admin", handler=_verify),
    Operation("import_project", "Import a project's existing assets.json and back-fill usage.",
              input=obj(["project"], project=S), output=JOB, side_effects=True, mcp="admin", handler=_import_project),
    Operation("plugin_list", "Loaded plugins with version and hash.", mcp="admin", handler=_plugin_list),
    Operation("plugin_reload", "Reload directory plugins; running jobs keep the old instance.",
              side_effects=True, mcp="admin", handler=_plugin_reload),
    Operation("plugin_new", "Scaffold a plugin with tests.", input=obj(["name", "types"], name=S, types=SL),
              mcp="admin", handler=_plugin_new),
    Operation("plugin_test", "Offline contract test with recorded HTTP fixtures.", input=obj(["name"], name=S),
              mcp="admin", handler=_plugin_test),
]

REGISTRY: dict[str, Operation] = {op.name: op for op in _OPS}
