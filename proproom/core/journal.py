"""Per-operation JSONL journal in <home>/journal/<op>.jsonl; replay to roll back or continue after a crash.

Handover §10.
"""
from __future__ import annotations

import json
import fcntl
import os
import shutil
import tempfile

from ..sdk.errors import ProproomError
from pathlib import Path
from typing import Any

from .ids import utcnow
from .paths import Home


def journal_path(home: Home, op_id: str) -> Path:
    return home.dir("journal") / f"{op_id}.jsonl"


def append_journal(home: Home, op_id: str, step: str, payload: dict[str, Any] | None = None) -> None:
    p = journal_path(home, op_id)
    entry = {"ts": utcnow(), "step": step, "data": payload or {}}
    line = json.dumps(entry, ensure_ascii=False) + "\n"
    with open(p, "a", encoding="utf-8") as f:
        f.write(line)
        f.flush()
        os.fsync(f.fileno())
    dir_fd = os.open(p.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


def read_journal(home: Home, op_id: str) -> list[dict[str, Any]]:
    p = journal_path(home, op_id)
    if not p.is_file():
        return []
    entries = []
    with open(p, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                entries.append(json.loads(line))
    return entries


def prepare_write(home: Home, op_id: str, path: Path, new_sha256: str, *, temporary: Path | None = None) -> None:
    """Save the old file and durably journal intent before an atomic replacement."""
    from .store import hash_file
    root = home.nas.resolve()
    path = path.absolute()
    if path != path.resolve() or not path.is_relative_to(root):
        raise ProproomError("path_traversal", f"Unsafe delivery path: {path}")
    if path.exists() and not path.is_file():
        raise ProproomError("delivery_conflict", f"Destination is not a file: {path}")
    if temporary is not None:
        if temporary.parent != path.parent or not temporary.name.startswith(f".prop-{op_id}-") or temporary.exists() or temporary.is_symlink():
            raise ProproomError("delivery_conflict", f"Unsafe temporary destination: {temporary}")
    backup = None
    old_hash = None
    old_mode = None
    old_mtime_ns = None
    if path.exists():
        stat = path.stat()
        old_mode, old_mtime_ns = stat.st_mode & 0o7777, stat.st_mtime_ns
        fd, backup_name = tempfile.mkstemp(prefix=f"{op_id}-", dir=home.dir("journal"))
        backup = Path(backup_name)
        with os.fdopen(fd, "wb") as out, path.open("rb") as src:
            shutil.copyfileobj(src, out)
            out.flush()
            os.fsync(out.fileno())
        old_hash, _ = hash_file(backup)
        dir_fd = os.open(home.dir("journal"), os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    append_journal(home, op_id, "write_intent", {
        "path": str(path.relative_to(root)), "before_sha256": old_hash,
        "after_sha256": new_sha256, "backup": backup.name if backup else None,
        "before_mode": old_mode, "before_mtime_ns": old_mtime_ns,
        "temporary": str(temporary.relative_to(root)) if temporary else None,
    })


def rollback_delivery(home: Home, op_id: str) -> int:
    """Validate the entire rollback before changing files, then undo its writes."""
    from .store import hash_file
    entries = read_journal(home, op_id)
    intents = [e["data"] for e in entries if e["step"] == "write_intent"]
    if not intents and any(e["step"] == "delivered_file" for e in entries):
        paths = [e["data"]["path"] for e in entries if e["step"] == "delivered_file" and "path" in e["data"]]
        raise ProproomError("recovery_ambiguous", "Legacy journal lacks original file backups", details={"paths": paths})
    root = home.nas.resolve()
    virtual_hashes: dict[Path, str | None] = {}
    actions: list[tuple[Path, Path | None, Path | None, dict[str, Any]]] = []
    # Simulate reverse replay to catch conflicts, including repeated writes to one path.
    for item in reversed(intents):
        path = root / item["path"]
        if not path.resolve().is_relative_to(root) or path != path.resolve() or (path.exists() and not path.is_file()):
            raise ProproomError("recovery_conflict", f"Unsafe recovery destination: {path}", details={"paths": [str(path)]})
        temporary = None
        if item.get("temporary"):
            temporary = root / item["temporary"]
            if (temporary.parent != path.parent or not temporary.name.startswith(f".prop-{op_id}-")
                    or temporary != temporary.resolve() or (temporary.exists() and not temporary.is_file())):
                raise ProproomError("recovery_conflict", f"Unsafe temporary destination: {temporary}", details={"paths": [str(temporary)]})
        if path not in virtual_hashes:
            virtual_hashes[path] = hash_file(path)[0] if path.is_file() else None
        current = virtual_hashes[path]
        before, after = item["before_sha256"], item["after_sha256"]
        backup = None
        if current != before:
            if current != after:
                raise ProproomError("recovery_conflict", f"Destination changed since delivery: {path}", details={"paths": [str(path)]})
            if before is not None:
                backup_name = item["backup"]
                if not backup_name or Path(backup_name).name != backup_name:
                    raise ProproomError("recovery_ambiguous", "Invalid journal backup path", details={"paths": [str(path)]})
                backup = home.dir("journal") / backup_name
                if not backup.is_file() or hash_file(backup)[0] != before:
                    raise ProproomError("recovery_ambiguous", f"Missing or corrupt backup: {backup}", details={"paths": [str(path), str(backup)]})
            virtual_hashes[path] = before
            actions.append((path, temporary, backup, item))
        elif temporary is not None:
            actions.append((path, temporary, None, {}))

    count = 0
    for path, temporary, backup, item in actions:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        if not item:
            continue
        if backup is None:
            path.unlink()
        else:
            fd, tmp_name = tempfile.mkstemp(prefix=".prop-restore-", dir=path.parent)
            try:
                with os.fdopen(fd, "wb") as out, backup.open("rb") as src:
                    shutil.copyfileobj(src, out)
                    out.flush()
                    os.fchmod(out.fileno(), item["before_mode"])
                    os.fsync(out.fileno())
                os.utime(tmp_name, ns=(item["before_mtime_ns"], item["before_mtime_ns"]))
                os.replace(tmp_name, path)
            finally:
                Path(tmp_name).unlink(missing_ok=True)
        count += 1
    append_journal(home, op_id, "delivery_rolled_back", {"restored": count})
    return count


def acquire_delivery_lock(home: Home, op_id: str, *, blocking: bool = False):
    """Hold across filesystem writes; OS releases it automatically after a crash."""
    if not op_id.isalnum():
        raise ProproomError("bad_argument", "Invalid operation id")
    lock = (home.dir("journal") / f"{op_id}.lock").open("a+b")
    try:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
    except BlockingIOError:
        lock.close()
        return None
    return lock
