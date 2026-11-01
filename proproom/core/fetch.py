"""Fetch and remote search pipelines.

Handover §8 / §12:
- remote search across source plugins, storing selections
- resolve -> rights check (over all evidence, failing closed) -> download -> ingest
"""
from __future__ import annotations

import json
import hashlib
import sqlite3
from pathlib import Path
from typing import Any

from . import db, dedupe, download, events, ops, rights, store
from .ids import ulid, utcnow
from .media import normalize_kind, sniff_mime
from .plugins import run_sync
from ..sdk.errors import ProproomError
from ..sdk.types import RepresentationOffer, ResolvedSource, RightsEvidence, SearchRequest, SourceRef, TransferRequest


def evaluate_source_rights(
    ctx: Any,
    pm: Any,
    policy_version: int,
    policy_rules: dict[str, Any],
    source_name: str,
    external_id: str,
    resolved: ResolvedSource,
) -> tuple[str, list[dict[str, Any]], set[str], list[RightsEvidence], list[dict[str, Any]]]:
    """Evaluate rights across all evidence items and policy plugins, failing closed."""
    plugin_restrictions: list[dict[str, Any]] = []
    if pm:
        for p in pm.plugins.values():
            if "policy" in p.types and p.instance and hasattr(p.instance, "decide"):
                try:
                    title_candidate = resolved.title or ""
                    full_title = f"{title_candidate} {external_id}".strip()
                    p_asset = {"kind": resolved.kind, "title": full_title, "external_id": external_id}
                    p_dec = p.instance.decide(ctx, p_asset)
                    if hasattr(p_dec, "decision"):
                        plugin_restrictions.append({
                            "decision": p_dec.decision,
                            "code": getattr(p_dec, "code", ""),
                            "plugin": p.name,
                        })
                except Exception as e:
                    # Fail-closed: policy plugin crash must deny
                    plugin_restrictions.append({
                        "decision": "deny",
                        "code": "policy_plugin_crashed",
                        "plugin": p.name,
                        "detail": str(e),
                    })

    ev_list = resolved.evidence
    if not ev_list:
        ev_list = [RightsEvidence(basis="unknown")]

    all_reasons = []
    all_obligations = set()
    overall_decision = "allow"

    for ev_item in ev_list:
        b_item = ev_item.basis or "unknown"
        lic_item = rights.normalize_license(ev_item.license_id) if ev_item.license_id else None
        pdr_item = ev_item.pd_reason
        restr_item = ev_item.restrictions

        dec_i, reas_i, oblig_i = rights.evaluate_rights(
            policy_rules=policy_rules,
            basis=b_item,
            license_id=lic_item,
            pd_reason=pdr_item,
            restrictions=restr_item,
            publisher=resolved.publisher,
            plugin_restrictions=plugin_restrictions,
        )
        all_reasons.extend(reas_i)
        all_obligations.update(oblig_i)

        if dec_i == "deny":
            overall_decision = "deny"
        elif dec_i == "review" and overall_decision != "deny":
            overall_decision = "review"

    return overall_decision, all_reasons, all_obligations, ev_list, plugin_restrictions


def remote(ctx: Any, args: dict[str, Any]) -> dict[str, Any]:
    """Search a remote source; candidates only, nothing is stored."""
    source_name = args["source"]
    query = args["query"]
    limit = args.get("k", 12)
    cursor = args.get("cursor")

    pm = getattr(ctx, "plugin_manager", None) or (ctx.pm() if hasattr(ctx, "pm") else None)
    plugin = pm.plugins.get(source_name) if pm else None
    if not plugin or not plugin.instance or not hasattr(plugin.instance, "search"):
        raise ProproomError("unknown_source", f"Source plugin '{source_name}' not available")

    source_ctx = pm.get_source_context(source_name)
    req = SearchRequest(query=query, limit=limit, cursor=cursor)

    cand_page = run_sync(plugin.instance.search(source_ctx, req))

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

    sel_id = ulid()
    conn = db.connect(ctx.booted.home.db)
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "INSERT INTO selection(id, created, created_by, query) VALUES (?, ?, ?, ?)",
            (sel_id, utcnow(), ctx.actor, json.dumps({"source": source_name, "query": query, "candidates": items})),
        )
        conn.execute("COMMIT")
    finally:
        conn.close()

    return {
        "selection_id": sel_id,
        "source": source_name,
        "items": items,
        "next_cursor": cand_page.next_cursor,
    }


def fetch(ctx: Any, args: dict[str, Any]) -> dict[str, Any]:
    """Resolve -> rights check -> download -> ingest one remote candidate."""
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
            else:
                s_row = conn.execute("SELECT query FROM selection WHERE id = ?", (sel_id,)).fetchone()
                if s_row:
                    q_data = json.loads(s_row["query"])
                    for cand in q_data.get("candidates", []):
                        if cand.get("idx") == idx:
                            source_name = cand.get("source")
                            external_id = cand.get("external_id")
                            break

        if not source_name or not external_id:
            raise ProproomError("bad_argument", "source and external_id (or selection_id and pick) required")

        conn.execute("BEGIN IMMEDIATE")
        op_id, is_new, prev_res = ops.claim_operation(
            conn, b.home, "fetch", args.get("idempotency_key"), args, actor=ctx.actor
        )
        conn.execute("COMMIT")
        if not is_new and prev_res is not None:
            return prev_res

        pm = getattr(ctx, "plugin_manager", None) or (ctx.pm() if hasattr(ctx, "pm") else None)
        plugin = pm.plugins.get(source_name) if pm else None
        if not plugin or not plugin.instance or not hasattr(plugin.instance, "resolve"):
            raise ProproomError("unknown_source", f"Source plugin '{source_name}' not available")

        source_ctx = pm.get_source_context(source_name)
        if args.get("options") is not None and args.get("revision"):
            raise ProproomError("bad_argument", "Specify either source options or revision")
        revision = json.dumps(args["options"], sort_keys=True, separators=(",", ":")) if args.get("options") is not None else args.get("revision", "")
        ref = SourceRef(source=source_name, external_id=external_id, revision=revision)

        resolved = run_sync(plugin.instance.resolve(source_ctx, ref))
        if not resolved.offers:
            raise ProproomError("no_offers", f"No downloadable representations offered for {external_id}")

        # Rights check BEFORE downloading!
        policy_rules = json.loads(
            conn.execute("SELECT rules FROM policy WHERE version = ?", (b.policy_version,)).fetchone()["rules"]
        )

        overall_decision, all_reasons, all_obligations, ev_list, plugin_restrictions = evaluate_source_rights(
            ctx=ctx,
            pm=pm,
            policy_version=b.policy_version,
            policy_rules=policy_rules,
            source_name=source_name,
            external_id=external_id,
            resolved=resolved,
        )

        if overall_decision == "deny":
            raise ProproomError(
                code="rights_denied",
                message=f"Rights policy denied fetch for {external_id}",
                details={"reasons": all_reasons, "obligations": list(all_obligations)},
                retryable=False,
            )

        # Choose offer (prefer <=1080p video derivative over full original, else original)
        chosen_offer = None
        for off in resolved.offers:
            if off.role == "vp9_1080" or (off.role != "original" and off.height and off.height <= 1080):
                chosen_offer = off
                break
        if not chosen_offer:
            for off in resolved.offers:
                if off.role == "original":
                    chosen_offer = off
                    break
        if not chosen_offer:
            chosen_offer = resolved.offers[0]

        # Special protocols stream into a core-owned sink after rights evaluation.
        max_bytes = args.get("max_bytes")
        if max_bytes is not None and (isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes < 1):
            raise ProproomError("bad_argument", "max_bytes must be a positive integer")
        def retrieve(offer: RepresentationOffer, limit: int | None) -> tuple[Path, str, int]:
            if callable(getattr(plugin.instance, "transfer", None)):
                return run_sync(download.transfer_to_file(
                    b.home, plugin.instance, source_ctx, TransferRequest(ref=resolved.ref, offer=offer),
                    max_bytes=limit,
                ))
            return run_sync(download.download_file(
                b.home, offer.url, user_agent=source_ctx.http.user_agent,
                allowed_domains=source_ctx.http.allowed_domains, max_bytes=limit,
            ))

        tmp_file, actual_sha, size = retrieve(chosen_offer, max_bytes)
        snapshot_offer = next((offer for offer in resolved.offers if offer.role == "snapshot"), None)
        snapshot_data = None
        try:
            if snapshot_offer is not None and snapshot_offer is not chosen_offer:
                remaining = max_bytes - size if max_bytes is not None else None
                if remaining is not None and remaining <= 0:
                    raise ProproomError("budget_exceeded", "Screenshot and snapshot exceed max_bytes")
                snapshot_tmp, _, _ = retrieve(snapshot_offer, remaining)
                try:
                    snapshot_data = store.put_file(b.home, snapshot_tmp, mime=snapshot_offer.mime, move=True)
                finally:
                    snapshot_tmp.unlink(missing_ok=True)
            stored_sha, stored_bytes, relpath = store.put_file(b.home, tmp_file, mime=chosen_offer.mime, move=True)
        finally:
            tmp_file.unlink(missing_ok=True)

        # Register inside transaction
        conn.execute("BEGIN IMMEDIATE")
        dedupe.register_blob(conn, stored_sha, stored_bytes, chosen_offer.mime, stored=True, storage_path=relpath)
        if snapshot_data:
            snapshot_sha, snapshot_bytes, snapshot_relpath = snapshot_data
            dedupe.register_blob(conn, snapshot_sha, snapshot_bytes, snapshot_offer.mime, stored=True, storage_path=snapshot_relpath)

        meta = resolved.metadata or {}
        title = resolved.title or meta.get("title") or external_id

        # Deduplicate asset by checking provenance (source, external_id)
        prov_row = conn.execute(
            "SELECT asset_id FROM provenance WHERE source = ? AND external_id = ?",
            (source_name, external_id),
        ).fetchone()

        if prov_row:
            aid = prov_row["asset_id"]
            asset_row = conn.execute("SELECT rid, slug, kind FROM asset WHERE id = ?", (aid,)).fetchone()
            rid = asset_row["rid"]
            slug = asset_row["slug"]
            kind = asset_row["kind"]
        else:
            slug = dedupe.generate_unique_slug(conn, source_name, external_id)
            kind = normalize_kind(resolved.kind, sniff_mime(None, chosen_offer.url)[1])

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
            revision=hashlib.sha256((resolved.ref.revision + "\0" + stored_sha + "\0" + snapshot_data[0]).encode("utf-8")).hexdigest() if snapshot_data else resolved.ref.revision,
            source_url=resolved.source_url or chosen_offer.url,
            final_url=meta.get("final_url"),
            publisher=resolved.publisher,
            publisher_type=resolved.publisher_type,
            snapshot_sha256=snapshot_data[0] if snapshot_data else None,
            raw_metadata=meta.get("raw", {}),
            plugin=plugin.name,
            plugin_version=plugin.version,
            plugin_hash=plugin.hash,
        )

        ass_id = None
        for ev_item in ev_list:
            b_item = ev_item.basis or "unknown"
            lic_item = rights.normalize_license(ev_item.license_id) if ev_item.license_id else None
            attr_item = ev_item.attribution or meta.get("artist") or meta.get("credit")
            ev_id, a_id = dedupe.register_rights(
                conn,
                provenance_id=prov_id,
                asset_id=aid,
                policy_version=b.policy_version,
                policy_rules=policy_rules,
                basis=b_item,
                license_id=lic_item,
                license_url=ev_item.license_url,
                pd_reason=ev_item.pd_reason,
                jurisdiction=ev_item.jurisdiction,
                attribution=attr_item,
                restrictions=ev_item.restrictions,
                covers=ev_item.covers,
                original_text=ev_item.original_text,
                locator=ev_item.locator,
                origin=ev_item.origin,
                plugin_restrictions=plugin_restrictions,
                created_by=source_name,
                publisher=resolved.publisher,
            )
            ass_id = a_id

        attribution = ev_list[0].attribution if ev_list else None

        dedupe.sync_search_doc(
            conn, rid=rid, title=title, description=meta.get("description", ""), artist=attribution or ""
        )


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
        events.publish_event(conn, "ingested", {"asset_id": aid, "source": source_name, "external_id": external_id})
        conn.execute("COMMIT")
        return result
    except Exception as e:
        if "op_id" in locals():
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            conn.execute("BEGIN IMMEDIATE")
            ops.fail_operation(conn, b.home, op_id, {"code": "fetch_failed", "message": str(e)})
            conn.execute("COMMIT")
        raise
    finally:
        conn.close()


def fetch_many(ctx: Any, args: dict[str, Any]) -> dict[str, Any]:
    """Batch fetch; per-item success/failure."""
    items = args.get("items", [])
    results = []
    for it in items:
        try:
            res = fetch(ctx, it)
            results.append({"status": "ok", "result": res})
        except Exception as e:
            results.append({"status": "error", "error": str(e)})
    return {"results": results}
