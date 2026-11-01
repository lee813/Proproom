"""A share ZIP contains only its own files and respects the share token."""
import hashlib
import io
import os
import socket
import threading
import time
from zipfile import ZipFile

import pytest
from fastapi.testclient import TestClient

from proproom.api import http
from proproom.api.registry import REGISTRY
from proproom.core import share_download
from tests.handover.support import ingest


@pytest.fixture()
def client(lib):
    with TestClient(http.app, raise_server_exceptions=False) as c:
        yield c


def test_zip_contains_only_shared_files_without_recording_usage(client, ctx, conn, home):
    a = ingest(conn, home, slug="one", title="One", data=b"first", mime="image/jpeg")
    b = ingest(conn, home, slug="two", title="Two", data=b"second", mime="image/png")
    ingest(conn, home, slug="private", title="Private", data=b"private")
    token = REGISTRY["share"](ctx, {"assets": [a.asset_id, b.asset_id]})["token"]
    before = conn.execute("SELECT count(*) FROM usage").fetchone()[0]
    response = client.get(f"/s/{token}/download.zip")
    assert response.status_code == 200 and response.headers["content-type"] == "application/zip"
    assert "attachment" in response.headers["content-disposition"]
    with ZipFile(io.BytesIO(response.content)) as archive:
        assert archive.namelist() == ["001.jpg", "002.png", "CREDITS.txt"]
        assert archive.read("001.jpg") == b"first" and archive.read("002.png") == b"second"
        credits = archive.read("CREDITS.txt").decode()
    assert "private" not in credits.lower()
    assert conn.execute("SELECT count(*) FROM usage").fetchone()[0] == before


def test_selected_zip_and_single_file_reuse_share_membership(client, ctx, conn, home):
    a = ingest(conn, home, slug="one", title="One", data=b"first", mime="image/jpeg")
    b = ingest(conn, home, slug="two", title="Two", data=b"second", mime="image/png")
    private = ingest(conn, home, slug="private", title="Private", data=b"private")
    token = REGISTRY["share"](ctx, {"assets": [a.asset_id, b.asset_id]})["token"]
    single = client.get(f"/s/{token}/media/{b.rep_id}?download=1")
    assert single.status_code == 200 and single.content == b"second"
    assert 'filename="002.png"' in single.headers["content-disposition"]
    part = client.get(f"/s/{token}/media/{a.rep_id}?download=1", headers={"Range": "bytes=1-3"})
    assert part.status_code == 206 and part.content == b"irs"
    assert "attachment" in part.headers["content-disposition"]
    assert client.get(f"/s/{token}/media/{private.rep_id}").status_code == 404
    response = client.get(f"/s/{token}/download.zip?pos=2")
    with ZipFile(io.BytesIO(response.content)) as archive:
        assert archive.namelist() == ["002.png", "CREDITS.txt"] and archive.read("002.png") == single.content
        credits = archive.read("CREDITS.txt").decode()
    assert "Two" in credits and "One" not in credits
    for query in ("pos=0", "pos=99", "pos=1&pos=99", "pos=1&pos=1"):
        assert client.get(f"/s/{token}/download.zip?{query}").status_code == 400
    response = client.get(f"/s/{token}/download.zip?pos=2&pos=1")
    with ZipFile(io.BytesIO(response.content)) as archive:
        assert archive.namelist() == ["001.jpg", "002.png", "CREDITS.txt"]


def test_credits_match_what_prop_credits_says_for_the_same_files(client, ctx, conn, home):
    a = ingest(conn, home, slug="one", title="One", attribution="Ann", license_id="cc-by-4.0")
    b = ingest(conn, home, slug="mine", title="Mine", basis="own_work", license_id=None, attribution="Me")
    c = ingest(conn, home, slug="odd", title="Odd", attribution="Stranger")
    conn.execute("DELETE FROM rights_assessment WHERE asset_id = ?", (c.asset_id,))  # no recorded basis
    token = REGISTRY["share"](ctx, {"assets": [a.asset_id, b.asset_id, c.asset_id]})["token"]
    with ZipFile(io.BytesIO(client.get(f"/s/{token}/download.zip").content)) as archive:
        text = archive.read("CREDITS.txt").decode()
    assert "- One by Ann (https://example.org/one) licensed under cc-by-4.0" in text
    assert "## Original Work" in text and "- Mine (002.png)" not in text and "- Mine (" in text
    # an asset with no rights assessment is still credited with what is known, not left out
    assert "## Other (no recorded rights basis)" in text and "- Odd by Stranger (https://example.org/odd)" in text
    assert text.endswith("- 001.jpg: One\n- 002.jpg: Mine\n- 003.jpg: Odd\n")


def test_files_are_named_from_what_they_are_not_the_stored_mime(client, ctx, conn, home):
    """Commons gives Ogg audio and video `application/ogg`; the file's own header says which it is."""
    a = ingest(conn, home, slug="ogg", title="Ogg", data=b"OggS" + b"\0" * 60, mime="application/ogg")
    b = ingest(conn, home, slug="webm", title="Webm", data=b"\x1a\x45\xdf\xa3" + b"\0" * 60,
               mime='video/webm; codecs="vp9, opus"')
    token = REGISTRY["share"](ctx, {"assets": [a.asset_id, b.asset_id]})["token"]
    with ZipFile(io.BytesIO(client.get(f"/s/{token}/download.zip").content)) as archive:
        assert archive.namelist() == ["001.ogg", "002.webm", "CREDITS.txt"]


def test_zip_rejects_invalid_revoked_and_expired_links(client, ctx, conn, home):
    a = ingest(conn, home, slug="one", title="One")
    first = REGISTRY["share"](ctx, {"assets": [a.asset_id]})["token"]
    assert client.get("/s/not-a-token/download.zip").status_code == 404
    REGISTRY["share_revoke"](ctx, {"share": first})
    assert client.get(f"/s/{first}/download.zip").status_code == 410
    second = REGISTRY["share"](ctx, {"assets": [a.asset_id]})["token"]
    conn.execute("UPDATE share SET expires = '2000-01-01T00:00:00.000Z' WHERE token_hash = ?",
                 (hashlib.sha256(second.encode()).hexdigest(),))
    assert client.get(f"/s/{second}/download.zip").status_code == 410


def test_missing_file_fails_before_sending_a_zip(client, ctx, conn, home):
    a = ingest(conn, home, slug="one", title="One")
    token = REGISTRY["share"](ctx, {"assets": [a.asset_id]})["token"]
    rel = conn.execute("SELECT b.storage_path FROM blob b JOIN representation r ON r.blob_sha256 = b.sha256 "
                       "WHERE r.id = ?", (a.rep_id,)).fetchone()[0]
    (home.root / rel).unlink()
    response = client.get(f"/s/{token}/download.zip")
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "share_media_missing"


def test_zip_stream_is_bounded_and_supports_large_files(tmp_path):
    path = tmp_path / "large"
    path.write_bytes(b"x" * (share_download.CHUNK_SIZE * 4 + 7))
    entry = share_download.SharedFile(1, "rep", path, "001.bin", "application/octet-stream", path.stat().st_size)
    chunks = list(share_download.stream([entry], "credits"))
    assert max(map(len, chunks)) < share_download.CHUNK_SIZE + 1024
    with ZipFile(io.BytesIO(b"".join(chunks))) as archive:
        assert archive.read("001.bin") == path.read_bytes() and archive.read("CREDITS.txt") == b"credits"


@pytest.mark.skipif(os.environ.get("PROP_CAPTURE_BROWSER_TEST") != "1", reason="requires browser test image")
def test_download_button_works_in_a_browser(lib, ctx, conn, home):
    import uvicorn
    from playwright.sync_api import sync_playwright

    asset = ingest(conn, home, slug="one", title="One", data=b"original", mime="image/jpeg")
    other = ingest(conn, home, slug="two", title="Two", data=b"other", mime="image/png")
    token = REGISTRY["share"](ctx, {"assets": [asset.asset_id, other.asset_id]})["token"]
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    server = uvicorn.Server(uvicorn.Config(http.app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        for _ in range(100):
            if server.started:
                break
            time.sleep(0.05)
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            page = browser.new_page(accept_downloads=True)
            page.goto(f"http://127.0.0.1:{port}/s/{token}")
            button = page.get_by_role("link", name="Download all (ZIP)")
            button.wait_for(state="visible")
            with page.expect_download() as info:
                button.click()
            with ZipFile(info.value.path()) as archive:
                assert archive.namelist() == ["001.jpg", "002.png", "CREDITS.txt"]
            select = page.get_by_role("button", name="Download selected (ZIP)")
            assert select.is_disabled()
            page.get_by_role("checkbox", name="Select #2").check()
            with page.expect_download() as info:
                select.click()
            with ZipFile(info.value.path()) as archive:
                assert archive.namelist() == ["002.png", "CREDITS.txt"] and archive.read("002.png") == b"other"
            with page.expect_download() as info:
                page.get_by_role("link", name="Download file #1").click()
            assert info.value.suggested_filename == "001.jpg"
            assert info.value.path().read_bytes() == b"original"
            browser.close()
    finally:
        server.should_exit = True
        thread.join(10)
