"""Delivery: plan, temp file in target dir, manifest patch, atomic rename, usage rows; clip/fast/exact cuts.

Handover §10.
"""
from __future__ import annotations

import json
import hashlib
import os
import sqlite3
from pathlib import Path
from typing import Any

from ..sdk.errors import ProproomError
from . import db, events, ops, store
from .ids import ulid, utcnow
from .journal import acquire_delivery_lock, append_journal, prepare_write, rollback_delivery
from .paths import Home


from .media import guess_extension


def resolve_project(
    conn: sqlite3.Connection,
    home: Home,
    project_arg: str,
    manifest_format: str = "assets_json",
    create: bool = True,
) -> tuple[str, Path, str]:
    """Resolve project id and directory path under /nas."""
    if not project_arg:
        raise ProproomError("bad_argument", "project argument is required")
    nas = home.nas
    clean = project_arg.strip("/")
    # Try as relative to /nas
    proj_dir = (nas / clean).resolve()
    # Path containment check against NAS
    try:
        if not proj_dir.is_relative_to(nas.resolve()):
            raise ProproomError("path_traversal", f"Project directory '{project_arg}' escapes NAS")
    except Exception:
        raise ProproomError("path_traversal", f"Project directory '{project_arg}' escapes NAS")

    if not proj_dir.is_dir():
        # Try checking if clean is in project/
        alt = (nas / "project" / clean).resolve()
        if alt.is_dir():
            proj_dir = alt
        elif create:
            proj_dir.mkdir(parents=True, exist_ok=True)

    relpath = str(proj_dir.relative_to(nas.resolve()))
    name = proj_dir.name

    now = utcnow()
    row = conn.execute("SELECT id, name, manifest_format FROM project WHERE path = ?", (relpath,)).fetchone()
    if row:
        return row["id"], proj_dir, relpath

    if not create:
        return "plan_" + ulid(), proj_dir, relpath

    proj_id = ulid()
    conn.execute(
        "INSERT INTO project(id, path, name, manifest_format, created) VALUES (?, ?, ?, ?, ?)",
        (proj_id, relpath, name, manifest_format, now),
    )
    return proj_id, proj_dir, relpath


def prepare_delivery_plan(
    conn: sqlite3.Connection,
    home: Home,
    project_dir: Path,
    project_id: str,
    items: list[dict[str, Any]],
    link_mode: str = "auto",
    cut_mode: str = "manifest",
    confirm_review: bool = False,
) -> dict[str, Any]:
    """Build a delivery plan before execution."""
    planned_files = []
    conflicts = []
    obligations = set()
    blocked = []
    credits_list = []

    for item in items:
        aid = item["asset_id"]
        rep_id = item["representation_id"]
        as_name = item.get("as_name") or item.get("slug") or aid

        rep = conn.execute("SELECT * FROM representation WHERE id = ?", (rep_id,)).fetchone()
        asset = conn.execute("SELECT * FROM asset WHERE id = ?", (aid,)).fetchone()
        blob = conn.execute("SELECT * FROM blob WHERE sha256 = ?", (rep["blob_sha256"],)).fetchone()
        ass = conn.execute(
            "SELECT ra.*, p.rules FROM rights_assessment ra "
            "JOIN policy p ON p.version = ra.policy_version "
            "WHERE ra.asset_id = ? ORDER BY ra.assessed_at DESC LIMIT 1",
            (aid,),
        ).fetchone()

        if not ass or ass["decision"] == "deny":
            blocked.append({
                "asset_id": aid,
                "as_name": as_name,
                "reason": "rights_denied",
                "details": json.loads(ass["reasons"]) if ass else [],
            })
            continue

        if ass["decision"] == "review" and not confirm_review:
            blocked.append({
                "asset_id": aid,
                "as_name": as_name,
                "reason": "needs_confirmation",
                "details": json.loads(ass["reasons"]) if ass else [],
            })
            continue

        item_obs = json.loads(ass["obligations"]) if ass["obligations"] else []
        obligations.update(item_obs)

        prov = conn.execute("SELECT * FROM provenance WHERE asset_id = ? ORDER BY retrieved DESC LIMIT 1", (aid,)).fetchone()
        ev = conn.execute("SELECT * FROM rights_evidence WHERE provenance_id = ? ORDER BY created DESC LIMIT 1", (prov["id"],)).fetchone() if prov else None

        # File extension
        blob_file = store.blob_abspath(home, rep["blob_sha256"])
        fallback_name = (prov["source_url"] if prov else None) or (prov["external_id"] if prov else None)
        ext = guess_extension(blob["mime"], blob_file, fallback_name)

        rel_dest = f"assets/{as_name}{ext}"
        abs_dest = (project_dir / rel_dest).resolve()
        try:
            if not abs_dest.is_relative_to(project_dir.resolve()):
                raise ProproomError("path_traversal", f"Target name '{as_name}' escapes project directory")
        except Exception:
            raise ProproomError("path_traversal", f"Target name '{as_name}' escapes project directory")

        if abs_dest.exists():
            conflicts.append({"relpath": rel_dest, "exists": True})

        credit_snapshot = {
            "title": asset["title"],
            "artist": ev["attribution"] if ev else None,
            "license": ev["license_id"] if ev else None,
            "source_url": prov["source_url"] if prov else None,
            "basis": ass["basis"],
        }
        credits_list.append(credit_snapshot)

        planned_files.append({
            "asset_id": aid,
            "representation_id": rep_id,
            "segment_id": item.get("segment_id"),
            "as_name": as_name,
            "relpath": rel_dest,
            "source_sha256": rep["blob_sha256"],
            "rights_assessment_id": ass["id"],
            "policy_version": ass["policy_version"],
            "credit_snapshot": credit_snapshot,
            "link_mode": link_mode,
        })

    return {
        "project_id": project_id,
        "planned_files": planned_files,
        "conflicts": conflicts,
        "obligations": sorted(obligations),
        "blocked": blocked,
        "credits": credits_list,
    }


def execute_delivery(
    conn: sqlite3.Connection,
    home: Home,
    project_dir: Path,
    project_id: str,
    operation_id: str,
    plan: dict[str, Any],
    manifest_format: str = "assets_json",
) -> dict[str, Any]:
    """Execute delivery plan atomically and record usage."""
    delivered = []
    assets_dir = project_dir / "assets"
    assets_dir.mkdir(parents=True, exist_ok=True)

    for item in plan["planned_files"]:
        aid = item["asset_id"]
        rep_id = item["representation_id"]
        seg_id = item["segment_id"]
        rel_dest = item["relpath"]
        abs_dest = project_dir / rel_dest
        src_sha = item["source_sha256"]
        blob_path = store.blob_abspath(home, src_sha)

        if not blob_path.is_file():
            raise ProproomError("blob_missing", f"Blob {src_sha} not found in store")

        temporary = abs_dest.parent / f".prop-{operation_id}-{ulid()}.tmp"
        prepare_write(home, operation_id, abs_dest, src_sha, temporary=temporary)
        used_mode = store.link_or_copy(blob_path, abs_dest, mode=item.get("link_mode", "auto"), temporary=temporary)
        actual_sha, _ = store.hash_file(abs_dest)

        now = utcnow()
        usage_id = ulid()

        conn.execute(
            "UPDATE usage SET removed_at = ? WHERE project_id = ? AND as_name = ? AND removed_at IS NULL",
            (now, project_id, item["as_name"]),
        )
        conn.execute(
            "INSERT INTO usage(id, project_id, operation_id, asset_id, representation_id, segment_id, "
            "as_name, clip_requested, clip_actual, modifications, credit_snapshot, rights_assessment_id, "
            "policy_version, delivered_relpath, delivered_sha256, link_mode, origin, created) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'link', ?)",
            (usage_id, project_id, operation_id, aid, rep_id, seg_id, item["as_name"],
             None, None, "[]", json.dumps(item["credit_snapshot"]), item["rights_assessment_id"],
             item["policy_version"], rel_dest, actual_sha, used_mode, now),
        )

        delivered.append({
            "asset_id": aid,
            "as_name": item["as_name"],
            "path": rel_dest,
            "link_mode": used_mode,
            "sha256": actual_sha,
        })
        append_journal(home, operation_id, "delivered_file", {"path": rel_dest, "sha256": actual_sha})

    # Update manifest in project (e.g. assets.json)
    _update_manifest(conn, project_dir, project_id, manifest_format, home=home, operation_id=operation_id)

    return {
        "project_id": project_id,
        "delivered": delivered,
        "obligations": plan["obligations"],
    }


def _update_manifest(conn: sqlite3.Connection, project_dir: Path, project_id: str, manifest_format: str, *, home: Home, operation_id: str) -> None:
    """Update project's assets.json manifest, preserving existing non-managed entries."""
    manifest_file = project_dir / "assets.json"
    rows = conn.execute(
        "SELECT u.as_name, a.kind, u.delivered_relpath, a.title, a.description, "
        "p.source_url, re.license_id, re.attribution, rep.width, rep.height, rep.duration, u.created "
        "FROM usage u "
        "JOIN asset a ON a.id = u.asset_id "
        "JOIN representation rep ON rep.id = u.representation_id "
        "LEFT JOIN provenance p ON p.asset_id = a.id "
        "LEFT JOIN rights_evidence re ON re.provenance_id = p.id "
        "WHERE u.project_id = ? AND u.removed_at IS NULL "
        "ORDER BY u.created",
        (project_id,),
    ).fetchall()

    manifest_assets_map: dict[str, Any] = {}
    other_entries: list[Any] = []
    was_list = False
    if manifest_file.is_file():
        raw_text = manifest_file.read_text(encoding="utf-8")
        try:
            existing = json.loads(raw_text)
        except Exception as e:
            raise ProproomError("corrupt_manifest", f"Unreadable manifest file {manifest_file}: {e}")
        if isinstance(existing, list):
            was_list = True
            entries_list = existing
        elif isinstance(existing, dict):
            entries_list = existing.get("assets", [])
        else:
            entries_list = []
        for entry in entries_list:
            eid = entry.get("id")
            if eid:
                manifest_assets_map[eid] = entry
            else:
                other_entries.append(entry)

    seen = set()
    for r in rows:
        as_name = r["as_name"]
        if as_name in seen:
            continue
        seen.add(as_name)
        kind = "footage" if r["kind"] == "footage" else r["kind"]
        size = [r["width"], r["height"]] if r["width"] and r["height"] else None
        item: dict[str, Any] = {
            "id": as_name,
            "kind": kind,
            "file": r["delivered_relpath"],
            "desc": r["description"] or r["title"],
            "source": r["source_url"] or "",
            "license": r["license_id"] or "",
            "artist": r["attribution"] or "",
            "credit": f"{r['attribution']} / {r['license_id']}" if r["attribution"] else (r["license_id"] or ""),
            "size": size,
            "fetched": r["created"][:10],
        }
        if r["duration"]:
            item["duration"] = r["duration"]
        manifest_assets_map[as_name] = item

    all_assets = list(manifest_assets_map.values()) + other_entries
    # Atomic write to assets.json
    tmp_m = project_dir / f".prop-{operation_id}-{ulid()}.tmp"
    out_data = all_assets if was_list else {"assets": all_assets}
    content = json.dumps(out_data, indent=1, ensure_ascii=False) + "\n"
    prepare_write(home, operation_id, manifest_file, hashlib.sha256(content.encode("utf-8")).hexdigest(), temporary=tmp_m)
    tmp_m.write_text(content, encoding="utf-8")
    os.replace(tmp_m, manifest_file)


def get_project_credits(conn: sqlite3.Connection, project_id: str) -> dict[str, Any]:
    """Attribution for a project, grouped by basis, from delivery-time snapshots."""
    rows = conn.execute(
        "SELECT ar.basis, a.kind, u.as_name, a.title, u.credit_snapshot, u.modifications, u.clip_actual "
        "FROM usage u "
        "JOIN asset a ON a.id = u.asset_id "
        "JOIN rights_assessment ar ON ar.id = u.rights_assessment_id "
        "WHERE u.project_id = ? AND u.removed_at IS NULL "
        "ORDER BY ar.basis, a.kind, u.created",
        (project_id,),
    ).fetchall()

    groups: dict[str, list[dict[str, Any]]] = {
        "open_license": [],
        "quotation": [],
        "own_work": [],
        "other": [],
    }

    for r in rows:
        basis = r["basis"] or "other"
        target_group = "open_license" if basis in ("license", "public_domain", "provider_license") else (
            "quotation" if basis == "quotation" else ("own_work" if basis == "own_work" else "other")
        )
        snap = json.loads(r["credit_snapshot"])
        groups[target_group].append({
            "as_name": r["as_name"],
            "kind": r["kind"],
            "title": snap.get("title") or r["title"],
            "artist": snap.get("artist"),
            "license": snap.get("license"),
            "source_url": snap.get("source_url"),
            "modifications": json.loads(r["modifications"]) if r["modifications"] else [],
        })

    # Generate formatted text
    text_lines = []
    if groups["open_license"]:
        text_lines.append("## Open Media Credits\n")
        for it in groups["open_license"]:
            parts = [it["title"]]
            if it["artist"]:
                parts.append(f"by {it['artist']}")
            if it["source_url"]:
                parts.append(f"({it['source_url']})")
            if it["license"]:
                parts.append(f"licensed under {it['license']}")
            text_lines.append("- " + " ".join(parts))
        text_lines.append("")

    if groups["quotation"]:
        text_lines.append("## On-screen Sources & Quotations\n")
        for it in groups["quotation"]:
            text_lines.append(f"- {it['title']}: {it['source_url'] or ''}")
        text_lines.append("")

    if groups["own_work"]:
        text_lines.append("## Original Work\n")
        for it in groups["own_work"]:
            text_lines.append(f"- {it['title']} ({it['as_name']})")
        text_lines.append("")

    return {
        "project_id": project_id,
        "groups": groups,
        "formatted": "\n".join(text_lines).strip(),
    }


def link(ctx: Any, args: dict[str, Any]) -> dict[str, Any]:
    """Deliver picks to a project; --plan previews, then execute by plan id."""
    b = ctx.booted
    project_arg = args.get("project")
    sel_id = args.get("selection_id")
    pick = args.get("pick")
    assets = args.get("assets")
    is_plan_only = args.get("plan", False)
    plan_id = args.get("plan_id")
    link_mode = args.get("link_mode", "auto")

    conn = db.connect(b.home.db)
    delivery_lock = None
    try:
        if plan_id:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT plan, state, result FROM operation WHERE id = ?", (plan_id,)).fetchone()
            if not row or not row["plan"]:
                raise ProproomError("plan_not_found", f"Plan id '{plan_id}' not found")
            if row["state"] == "succeeded" and row["result"]:
                conn.execute("COMMIT")
                return json.loads(row["result"])
            plan_data = json.loads(row["plan"])
            proj_id = plan_data["project_id"]
            proj_row = conn.execute("SELECT path FROM project WHERE id = ?", (proj_id,)).fetchone()
            if not proj_row:
                proj_id, proj_dir, _ = resolve_project(conn, b.home, plan_data.get("project") or str(proj_id), create=True)
            else:
                proj_dir = b.home.nas / proj_row["path"]
            op_id = plan_id
            delivery_lock = acquire_delivery_lock(b.home, op_id)
            if delivery_lock is None:
                raise ProproomError("operation_running", "Delivery is already running")
            conn.execute("UPDATE operation SET state = 'running', started = ? WHERE id = ?", (utcnow(), op_id))
            conn.execute("COMMIT")
            conn.execute("BEGIN IMMEDIATE")
            res = execute_delivery(conn, b.home, proj_dir, proj_id, plan_id, plan_data)
            ops.complete_operation(conn, b.home, plan_id, res)
            events.publish_event(conn, "linked", {"project_id": proj_id, "delivered": res.get("delivered", [])})
            conn.execute("COMMIT")
            return res

        if not project_arg:
            raise ProproomError("bad_argument", "project argument is required for link")

        proj_id, proj_dir, proj_relpath = resolve_project(
            conn, b.home, project_arg, create=not is_plan_only
        )

        conn.execute("BEGIN IMMEDIATE")
        op_id, is_new, prev_res = ops.claim_operation(
            conn, b.home, "link", args.get("idempotency_key"), args, actor=ctx.actor,
            initial_state="planned" if is_plan_only else "running"
        )
        conn.execute("COMMIT")
        if not is_new and prev_res is not None:
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
                rep = conn.execute(
                    "SELECT id FROM representation WHERE asset_id = ? ORDER BY role='original' DESC LIMIT 1",
                    (aid,),
                ).fetchone()
                if rep:
                    items_to_deliver.append({"asset_id": aid, "representation_id": rep["id"], "segment_id": None})

        if not items_to_deliver:
            raise ProproomError("no_items", "No matching items to deliver")

        plan = prepare_delivery_plan(
            conn, b.home, proj_dir, proj_id, items_to_deliver, link_mode=link_mode,
            confirm_review=args.get("confirm_review", False)
        )
        plan["project"] = proj_relpath
        conn.execute("BEGIN IMMEDIATE")
        ops.update_plan(conn, b.home, op_id, plan)
        conn.execute("COMMIT")

        if is_plan_only:
            return {"plan_id": op_id, "project": proj_relpath, **plan}

        # Record running before filesystem side effects, so a crash is recoverable.
        delivery_lock = acquire_delivery_lock(b.home, op_id)
        if delivery_lock is None:
            raise ProproomError("operation_running", "Delivery is already running")
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("UPDATE operation SET state = 'running', started = ? WHERE id = ?", (utcnow(), op_id))
        conn.execute("COMMIT")
        conn.execute("BEGIN IMMEDIATE")
        res = execute_delivery(conn, b.home, proj_dir, proj_id, op_id, plan)
        ops.complete_operation(conn, b.home, op_id, res)
        events.publish_event(conn, "linked", {"project_id": proj_id, "delivered": res.get("delivered", [])})
        conn.execute("COMMIT")
        return res
    except Exception as e:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        if "op_id" in locals() and (delivery_lock is not None or not plan_id):
            # Leave the operation running if rollback itself encounters a conflict.
            rollback_delivery(b.home, op_id)
            conn.execute("BEGIN IMMEDIATE")
            ops.fail_operation(conn, b.home, op_id, {"code": "link_failed", "message": str(e)})
            conn.execute("COMMIT")
        raise
    finally:
        conn.close()
        if delivery_lock is not None:
            delivery_lock.close()


def link_many(ctx: Any, args: dict[str, Any]) -> dict[str, Any]:
    """Batch link; per-item success/failure."""
    items = args.get("items", [])
    results = []
    for it in items:
        try:
            r = link(ctx, it)
            results.append({"status": "ok", **r})
        except Exception as e:
            results.append({"status": "error", "error": str(e), "item": it})
    return {"results": results}


def credits(ctx: Any, args: dict[str, Any]) -> dict[str, Any]:
    """Attribution for a project, grouped by basis, from delivery-time snapshots."""
    b = ctx.booted
    project_arg = args["project"]
    clean = project_arg.strip("/")
    conn = db.connect(b.home.db, readonly=True)
    try:
        proj_row = conn.execute(
            "SELECT id FROM project WHERE path = ? OR path = ? OR name = ?",
            (clean, f"project/{clean}", clean)
        ).fetchone()
        if not proj_row:
            p = (b.home.nas / clean).resolve()
            if not p.is_dir():
                p = (b.home.nas / "project" / clean).resolve()
            if p.is_dir():
                rel = str(p.relative_to(b.home.nas.resolve()))
                proj_row = conn.execute("SELECT id FROM project WHERE path = ?", (rel,)).fetchone()
        if not proj_row:
            raise ProproomError("project_not_found", f"Project '{project_arg}' not found")
        return get_project_credits(conn, proj_row["id"])
    finally:
        conn.close()

