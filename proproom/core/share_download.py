"""Stream the files in an already-authorized share as a ZIP without library writes."""
from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from zipfile import ZIP_STORED, ZipFile

from . import db, delivery
from .media import guess_extension
from ..sdk.errors import ProproomError

CHUNK_SIZE = 256 * 1024
CREDITS_NAME = "CREDITS.txt"


@dataclass(frozen=True)
class SharedFile:
    pos: int
    representation_id: str
    path: Path
    name: str
    mime: str
    size: int


class _Stream:
    """Unseekable ZipFile output whose buffered bytes can be drained between writes."""
    def __init__(self) -> None:
        self.buffer = bytearray()
        self.offset = 0

    def write(self, data: bytes) -> int:
        self.buffer.extend(data)
        self.offset += len(data)
        return len(data)

    def tell(self) -> int:
        return self.offset

    def flush(self) -> None:
        pass

    def drain(self) -> bytes:
        data = bytes(self.buffer)
        self.buffer.clear()
        return data


def prepare(home, share_id: str, *, positions: list[int] | None = None,
            representation_id: str | None = None) -> list[SharedFile]:
    """Resolve and validate shared blobs for both individual downloads and ZIPs."""
    if positions is not None and (not positions or len(set(positions)) != len(positions) or min(positions) < 1):
        raise ProproomError("bad_argument", "Select distinct positive item numbers")
    conn = db.connect(home.db, readonly=True)
    try:
        rows = conn.execute(
            "SELECT si.pos, si.representation_id, b.mime, b.bytes, b.stored, b.storage_path "
            "FROM share_item si LEFT JOIN representation r ON r.id = si.representation_id "
            "LEFT JOIN blob b ON b.sha256 = r.blob_sha256 WHERE si.share_id = ? ORDER BY si.pos", (share_id,)
        ).fetchall()
    finally:
        conn.close()
    if positions is not None:
        selected = set(positions)
        if not selected.issubset({row["pos"] for row in rows}):
            raise ProproomError("bad_argument", "A selected item is not in this share")
        rows = [row for row in rows if row["pos"] in selected]
    if representation_id is not None:
        rows = [row for row in rows if row["representation_id"] == representation_id][:1]
    root = home.root.resolve()
    files = []
    for row in rows:
        if not row["stored"] or not row["storage_path"]:
            raise ProproomError("share_media_missing", f"Shared item {row['pos']} has no stored file")
        path = (root / row["storage_path"]).resolve()
        if not path.is_relative_to(root) or not path.is_file():
            raise ProproomError("share_media_missing", f"Shared item {row['pos']} has no accessible stored file")
        try:
            with path.open("rb"):
                pass
        except OSError as e:
            raise ProproomError("share_media_missing", f"Shared item {row['pos']} cannot be opened: {e}") from e
        files.append(SharedFile(row["pos"], row["representation_id"], path,
                                f"{row['pos']:03d}{guess_extension(row['mime'], path)}",
                                row["mime"] or "application/octet-stream", row["bytes"]))
    return files


def credits_text(home, files: list[SharedFile]) -> str:
    """CREDITS.txt for exactly these files: the same text `prop credits` gives a project, then which file is which."""
    conn = db.connect(home.db, readonly=True)
    try:
        credits = []
        for f in files:
            row = conn.execute("SELECT asset_id FROM representation WHERE id = ?", (f.representation_id,)).fetchone()
            credits.append((f.name, delivery.asset_credit(conn, row["asset_id"])))
    finally:
        conn.close()
    groups: dict[str, list[dict]] = {"open_license": [], "quotation": [], "own_work": [], "other": []}
    for name, snap in credits:
        groups[delivery.credit_group(snap["basis"])].append({**snap, "as_name": name})
    listing = "\n".join(f"- {name}: {snap['title']}" for name, snap in credits)
    return f"{delivery.format_credits(groups)}\n\n## Files\n\n{listing}\n"


def stream(files: list[SharedFile], credits: str) -> Iterator[bytes]:
    """Produce a ZIP in bounded chunks, the files and then CREDITS.txt; a mid-stream read error aborts the download."""
    sink = _Stream()
    with ZipFile(sink, "w", compression=ZIP_STORED, allowZip64=True) as archive:
        for file in files:
            with file.path.open("rb") as source, archive.open(file.name, "w", force_zip64=True) as dest:
                while data := source.read(CHUNK_SIZE):
                    dest.write(data)
                    yield sink.drain()
            yield sink.drain()
        archive.writestr(CREDITS_NAME, credits.encode("utf-8"))
    yield sink.drain()
