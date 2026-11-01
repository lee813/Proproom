"""§9 contact sheets and share links."""
from __future__ import annotations

import datetime
import hashlib
import io
import shutil
import subprocess

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from support import gap, ingest

from proproom.api import http
from proproom.api.registry import REGISTRY
from proproom.core import sheet


def _png(color, size) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, "PNG")
    return buf.getvalue()


def test_sheet_geometry_and_letterbox(tmp_path):
    """§9: ~1600px wide, 4x3 grid, letterboxed — a 1:4 portrait keeps its top and bottom."""
    src = tmp_path / "tall.png"
    src.write_bytes(_png((255, 0, 0), (100, 400)))
    out = sheet.render_contact_sheet([sheet.SheetCell(idx=1, image_path=src, title="tall")], tmp_path / "s.jpg")
    img = Image.open(out).convert("RGB")
    assert img.width == 1600 and img.height == 3 * (1600 // 4) * 3 // 4
    cell_w, cell_h = 400, 300
    column = [img.getpixel((cell_w // 2, y)) for y in range(cell_h)]
    red_rows = [y for y, (r, g, b) in enumerate(column) if r > 200 and g < 60 and b < 60]
    # the whole image height is visible (scaled to the thumb box), not centre-cropped to the cell aspect
    assert red_rows and red_rows[-1] - red_rows[0] > 0.6 * cell_h


def test_search_sheet_is_bound_to_the_selection(ctx, conn, home):
    ingest(conn, home, slug="p", title="parliament", data=_png((0, 0, 255), (800, 600)), mime="image/png")
    res = REGISTRY["search"](ctx, {"query": "parliament", "sheet": True})
    sha = conn.execute("SELECT sheet_sha256 FROM selection WHERE id = ?", (res["selection_id"],)).fetchone()[0]
    assert sha and res["sheet"].endswith(sha)


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg not installed")
def test_video_cells_show_frames(tmp_path):
    vid = tmp_path / "red.webm"
    subprocess.run(["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "color=red:s=320x240:d=1",
                    "-c:v", "libvpx-vp9", str(vid)], check=True)
    out = sheet.render_contact_sheet([sheet.SheetCell(idx=1, image_path=vid, kind="footage", duration=1.0)],
                                     tmp_path / "s.jpg")
    r, g, b = Image.open(out).convert("RGB").getpixel((200, 140))
    assert r > 200 and g < 60


@pytest.fixture()
def client(lib):
    with TestClient(http.app, raise_server_exceptions=False) as c:
        yield c


@pytest.fixture()
def shared(ctx, conn, home):
    a = ingest(conn, home, slug="s", title="Shared", data=b"0123456789abcdef", mime="image/jpeg")
    b = ingest(conn, home, slug="other", title="Not shared")
    res = REGISTRY["share"](ctx, {"assets": [a.asset_id], "title": "Pick"})
    return res, a, b


def test_share_stores_only_the_token_hash(conn, shared):
    res, _, _ = shared
    rows = conn.execute("SELECT token_hash FROM share").fetchall()
    assert [r[0] for r in rows] == [hashlib.sha256(res["token"].encode()).hexdigest()]
    assert res["token"] not in str([tuple(r) for r in conn.execute("SELECT * FROM share")])


def test_share_media_requires_membership(client, shared):
    res, a, b = shared
    t = res["token"]
    assert client.get(f"/s/{t}/media/{a.rep_id}", headers={"Range": "bytes=0-3"}).content == b"0123"
    assert client.get(f"/s/{t}/media/{b.rep_id}").status_code == 404
    assert client.get(f"/s/not-a-token/media/{a.rep_id}").status_code == 404


def test_revoked_and_expired_links_stop_working(client, conn, ctx, shared):
    res, a, _ = shared
    REGISTRY["share_revoke"](ctx, {"share": res["token"]})
    assert client.get(f"/s/{res['token']}/media/{a.rep_id}").status_code == 410
    res2 = REGISTRY["share"](ctx, {"assets": [a.asset_id]})
    conn.execute("UPDATE share SET expires = '2000-01-01T00:00:00.000Z' WHERE token_hash = ?",
                 (hashlib.sha256(res2["token"].encode()).hexdigest(),))
    assert client.get(f"/s/{res2['token']}/items").status_code == 410


def test_feedback_round_trip(client, ctx, shared):
    res, _, _ = shared
    r = client.post(f"/s/{res['token']}/feedback", json={"pos": 1, "verdict": "reject", "comment": "too dark"})
    assert r.status_code == 200
    fb = REGISTRY["share_feedback"](ctx, {"share": res["token"]})["feedback"]
    assert [(f["pos"], f["verdict"], f["comment"]) for f in fb] == [(1, "reject", "too dark")]


def test_share_expiry_argument(ctx, shared):
    _, a, _ = shared
    res = REGISTRY["share"](ctx, {"assets": [a.asset_id], "expires": "7d"})
    exp = datetime.datetime.fromisoformat(res["expires"].replace("Z", "+00:00"))
    days = (exp - datetime.datetime.now(datetime.timezone.utc)).days
    assert 6 <= days <= 7


def test_suffix_range_request(client, shared):
    res, a, _ = shared
    r = client.get(f"/s/{res['token']}/media/{a.rep_id}", headers={"Range": "bytes=-4"})
    assert r.status_code == 206 and r.content == b"cdef"


def test_invalid_feedback_is_a_client_error(client, shared):
    res, _, _ = shared
    assert client.post(f"/s/{res['token']}/feedback", json={"pos": 1, "verdict": "maybe"}).status_code in (400, 422)
    assert client.post(f"/s/{res['token']}/feedback", json={"pos": 99, "verdict": "want"}).status_code in (400, 404)
