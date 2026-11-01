"""Deduplication and asset registration (handover §5.2).

Rules:
  - Blobs are deduplicated by sha256.
  - Representations represent concrete file variants of an asset (original, thumb, vp9, etc.).
  - Provenances record external origins. Same (source, external_id, revision) links to same asset.
  - Search doc is kept up to date in the same transaction.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from . import cjk, rights, store
from .ids import ulid, utcnow, utcnow_monotonic
from .paths import Home
from ..sdk.errors import ProproomError


def register_blob(
    conn: sqlite3.Connection,
    sha256: str,
    size_bytes: int,
    mime: str,
    stored: bool = True,
    storage_path: str | None = None,
) -> None:
    now = utcnow()
    conn.execute(
        "INSERT INTO blob(sha256, bytes, mime, stored, storage_path, created, verify_state) "
        "VALUES (?, ?, ?, ?, ?, ?, 'ok') "
        "ON CONFLICT(sha256) DO UPDATE SET "
        "stored = max(stored, excluded.stored), "
        "storage_path = coalesce(storage_path, excluded.storage_path)",
        (sha256, size_bytes, mime, 1 if stored else 0, storage_path, now),
    )


def generate_unique_slug(conn: sqlite3.Connection, prefix: str, text: str, max_len: int = 50) -> str:
    """Generate a clean, collision-free slug (AGENTS.md: one slug generator)."""
    clean_text = str(text).lower().replace("file:", "").replace(" ", "-")
    combined = f"{prefix}-{clean_text}" if prefix else clean_text
    base = "".join(c for c in combined if c.isalnum() or c in "-_")[:max_len].strip("-_")
    if not base:
        base = "asset"
    slug = base
    counter = 1
    cutoff = max(10, max_len - 6)
    while conn.execute(
        "SELECT 1 FROM asset WHERE slug = ? UNION SELECT 1 FROM asset_slug_history WHERE slug = ?",
        (slug, slug),
    ).fetchone():
        slug = f"{base[:cutoff]}-{counter}"
        counter += 1
    return slug


def register_asset(
    conn: sqlite3.Connection,
    slug: str,
    kind: str,
    title: str,
    description: str | None = None,
    synthetic: bool = False,
    generator: dict[str, Any] | None = None,
    rating: int | None = None,
    asset_id: str | None = None,
) -> tuple[str, int]:
    """Register an asset if not exists, or return existing (asset_id, rid)."""
    # Check if slug exists in asset or asset_slug_history
    row = conn.execute("SELECT id, rid FROM asset WHERE slug = ?", (slug,)).fetchone()
    if row:
        return row["id"], row["rid"]

    hist = conn.execute("SELECT asset_id FROM asset_slug_history WHERE slug = ?", (slug,)).fetchone()
    if hist:
        aid = hist["asset_id"]
        rid_row = conn.execute("SELECT rid FROM asset WHERE id = ?", (aid,)).fetchone()
        return aid, rid_row["rid"]

    now = utcnow()
    aid = asset_id or ulid()
    # Find next available rid (integer rowid)
    max_rid = conn.execute("SELECT coalesce(max(rid), 0) FROM asset").fetchone()[0]
    rid = max_rid + 1
    gen_json = json.dumps(generator) if generator else None

    conn.execute(
        "INSERT INTO asset(id, rid, slug, kind, title, description, synthetic, generator, rating, status, created, updated) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?)",
        (aid, rid, slug, kind, title, description, 1 if synthetic else 0, gen_json, rating, now, now),
    )
    return aid, rid


def register_representation(
    conn: sqlite3.Connection,
    asset_id: str,
    blob_sha256: str,
    role: str = "original",
    width: int | None = None,
    height: int | None = None,
    duration: float | None = None,
    fps: float | None = None,
    codec: str | None = None,
    phash: str | None = None,
    max_display_px: int | None = None,
    derived_from: str | None = None,
    derivation: dict[str, Any] | None = None,
) -> str:
    """Register a representation. Returns representation_id."""
    row = conn.execute(
        "SELECT id FROM representation WHERE asset_id = ? AND blob_sha256 = ? AND role = ?",
        (asset_id, blob_sha256, role),
    ).fetchone()
    if row:
        return row["id"]

    now = utcnow()
    rep_id = ulid()
    deriv_json = json.dumps(derivation) if derivation else None
    conn.execute(
        "INSERT INTO representation(id, asset_id, blob_sha256, role, width, height, duration, fps, codec, "
        "phash, max_display_px, derived_from, derivation, created) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (rep_id, asset_id, blob_sha256, role, width, height, duration, fps, codec, phash, max_display_px,
         derived_from, deriv_json, now),
    )
    return rep_id


def register_provenance(
    conn: sqlite3.Connection,
    asset_id: str,
    source: str,
    external_id: str,
    revision: str = "",
    source_url: str | None = None,
    final_url: str | None = None,
    publisher: str | None = None,
    publisher_type: str | None = None,
    raw_metadata: dict[str, Any] | None = None,
    snapshot_sha256: str | None = None,
    plugin: str = "core",
    plugin_version: str = "0.0.1",
    plugin_hash: str = "0" * 64,
) -> str:
    """Register provenance entry. Returns provenance_id."""
    row = conn.execute(
        "SELECT id, asset_id FROM provenance WHERE source = ? AND external_id = ? AND revision = ?",
        (source, external_id, revision),
    ).fetchone()
    if row:
        if row["asset_id"] == asset_id:
            return row["id"]
        raise ProproomError(
            "provenance_conflict",
            f"Provenance entry ({source}, {external_id}, {revision}) already belongs to asset {row['asset_id']}"
        )

    now = utcnow()
    prov_id = ulid()
    raw_json = json.dumps(raw_metadata or {}, ensure_ascii=False)
    conn.execute(
        "INSERT INTO provenance(id, asset_id, source, external_id, revision, source_url, final_url, publisher, "
        "publisher_type, retrieved, raw_metadata, snapshot_sha256, plugin, plugin_version, plugin_hash) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (prov_id, asset_id, source, external_id, revision, source_url, final_url, publisher, publisher_type,
         now, raw_json, snapshot_sha256, plugin, plugin_version, plugin_hash),
    )
    return prov_id


def register_rights(
    conn: sqlite3.Connection,
    provenance_id: str,
    asset_id: str,
    policy_version: int,
    policy_rules: dict[str, Any],
    basis: str,
    license_id: str | None = None,
    license_url: str | None = None,
    pd_reason: str | None = None,
    jurisdiction: str | None = None,
    attribution: str | None = None,
    restrictions: list[str] | None = None,
    covers: str = "work",
    original_text: str | None = None,
    locator: str | None = None,
    origin: str = "source",
    created_by: str = "core",
    publisher: str | None = None,
    synthetic: bool = False,
    anchor: str | None = None,
    plugin_restrictions: list[dict[str, Any]] | None = None,
) -> tuple[str, str]:
    """Register rights evidence and compute rights assessment. Returns (evidence_id, assessment_id)."""
    now = utcnow()
    ev_id = ulid()
    norm_lic = rights.normalize_license(license_id)
    restr_json = json.dumps(restrictions or [])

    conn.execute(
        "INSERT INTO rights_evidence(id, provenance_id, basis, license_id, license_url, pd_reason, jurisdiction, "
        "attribution, restrictions, covers, original_text, locator, origin, created_by, created) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (ev_id, provenance_id, basis, norm_lic, license_url, pd_reason, jurisdiction, attribution,
         restr_json, covers, original_text, locator, origin, created_by, now),
    )

    decision, reasons, obligations = rights.evaluate_rights(
        policy_rules=policy_rules,
        basis=basis,
        license_id=norm_lic,
        pd_reason=pd_reason,
        restrictions=restrictions,
        publisher=publisher,
        synthetic=synthetic,
        anchor=anchor,
        plugin_restrictions=plugin_restrictions,
    )

    if restrictions:
        for r in restrictions:
            conn.execute(
                "INSERT INTO flag(target_type, target_id, kind, severity, evidence, origin, created) "
                "VALUES ('asset', ?, 'restriction', 'warn', ?, 'source', ?)",
                (asset_id, json.dumps({"restriction": r}), now),
            )

    ass_id = ulid()
    conn.execute(
        "INSERT INTO rights_assessment(id, asset_id, policy_version, decision, basis, reasons, evidence_ids, "
        "obligations, assessed_at, assessed_by) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (ass_id, asset_id, policy_version, decision, None if basis == "unknown" else basis,  # NULL: no basis to rest on
         json.dumps(reasons), json.dumps([ev_id]), json.dumps(obligations), utcnow_monotonic(), created_by),
    )
    # Vector rows carry the asset's decision for filtering; a new assessment must reach them too.
    for space in conn.execute("SELECT id, vec_table FROM embedding_space").fetchall():
        conn.execute(f"UPDATE {space['vec_table']} SET rights = ? WHERE rid IN "
                     "(SELECT rid FROM vec_target WHERE space_id = ? AND asset_id = ?)",
                     (decision, space["id"], asset_id))
    return ev_id, ass_id


def sync_search_doc(
    conn: sqlite3.Connection,
    rid: int,
    title: str = "",
    description: str = "",
    artist: str = "",
    tags: list[str] | None = None,
    notes: list[str] | None = None,
    aliases: list[str] | None = None,
    cjk_text: str | None = None,
) -> None:
    """Sync search_doc table for an asset (triggers fts_latin and fts_cjk)."""
    tags_str = " ".join(tags or [])
    notes_str = " ".join(notes or [])
    aliases_str = " ".join(aliases or [])

    # Extract all CJK text across all fields
    cjk_parts = []
    if cjk_text:
        cjk_parts.append(cjk_text)
    for s in (title, description, artist, tags_str, aliases_str):
        if cjk.has_cjk(s):
            cjk_parts.append(s)
    combined_cjk = " ".join(cjk_parts)

    conn.execute(
        "INSERT INTO search_doc(rid, title, description, artist, tags, notes, aliases, cjk) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(rid) DO UPDATE SET "
        "title = excluded.title, description = excluded.description, artist = excluded.artist, "
        "tags = excluded.tags, notes = excluded.notes, aliases = excluded.aliases, cjk = excluded.cjk",
        (rid, title, description or "", artist or "", tags_str, notes_str, aliases_str, combined_cjk),
    )
