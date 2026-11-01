"""Content-addressed blob store under <home>/blobs (sha256, immutable, reflink/copy/hardlink delivery).

Handover §5.2, §10.
"""
from __future__ import annotations

import ctypes
import hashlib
import json
import os
import shutil
import threading
from pathlib import Path
from typing import Any, Literal

from ..sdk.errors import ProproomError
from . import db, dedupe, jobs, ops
from .ids import utcnow
from .media import sniff_mime
from .paths import Home

LinkMode = Literal["reflink", "copy", "hardlink", "auto"]


def hash_file(path: Path) -> tuple[str, int]:
    """Compute sha256 hex digest and size in bytes."""
    h = hashlib.sha256()
    size = 0
    with open(path, "rb") as f:
        while chunk := f.read(1024 * 1024):
            h.update(chunk)
            size += len(chunk)
    return h.hexdigest(), size


def hash_bytes(data: bytes) -> tuple[str, int]:
    """Compute sha256 hex digest and size in bytes."""
    return hashlib.sha256(data).hexdigest(), len(data)


def blob_relpath(sha256: str) -> str:
    """Relative path under <home>: blobs/ab/abcdef..."""
    return f"blobs/{sha256[:2]}/{sha256}"


def blob_abspath(home: Home, sha256: str) -> Path:
    return home.root / blob_relpath(sha256)


def put_bytes(home: Home, data: bytes, mime: str = "application/octet-stream") -> tuple[str, int, str]:
    """Store data in the CAS blob store. Returns (sha256, bytes, relpath)."""
    sha, size = hash_bytes(data)
    target = blob_abspath(home, sha)
    if not target.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = home.dir("tmp") / f"put_{sha}_{os.getpid()}_{threading.get_ident()}"
        tmp.write_bytes(data)
        os.chmod(tmp, 0o444)  # immutable
        os.replace(tmp, target)
    return sha, size, blob_relpath(sha)


def put_file(home: Home, src_path: Path, mime: str = "application/octet-stream", *, move: bool = False) -> tuple[str, int, str]:
    """Store a file in the CAS blob store. Returns (sha256, bytes, relpath)."""
    sha, size = hash_file(src_path)
    target = blob_abspath(home, sha)
    if not target.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = home.dir("tmp") / f"put_{sha}_{os.getpid()}_{threading.get_ident()}"
        if move:
            shutil.move(src_path, tmp)
        else:
            shutil.copy2(src_path, tmp)
        os.chmod(tmp, 0o444)  # immutable
        os.replace(tmp, target)
    elif move:
        src_path.unlink(missing_ok=True)
    return sha, size, blob_relpath(sha)


def verify_blob(home: Home, sha256: str) -> str:
    """Check if the blob exists and its sha256 matches. Returns 'ok' | 'mismatch' | 'missing'."""
    p = blob_abspath(home, sha256)
    if not p.is_file():
        return "missing"
    actual_sha, _ = hash_file(p)
    return "ok" if actual_sha == sha256 else "mismatch"


def _reflink(src: Path, dst: Path) -> bool:
    """Attempt clone / reflink on supported filesystems (e.g. btrfs, XFS) using FICLONE."""
    if hasattr(os, "O_RDONLY"):
        try:
            FICLONE = 0x40049409
            import fcntl
            with open(src, "rb") as s, open(dst, "wb") as d:
                fcntl.ioctl(d.fileno(), FICLONE, s.fileno())
            return True
        except Exception:
            dst.unlink(missing_ok=True)
    return False


def link_or_copy(src: Path, dst: Path, mode: LinkMode = "auto", *, temporary: Path | None = None) -> str:
    """Deliver a file from CAS store to a destination path using reflink, copy, or hardlink.

    Returns the actual link mode used: 'reflink', 'copy', or 'hardlink'.
    """
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp_dst = temporary or dst.parent / f".tmp_{dst.name}_{os.getpid()}"
    if tmp_dst.parent != dst.parent or tmp_dst.exists() or tmp_dst.is_symlink():
        raise ProproomError("delivery_conflict", f"Temporary destination already exists or is unsafe: {tmp_dst}")

    used_mode = mode
    if mode == "hardlink":
        try:
            os.link(src, tmp_dst)
            used_mode = "hardlink"
        except OSError:
            shutil.copy2(src, tmp_dst)
            tmp_dst.chmod(tmp_dst.stat().st_mode | 0o644)
            used_mode = "copy"
    elif mode == "copy":
        shutil.copy2(src, tmp_dst)
        tmp_dst.chmod(tmp_dst.stat().st_mode | 0o644)
        used_mode = "copy"
    else:  # auto or reflink
        if _reflink(src, tmp_dst):
            used_mode = "reflink"
        else:
            shutil.copy2(src, tmp_dst)
            tmp_dst.chmod(tmp_dst.stat().st_mode | 0o644)
            used_mode = "copy"

    os.replace(tmp_dst, dst)
    return used_mode


def add(ctx: Any, args: dict[str, Any]) -> dict[str, Any]:
    """Ingest local files (own work / manual)."""
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

        policy_rules = json.loads(
            conn.execute("SELECT rules FROM policy WHERE version = ?", (b.policy_version,)).fetchone()["rules"]
        )
        added = []

        conn.execute("BEGIN IMMEDIATE")
        for p_str in paths:
            p = Path(p_str)
            if not p.is_absolute():
                p = b.home.nas / p_str
            if not p.is_file():
                continue

            mime, kind = sniff_mime(p)
            stored_sha, stored_bytes, relpath = put_file(b.home, p, mime=mime, move=False)
            dedupe.register_blob(conn, stored_sha, stored_bytes, mime, stored=True, storage_path=relpath)

            rel = p.relative_to(b.home.nas) if p.is_relative_to(b.home.nas) else p
            slug = dedupe.generate_unique_slug(conn, "manual", rel.as_posix())
            title = p.stem.replace("_", " ")

            aid, rid = dedupe.register_asset(conn, slug=slug, kind=kind, title=title)
            rep_id = dedupe.register_representation(conn, asset_id=aid, blob_sha256=stored_sha, role="original")
            prov_id = dedupe.register_provenance(conn, asset_id=aid, source="manual", external_id=str(p))
            dedupe.register_rights(
                conn,
                provenance_id=prov_id,
                asset_id=aid,
                policy_version=b.policy_version,
                policy_rules=policy_rules,
                basis=basis,
                attribution="Local User",
                created_by=ctx.actor,
            )
            dedupe.sync_search_doc(conn, rid=rid, title=title)

            added.append({"asset_id": aid, "slug": slug, "sha256": stored_sha, "representation_id": rep_id})

        result = {"added": added, "count": len(added)}
        ops.complete_operation(conn, b.home, op_id, result)
        conn.execute("COMMIT")
        return result
    finally:
        conn.close()


def verify(ctx: Any, args: dict[str, Any]) -> dict[str, Any]:
    """Recompute blob sha256 and report mismatches and the projects sharing them."""
    b = ctx.booted
    conn = db.connect(b.home.db)
    try:
        conn.execute("BEGIN IMMEDIATE")
        job_id = jobs.create_job(conn, "verify", {}, state="running")  # runs here; the worker leaves it alone
        conn.execute("COMMIT")

        rows = conn.execute("SELECT sha256 FROM blob WHERE stored = 1").fetchall()
        checked = 0
        mismatches = []
        for r in rows:
            st = verify_blob(b.home, r["sha256"])
            checked += 1
            if st != "ok":
                mismatches.append({"sha256": r["sha256"], "state": st})
            conn.execute(
                "UPDATE blob SET verify_state = ?, verified_at = ? WHERE sha256 = ?",
                (st, utcnow(), r["sha256"]),
            )

        jobs.finish_job(conn, job_id, "succeeded")
        return {
            "job_id": job_id,
            "state": "succeeded",
            "progress": {"checked": checked, "mismatches": mismatches},
        }
    finally:
        conn.close()

