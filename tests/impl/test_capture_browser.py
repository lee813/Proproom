"""Capture end-to-end against a local HTTP fixture with real Chromium."""
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from PIL import Image
from proproom.api.registry import REGISTRY
from proproom.core import db, store
from proproom.sdk.errors import ProproomError

pytestmark = pytest.mark.skipif(os.environ.get("PROP_CAPTURE_BROWSER_TEST") != "1", reason="requires browser test image")


@pytest.fixture
def website():
    requests = []
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append((self.path, self.headers.get("Host")))
            if self.path == "/rate" and sum(path == "/rate" for path, _ in requests) == 1:
                self.send_response(429)
                self.send_header("Retry-After", "0")
                self.end_headers()
                return
            if self.path == "/redirect":
                self.send_response(302)
                self.send_header("Location", "/page")
                self.end_headers()
                return
            if self.path == "/escape":
                self.send_response(302)
                self.send_header("Location", f"http://localhost:{self.server.server_port}/blocked")
                self.end_headers()
                return
            license_id = "by-nc" if self.path == "/denied" else "by"
            change = str(sum(path == "/changing" for path, _ in requests)) if self.path == "/changing" else "stable"
            license_link = "" if self.path == "/unlicensed" else f'<link rel="license" href="https://creativecommons.org/licenses/{license_id}/4.0/">'
            anchor = f'<a rel="license" href="https://creativecommons.org/licenses/{license_id}/4.0/">Licence</a>' if self.path == "/anchor" else ""
            if anchor:
                license_link = ""
            content = f'''<!doctype html><html><head><title>Official results</title>
              <meta property="og:site_name" content="Civic Office">
              {license_link}
              </head><body><div role="dialog" id="cookies">Cookies <button onclick="this.parentNode.remove()">Reject all</button></div>
              <main style="height:1500px"><h1>Results overview</h1><p>Public release {change}</p>{anchor}</main></body></html>'''.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def configure(home):
    text = home.config.read_text()
    home.config.write_text(text.replace('allowed_domains = []', 'allowed_domains = ["127.0.0.1"]'))


def test_capture_fetch_produces_screenshot_and_snapshot(ctx, home, website):
    configure(home)
    base, requests = website
    result = REGISTRY["fetch"](ctx, {"source": "capture", "external_id": base + "/redirect", "options": {
        "viewport_width": 430, "viewport_height": 600, "device_scale_factor": 2,
        "scroll_text": "Public release", "highlight": {"x": 10, "y": 20, "width": 100, "height": 40}}})
    image_path = store.blob_abspath(home, result["sha256"])
    with Image.open(image_path) as image:
        assert image.size == (860, 1200)
    conn = db.connect(home.db)
    row = conn.execute("SELECT source_url, final_url, publisher, raw_metadata, snapshot_sha256 FROM provenance WHERE asset_id = ?", (result["asset_id"],)).fetchone()
    assert row["source_url"] == base + "/redirect" and row["final_url"] == base + "/page"
    assert row["publisher"] == "Civic Office"
    # One inspection plus one capture; screenshot and snapshot share that capture.
    assert sum(path == "/page" for path, host in requests) == 2
    assert json.loads(row["raw_metadata"])["capture_options"]["highlight"]["width"] == 100
    assert b"Official results" in store.blob_abspath(home, row["snapshot_sha256"]).read_bytes()
    ev = conn.execute("SELECT basis, license_id, locator FROM rights_evidence WHERE provenance_id = (SELECT id FROM provenance WHERE asset_id = ?)", (result["asset_id"],)).fetchone()
    assert ev["basis"] == "license" and ev["license_id"] == "cc-by-4.0" and ev["locator"]
    conn.close()


def test_capture_does_not_follow_undeclared_redirect(ctx, home, website):
    configure(home)
    base, requests = website
    with pytest.raises(Exception):
        REGISTRY["fetch"](ctx, {"source": "capture", "external_id": base + "/escape"})
    assert not any(path == "/blocked" for path, host in requests)


def test_capture_denies_restrictive_licence_before_transfer(ctx, home, website):
    configure(home)
    base, requests = website
    with pytest.raises(ProproomError) as err:
        REGISTRY["fetch"](ctx, {"source": "capture", "external_id": base + "/denied"})
    assert err.value.code == "rights_denied"
    assert sum(path == "/denied" for path, host in requests) == 1


def test_capture_connects_to_existing_cdp_browser(ctx, home, website, tmp_path):
    import socket
    import subprocess
    import time
    import urllib.error
    import urllib.request
    from playwright.sync_api import sync_playwright
    configure(home)
    base, _ = website
    with sync_playwright() as playwright:
        executable = playwright.chromium.executable_path
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    log_file = (tmp_path / "chromium.log").open("w+")
    browser = subprocess.Popen([executable, "--headless", "--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage",
                                f"--remote-debugging-port={port}", f"--user-data-dir={tmp_path / 'cdp'}"],
                               stdout=subprocess.DEVNULL, stderr=log_file, env={**os.environ, "TMPDIR": "/tmp"})
    try:
        for _ in range(100):
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=1):
                    break
            except (OSError, urllib.error.URLError):
                time.sleep(0.1)
        else:
            log_file.flush()
            raise AssertionError(f"CDP browser did not start (exit {browser.poll()}): {(tmp_path / 'chromium.log').read_text()}")
        text = home.config.read_text()
        home.config.write_text(text.replace('cdp_url = ""', f'cdp_url = "http://127.0.0.1:{port}"'))
        result = REGISTRY["fetch"](ctx, {"source": "capture", "external_id": base + "/page"})
        assert store.blob_abspath(home, result["sha256"]).is_file()
        assert browser.poll() is None  # Capture disconnects without stopping the shared browser.
    finally:
        browser.terminate()
        browser.wait(timeout=10)
        log_file.close()


def test_capture_defaults_to_reviewable_quotation(ctx, home, website):
    configure(home)
    base, _ = website
    result = REGISTRY["fetch"](ctx, {"source": "capture", "external_id": base + "/unlicensed"})
    assert result["decision"] == "review"
    conn = db.connect(home.db)
    row = conn.execute("SELECT basis FROM rights_evidence WHERE provenance_id = (SELECT id FROM provenance WHERE asset_id = ?)", (result["asset_id"],)).fetchone()
    assert row["basis"] == "quotation"
    conn.close()


def test_capture_retries_http_429(ctx, home, website):
    configure(home)
    base, requests = website
    result = REGISTRY["fetch"](ctx, {"source": "capture", "external_id": base + "/rate"})
    assert result["decision"] == "allow"
    assert sum(path == "/rate" for path, host in requests) == 3


def test_capture_refuses_page_that_changes_after_rights_check(ctx, home, website):
    configure(home)
    base, _ = website
    with pytest.raises(ProproomError) as err:
        REGISTRY["fetch"](ctx, {"source": "capture", "external_id": base + "/changing"})
    assert err.value.code == "capture_changed"
    conn = db.connect(home.db)
    assert conn.execute("SELECT count(*) FROM asset").fetchone()[0] == 0
    conn.close()


def test_capture_reads_an_explicit_anchor_licence(ctx, home, website):
    configure(home)
    base, _ = website
    result = REGISTRY["fetch"](ctx, {"source": "capture", "external_id": base + "/anchor"})
    assert result["decision"] == "allow"
    conn = db.connect(home.db)
    row = conn.execute("SELECT license_id FROM rights_evidence WHERE provenance_id = (SELECT id FROM provenance WHERE asset_id = ?)", (result["asset_id"],)).fetchone()
    assert row["license_id"] == "cc-by-4.0"
    conn.close()


def test_capture_clips_to_requested_rectangle(ctx, home, website):
    configure(home)
    base, _ = website
    result = REGISTRY["fetch"](ctx, {"source": "capture", "external_id": base + "/page", "options": {
        "viewport_width": 430, "viewport_height": 600, "device_scale_factor": 2,
        "clip": {"x": 0, "y": 0, "width": 100, "height": 80}}})
    with Image.open(store.blob_abspath(home, result["sha256"])) as image:
        assert image.size == (200, 160)
