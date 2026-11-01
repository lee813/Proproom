"""Project importer pipeline (handover §13).

Imports existing assets.json and local asset files into CAS, provenance, rights,
and usage records.
"""
from __future__ import annotations

import json
import urllib.parse
from pathlib import Path
from typing import Any

from . import db, dedupe, delivery, jobs, ops, store
from .ids import ulid, utcnow
from .media import normalize_kind, sniff_mime


def import_project(ctx: Any, args: dict[str, Any]) -> dict[str, Any]:
    """Import a project's existing assets.json and back-fill usage."""
    b = ctx.booted
    project_arg = args["project"]

    conn = db.connect(b.home.db)
    try:
        conn.execute("BEGIN IMMEDIATE")
        op_id, _, _ = ops.claim_operation(
            conn, b.home, "import_project", args.get("idempotency_key"), args, actor=ctx.actor
        )
        proj_id, proj_dir, proj_relpath = delivery.resolve_project(conn, b.home, project_arg)
        # Created `running`: this call owns the job, so the worker never picks it up (worker.STEPPED_KINDS).
        job_id = jobs.create_job(conn, "import_project", {"project": proj_relpath}, operation_id=op_id,
                                 state="running")
        conn.execute("COMMIT")

        manifest_file = proj_dir / "assets.json"
        if not manifest_file.is_file():
            conn.execute("BEGIN IMMEDIATE")
            jobs.finish_job(conn, job_id, "failed", {"error": "assets.json not found"})
            conn.execute("COMMIT")
            return {"job_id": job_id, "state": "failed", "progress": {"error": "assets.json not found"}}

        manifest_raw = manifest_file.read_text(encoding="utf-8")
        manifest_data = json.loads(manifest_raw)
        if isinstance(manifest_data, list):
            assets_list = manifest_data
        elif isinstance(manifest_data, dict):
            assets_list = manifest_data.get("assets", [])
        else:
            assets_list = []

        policy_row = conn.execute("SELECT rules FROM policy WHERE version = ?", (b.policy_version,)).fetchone()
        policy_rules = json.loads(policy_row["rules"]) if policy_row else {}

        imported, skipped = 0, []
        try:
            for item in assets_list:
                as_name = item.get("id") or Path(item.get("path") or item.get("file") or item.get("original") or "").stem
                if not as_name:
                    skipped.append({"entry": item, "reason": "no_id_or_path"})
                    continue

                # Idempotency check: if already imported in this project, skip
                existing_usage = conn.execute(
                    "SELECT id FROM usage WHERE project_id = ? AND as_name = ? AND removed_at IS NULL",
                    (proj_id, as_name),
                ).fetchone()
                if existing_usage:
                    skipped.append({"as_name": as_name, "reason": "already_imported"})
                    continue

                candidate_paths = [
                    item.get("file"),
                    item.get("original"),
                    item.get("path"),
                    f"assets/{item.get('path')}" if item.get("path") else None,
                    f"assets/{Path(item.get('path', '')).name}" if item.get("path") else None,
                    f"images/{Path(item.get('path', '')).name}" if item.get("path") else None,
                ]
                abs_file = None
                rel_file = None
                for cp in candidate_paths:
                    if not cp:
                        continue
                    cand = proj_dir / cp
                    if cand.is_file():
                        abs_file = cand
                        rel_file = cp
                        break

                if not abs_file or not abs_file.is_file():
                    skipped.append({"as_name": as_name, "reason": "file_not_found",
                                    "tried": [cp for cp in candidate_paths if cp]})
                    continue

                mime, inferred_kind = sniff_mime(abs_file, rel_file)
                kind = normalize_kind(item.get("kind"), inferred_kind)

                # Copied into the CAS before the transaction: a large video takes seconds, and holding the write
                # lock that long makes every other writer fail with `database is locked`.
                stored_sha, stored_bytes, blob_path = store.put_file(b.home, abs_file, mime=mime, move=False)

                # One short transaction per item. A failure stops the import; items already committed stay, and
                # running it again skips them (`already_imported`).
                conn.execute("BEGIN IMMEDIATE")
                try:
                    dedupe.register_blob(conn, stored_sha, stored_bytes, mime, stored=True, storage_path=blob_path)

                    # Check if this exact blob already belongs to an existing asset
                    existing_rep = conn.execute(
                        "SELECT asset_id, id FROM representation WHERE blob_sha256 = ? LIMIT 1",
                        (stored_sha,)
                    ).fetchone()
                    if existing_rep:
                        aid = existing_rep["asset_id"]
                        rid_row = conn.execute("SELECT rid, slug, title FROM asset WHERE id = ?", (aid,)).fetchone()
                        rid = rid_row["rid"]
                        slug = rid_row["slug"]
                        title = rid_row["title"]
                        rep_id = existing_rep["id"]
                    else:
                        slug = dedupe.generate_unique_slug(conn, proj_dir.name, as_name)
                        title = item.get("desc") or item.get("title") or as_name
                        aid, rid = dedupe.register_asset(conn, slug=slug, kind=kind, title=title)
                        size = item.get("size")
                        if isinstance(size, (list, tuple)) and len(size) >= 2:
                            w, h = size[0], size[1]
                        else:
                            w, h = None, None
                        rep_id = dedupe.register_representation(
                            conn, asset_id=aid, blob_sha256=stored_sha, role="original",
                            width=w, height=h, duration=item.get("duration")
                        )

                    src_val = item.get("source") or item.get("download") or as_name
                    if str(src_val).startswith(("http://", "https://")):
                        parsed_src = urllib.parse.urlparse(str(src_val))
                        host = (parsed_src.hostname or "").lower()
                        if host == "commons.wikimedia.org" or host.endswith(".wikimedia.org"):
                            src_name = "commons"
                        elif host == "archive.org" or host.endswith(".archive.org"):
                            src_name = "archive_org"
                        elif host == "wikidata.org" or host.endswith(".wikidata.org"):
                            src_name = "wikidata"
                        elif host:
                            src_name = host
                        else:
                            src_name = "web"
                        ext_id = str(src_val)
                    else:
                        src_name = "import"
                        ext_id = str(src_val)

                    # Disambiguate if this (source, external_id) already belongs to a different asset
                    prov_conflict = conn.execute(
                        "SELECT asset_id FROM provenance WHERE source = ? AND external_id = ?",
                        (src_name, ext_id),
                    ).fetchone()
                    if prov_conflict and prov_conflict["asset_id"] != aid:
                        ext_id = f"{ext_id}#{as_name}"
                        prov_conflict2 = conn.execute(
                            "SELECT asset_id FROM provenance WHERE source = ? AND external_id = ?",
                            (src_name, ext_id),
                        ).fetchone()
                        if prov_conflict2 and prov_conflict2["asset_id"] != aid:
                            ext_id = f"{ext_id}-{aid[:6]}"

                    prov_id = dedupe.register_provenance(
                        conn, asset_id=aid, source=src_name,
                        external_id=ext_id, source_url=str(src_val)
                    )

                    lic_str = item.get("license") or item.get("license_url") or ""
                    lic_lower = lic_str.lower()
                    if "public domain" in lic_lower or "public_domain" in lic_lower or lic_lower in ("pd", "cc0", "cc0-1.0", "cc-zero"):
                        basis = "public_domain"
                        pd_reason = "cc0" if "cc0" in lic_lower else None
                    elif "quote" in lic_lower or "quotation" in lic_lower:
                        basis = "quotation"
                        pd_reason = None
                    elif "own work" in lic_lower or "own_work" in lic_lower:
                        basis = "own_work"
                        pd_reason = None
                    elif lic_str:
                        basis = "license"
                        pd_reason = None
                    else:
                        basis = "unknown"
                        pd_reason = None

                    attribution = item.get("artist") or item.get("author") or item.get("credit") or "Unknown"

                    ev_id, ass_id = dedupe.register_rights(
                        conn, provenance_id=prov_id, asset_id=aid, policy_version=b.policy_version,
                        policy_rules=policy_rules, basis=basis,
                        license_id=lic_str or None,
                        pd_reason=pd_reason,
                        attribution=attribution, created_by="import_project"
                    )

                    dedupe.sync_search_doc(conn, rid=rid, title=title, description=item.get("desc", ""), artist=attribution)

                    # Backfill usage row
                    usage_id = ulid()
                    conn.execute(
                        "INSERT INTO usage(id, project_id, operation_id, asset_id, representation_id, as_name, "
                        "credit_snapshot, rights_assessment_id, policy_version, delivered_relpath, delivered_sha256, link_mode, origin, created) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'copy', 'import', ?) "
                        "ON CONFLICT(project_id, as_name) WHERE removed_at IS NULL DO NOTHING",
                        (usage_id, proj_id, op_id, aid, rep_id, as_name, json.dumps(item), ass_id, b.policy_version, rel_file, stored_sha, utcnow()),
                    )
                    conn.execute("COMMIT")
                except BaseException:
                    conn.execute("ROLLBACK")
                    raise
                imported += 1

            progress = {"imported": imported, "skipped": len(skipped), "skipped_items": skipped[:50]}
            res = {"job_id": job_id, "state": "succeeded", "progress": progress}
            conn.execute("BEGIN IMMEDIATE")
            jobs.update_job_progress(conn, job_id, progress)
            jobs.finish_job(conn, job_id, "succeeded")
            ops.complete_operation(conn, b.home, op_id, res)
            conn.execute("COMMIT")
            return res
        except Exception as e:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            conn.execute("BEGIN IMMEDIATE")
            jobs.finish_job(conn, job_id, "failed", {"error": str(e)})
            conn.execute("COMMIT")
            raise
    finally:
        conn.close()
