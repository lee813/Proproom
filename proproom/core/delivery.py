"""Delivery: plan, temp file in target dir, manifest patch, atomic rename, usage rows; clip/fast/exact cuts.

Handover §10.
"""
from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from typing import Any

from ..sdk.errors import ProproomError
from . import store
from .ids import ulid, utcnow
from .journal import append_journal
from .paths import Home


def resolve_project(conn: sqlite3.Connection, home: Home, project_arg: str, manifest_format: str = "assets_json") -> tuple[str, Path, str]:
    """Resolve project id and directory path under /nas."""
    nas = home.nas
    clean = project_arg.strip("/")
    # Try as relative to /nas
    proj_dir = nas / clean
    if not proj_dir.is_dir():
        # Try checking if clean is in project/
        alt = nas / "project" / clean
        if alt.is_dir():
            proj_dir = alt
            clean = f"project/{clean}"
        else:
            proj_dir.mkdir(parents=True, exist_ok=True)

    relpath = str(proj_dir.relative_to(nas))
    name = proj_dir.name

    now = utcnow()
    row = conn.execute("SELECT id, name, manifest_format FROM project WHERE path = ?", (relpath,)).fetchone()
    if row:
        return row["id"], proj_dir, relpath

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

        item_obs = json.loads(ass["obligations"]) if ass["obligations"] else []
        obligations.update(item_obs)

        # File extension
        mime = blob["mime"] or "image/jpeg"
        ext = ".jpg"
        if "png" in mime:
            ext = ".png"
        elif "webp" in mime:
            ext = ".webp"
        elif "mp4" in mime:
            ext = ".mp4"
        elif "webm" in mime:
            ext = ".webm"
        elif "mp3" in mime or "audio" in mime:
            ext = ".mp3"

        rel_dest = f"assets/{as_name}{ext}"
        abs_dest = project_dir / rel_dest
        if abs_dest.exists():
            conflicts.append({"relpath": rel_dest, "exists": True})

        prov = conn.execute("SELECT * FROM provenance WHERE asset_id = ? ORDER BY retrieved DESC LIMIT 1", (aid,)).fetchone()
        ev = conn.execute("SELECT * FROM rights_evidence WHERE provenance_id = ? ORDER BY created DESC LIMIT 1", (prov["id"],)).fetchone() if prov else None

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

        used_mode = store.link_or_copy(blob_path, abs_dest, mode=item.get("link_mode", "auto"))
        actual_sha, _ = store.hash_file(abs_dest)

        now = utcnow()
        usage_id = ulid()

        conn.execute(
            "INSERT INTO usage(id, project_id, operation_id, asset_id, representation_id, segment_id, "
            "as_name, clip_requested, clip_actual, modifications, credit_snapshot, rights_assessment_id, "
            "policy_version, delivered_relpath, delivered_sha256, link_mode, origin, created) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'link', ?) "
            "ON CONFLICT(project_id, as_name) WHERE removed_at IS NULL DO UPDATE SET "
            "asset_id = excluded.asset_id, representation_id = excluded.representation_id, "
            "delivered_sha256 = excluded.delivered_sha256, created = excluded.created",
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
    _update_manifest(conn, project_dir, project_id, manifest_format)

    return {
        "project_id": project_id,
        "delivered": delivered,
        "obligations": plan["obligations"],
    }


def _update_manifest(conn: sqlite3.Connection, project_dir: Path, project_id: str, manifest_format: str) -> None:
    """Update project's assets.json manifest."""
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

    manifest_assets = []
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
        manifest_assets.append(item)

    # Atomic write to assets.json
    tmp_m = project_dir / f".tmp_assets_{os.getpid()}.json"
    tmp_m.write_text(json.dumps({"assets": manifest_assets}, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
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
