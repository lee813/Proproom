"""Project importer pipeline (handover §13).

Imports a project's media into CAS, provenance, rights and usage records. Entries listed in assets.json (or, when a
project has none, in the older `assets/manifest.json`) bring their source and author and back-fill usage; media files
under `assets/` and `images/` that no list covers come in as the user's own files, without usage rows, so a later
`link` doesn't write them into the user's assets.json.
"""
from __future__ import annotations

import json
import urllib.parse
from pathlib import Path
from typing import Any

from . import db, dedupe, delivery, jobs, ops, store
from .ids import ulid, utcnow
from .media import is_media_file, normalize_kind, sniff_mime

MEDIA_DIRS = ("assets", "images")


def _source(item: dict[str, Any], as_name: str) -> tuple[str, str]:
    src_val = item.get("source") or item.get("download") or as_name
    if str(src_val).startswith(("http://", "https://")):
        host = (urllib.parse.urlparse(str(src_val)).hostname or "").lower()
        if host == "commons.wikimedia.org" or host.endswith(".wikimedia.org"):
            return "commons", str(src_val)
        if host == "archive.org" or host.endswith(".archive.org"):
            return "archive_org", str(src_val)
        if host == "wikidata.org" or host.endswith(".wikidata.org"):
            return "wikidata", str(src_val)
        return host or "web", str(src_val)
    return "import", str(src_val)


def _manifest_items(proj_dir: Path) -> list[dict[str, Any]]:
    """Entries of an older `assets/manifest.json` ({filename, url, source, author, usage}) in assets.json's shape."""
    path = proj_dir / "assets" / "manifest.json"
    if not path.is_file():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        return []
    items = []
    for entry in data:
        if isinstance(entry, dict) and entry.get("filename"):
            items.append({"id": Path(entry["filename"]).stem, "file": f"assets/{entry['filename']}",
                          "source": entry.get("source") or entry.get("url") or "", "artist": entry.get("author") or ""})
    return items


def _credit(item: dict[str, Any]) -> str | None:
    """Who made it, as the list says. `Unknown` is how `link` once wrote 'not recorded', so it counts as no answer."""
    for key in ("artist", "author", "credit"):
        value = str(item.get(key) or "").strip()
        if value and value.lower() != "unknown" and not value.lower().startswith("unknown /"):
            return value
    return None


def _basis(lic_str: str, *, sourced: bool = False) -> tuple[str, str | None]:
    lic_lower = lic_str.lower()
    if "public domain" in lic_lower or "public_domain" in lic_lower or lic_lower in ("pd", "cc0", "cc0-1.0", "cc-zero"):
        return "public_domain", "cc0" if "cc0" in lic_lower else None
    if "quote" in lic_lower or "quotation" in lic_lower:
        return "quotation", None
    if lic_str and not ("own work" in lic_lower or "own_work" in lic_lower):
        return "license", None
    # Taken from somewhere else but no license recorded: usable, with the basis left open. Only a file with no known
    # source is the user's own, as with `prop add`.
    return ("unknown", None) if sourced else ("own_work", None)


def _ingest(conn: Any, b: Any, *, proj_id: str, proj_dir: Path, proj_relpath: str, op_id: str, as_name: str,
            item: dict[str, Any], abs_file: Path, rel_file: str, listed: bool) -> dict[str, Any] | None:
    """Store one file and record it. Returns the item's entry for `blocked` when the rights check didn't allow it,
    or {"skipped": reason} when the file is already in the library (unlisted files only)."""
    mime, inferred_kind = sniff_mime(abs_file, rel_file)
    kind = normalize_kind(item.get("kind"), inferred_kind)

    # Copied into the CAS before the transaction: a large video takes seconds, and holding the write lock that long
    # makes every other writer fail with `database is locked`.
    stored_sha, stored_bytes, blob_path = store.put_file(b.home, abs_file, mime=mime, move=False)

    # One short transaction per item. A failure stops the import; items already committed stay, and running it
    # again skips them.
    conn.execute("BEGIN IMMEDIATE")
    try:
        dedupe.register_blob(conn, stored_sha, stored_bytes, mime, stored=True, storage_path=blob_path)

        existing_rep = conn.execute(
            "SELECT asset_id, id FROM representation WHERE blob_sha256 = ? LIMIT 1", (stored_sha,)
        ).fetchone()
        if existing_rep and not listed:
            conn.execute("COMMIT")
            return {"skipped": "already_in_library"}
        if existing_rep:
            aid = existing_rep["asset_id"]
            rid_row = conn.execute("SELECT rid, title FROM asset WHERE id = ?", (aid,)).fetchone()
            rid, title = rid_row["rid"], rid_row["title"]
            rep_id = existing_rep["id"]
        else:
            slug = dedupe.generate_unique_slug(conn, proj_dir.name, as_name)
            title = item.get("desc") or item.get("title") or as_name.replace("_", " ")
            aid, rid = dedupe.register_asset(conn, slug=slug, kind=kind, title=title)
            size = item.get("size")
            w, h = (size[0], size[1]) if isinstance(size, (list, tuple)) and len(size) >= 2 else (None, None)
            rep_id = dedupe.register_representation(
                conn, asset_id=aid, blob_sha256=stored_sha, role="original",
                width=w, height=h, duration=item.get("duration")
            )

        if listed:
            src_name, ext_id = _source(item, as_name)
            src_url = ext_id if ext_id.startswith(("http://", "https://")) else None
        else:
            src_name, ext_id, src_url = "import", f"{proj_relpath}/{rel_file}", None

        # Disambiguate if this (source, external_id) already belongs to a different asset
        prov_conflict = conn.execute(
            "SELECT asset_id FROM provenance WHERE source = ? AND external_id = ?", (src_name, ext_id),
        ).fetchone()
        if prov_conflict and prov_conflict["asset_id"] != aid:
            ext_id = f"{ext_id}#{as_name}"
            prov_conflict2 = conn.execute(
                "SELECT asset_id FROM provenance WHERE source = ? AND external_id = ?", (src_name, ext_id),
            ).fetchone()
            if prov_conflict2 and prov_conflict2["asset_id"] != aid:
                ext_id = f"{ext_id}-{aid[:6]}"

        prov_id = dedupe.register_provenance(conn, asset_id=aid, source=src_name, external_id=ext_id,
                                             source_url=src_url)

        lic_str = item.get("license") or item.get("license_url") or ""
        basis, pd_reason = _basis(lic_str, sourced=listed and bool(item.get("source") or item.get("download")))
        attribution = _credit(item)
        policy_row = conn.execute("SELECT rules FROM policy WHERE version = ?", (b.policy_version,)).fetchone()
        _, ass_id = dedupe.register_rights(
            conn, provenance_id=prov_id, asset_id=aid, policy_version=b.policy_version,
            policy_rules=json.loads(policy_row["rules"]) if policy_row else {}, basis=basis,
            license_id=lic_str or None, pd_reason=pd_reason,
            attribution=attribution, created_by="import_project"
        )

        dedupe.sync_search_doc(conn, rid=rid, title=title, description=item.get("desc", ""), artist=attribution or "")

        if listed:
            conn.execute(
                "INSERT INTO usage(id, project_id, operation_id, asset_id, representation_id, as_name, "
                "credit_snapshot, rights_assessment_id, policy_version, delivered_relpath, delivered_sha256, link_mode, origin, created) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'copy', 'import', ?) "
                "ON CONFLICT(project_id, as_name) WHERE removed_at IS NULL DO NOTHING",
                (ulid(), proj_id, op_id, aid, rep_id, as_name, json.dumps(item), ass_id, b.policy_version, rel_file,
                 stored_sha, utcnow()),
            )
        assessment = conn.execute("SELECT decision, reasons FROM rights_assessment WHERE id = ?",
                                  (ass_id,)).fetchone()
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    if assessment["decision"] == "allow":
        return None
    return {"as_name": as_name, "file": rel_file, "asset_id": aid, "decision": assessment["decision"],
            "reasons": [r["code"] for r in json.loads(assessment["reasons"])]}


def import_project(ctx: Any, args: dict[str, Any]) -> dict[str, Any]:
    """Import a project's media, back-fill usage for what assets.json lists, and report what rights blocked."""
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

        imported, skipped, blocked, unlisted = 0, [], [], 0
        try:
            manifest_file = proj_dir / "assets.json"
            assets_list: list[Any] = []
            if manifest_file.is_file():
                manifest_data = json.loads(manifest_file.read_text(encoding="utf-8"))
                if isinstance(manifest_data, list):
                    assets_list = manifest_data
                elif isinstance(manifest_data, dict):
                    assets_list = manifest_data.get("assets", [])
            else:
                assets_list = _manifest_items(proj_dir)

            listed_files: set[str] = set()
            for item in assets_list:
                as_name = item.get("id") or Path(item.get("path") or item.get("file") or item.get("original") or "").stem
                if not as_name:
                    skipped.append({"entry": item, "reason": "no_id_or_path"})
                    continue

                candidate_paths = [
                    item.get("file"),
                    item.get("original"),
                    item.get("path"),
                    f"assets/{item.get('path')}" if item.get("path") else None,
                    f"assets/{Path(item.get('path', '')).name}" if item.get("path") else None,
                    f"images/{Path(item.get('path', '')).name}" if item.get("path") else None,
                ]
                abs_file, rel_file = None, None
                for cp in candidate_paths:
                    if cp and (proj_dir / cp).is_file():
                        abs_file, rel_file = proj_dir / cp, cp
                        break
                if abs_file:
                    listed_files.add(abs_file.resolve().as_posix())

                # Idempotency check: if already imported in this project, skip
                if conn.execute(
                    "SELECT id FROM usage WHERE project_id = ? AND as_name = ? AND removed_at IS NULL",
                    (proj_id, as_name),
                ).fetchone():
                    skipped.append({"as_name": as_name, "reason": "already_imported"})
                    continue
                if not abs_file or not rel_file:
                    skipped.append({"as_name": as_name, "reason": "file_not_found",
                                    "tried": [cp for cp in candidate_paths if cp]})
                    continue

                held = _ingest(conn, b, proj_id=proj_id, proj_dir=proj_dir, proj_relpath=proj_relpath, op_id=op_id,
                               as_name=as_name, item=item, abs_file=abs_file, rel_file=rel_file, listed=True)
                imported += 1
                if held:
                    blocked.append(held)

            for d in MEDIA_DIRS:
                if not (proj_dir / d).is_dir():
                    continue
                for f in sorted((proj_dir / d).rglob("*")):
                    rel = f.relative_to(proj_dir)
                    if (not f.is_file() or not is_media_file(f) or f.resolve().as_posix() in listed_files
                            or any(part.startswith(".") for part in rel.parts)):
                        continue
                    held = _ingest(conn, b, proj_id=proj_id, proj_dir=proj_dir, proj_relpath=proj_relpath,
                                   op_id=op_id, as_name=f.stem, item={}, abs_file=f, rel_file=rel.as_posix(),
                                   listed=False)
                    if held and "skipped" in held:
                        skipped.append({"file": rel.as_posix(), "reason": held["skipped"]})
                        continue
                    imported += 1
                    unlisted += 1
                    if held:
                        blocked.append(held)

            progress = {"imported": imported, "not_in_assets_json": unlisted, "skipped": len(skipped),
                        "skipped_items": skipped[:50], "blocked": len(blocked), "blocked_items": blocked[:50]}
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
