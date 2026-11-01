"""Each item appears once: a share card per item however many sources it has (§9), a search result per clip however
many segments it has (§7)."""
from __future__ import annotations

import os
import socket
import threading
import time

import pytest
from fastapi.testclient import TestClient
from support import ingest, policy_rules

from proproom.api import http
from proproom.api.registry import REGISTRY
from proproom.core import dedupe


@pytest.fixture()
def client(lib):
    with TestClient(http.app, raise_server_exceptions=False) as c:
        yield c


def test_an_asset_with_two_sources_is_one_card(client, ctx, conn, home):
    a = ingest(conn, home, slug="train", title="Train", attribution="First")
    conn.execute("BEGIN IMMEDIATE")
    first = conn.execute("SELECT id FROM provenance WHERE asset_id = ?", (a.asset_id,)).fetchone()[0]
    dedupe.register_rights(conn, provenance_id=first, asset_id=a.asset_id, policy_version=1,
                           policy_rules=policy_rules(conn), basis="license", license_id="cc-by-4.0",
                           attribution="First, corrected")
    second = dedupe.register_provenance(conn, asset_id=a.asset_id, source="import", external_id="episode/train",
                                        source_url="https://example.org/train-2")
    dedupe.register_rights(conn, provenance_id=second, asset_id=a.asset_id, policy_version=1,
                           policy_rules=policy_rules(conn), basis="license", license_id="cc-by-4.0",
                           attribution="Second")
    conn.execute("COMMIT")
    res = REGISTRY["share"](ctx, {"assets": [a.asset_id]})
    items = client.get(f"/s/{res['token']}/items").json()["items"]
    assert [i["pos"] for i in items] == [1]


@pytest.mark.skipif(os.environ.get("PROP_CAPTURE_BROWSER_TEST") != "1", reason="requires browser test image")
def test_the_share_page_renders_a_card_per_item_in_a_browser(lib, ctx, conn, home):
    """The page builds its cards in JavaScript; a script error leaves it blank while /items still answers."""
    import uvicorn
    from playwright.sync_api import sync_playwright
    a = ingest(conn, home, slug="one", title="One")
    b = ingest(conn, home, slug="two", title="Two")
    token = REGISTRY["share"](ctx, {"assets": [a.asset_id, b.asset_id]})["token"]
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    server = uvicorn.Server(uvicorn.Config(http.app, host="127.0.0.1", port=port, log_level="warning"))
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    try:
        for _ in range(100):
            if server.started:
                break
            time.sleep(0.05)
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            page = browser.new_page()
            errors = []
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.on("console", lambda m: m.type == "error" and errors.append(m.text))
            page.goto(f"http://127.0.0.1:{port}/s/{token}")
            page.wait_for_load_state("networkidle")
            titles = page.locator(".item-title").all_inner_texts()
            browser.close()
    finally:
        server.should_exit = True
        t.join(10)
    assert [e for e in errors if "Failed to load resource" not in e] == []
    assert titles == ["#1 One", "#2 Two"]


def test_search_lists_a_clip_once_however_many_segments_it_has(ctx, conn, home):
    """A clip cut into scenes is one result, not one per scene (handover §7)."""
    from proproom.core.ids import ulid
    f = ingest(conn, home, slug="long", title="city skyline aerial", kind="footage", mime="video/webm")
    conn.execute("BEGIN IMMEDIATE")
    for n in range(6):
        conn.execute("INSERT INTO segment(id, representation_id, t0, t1, algo, algo_version, params) "
                     "VALUES (?, ?, ?, ?, 'scenecut', '1', '{}')", (ulid(), f.rep_id, n * 10.0, n * 10.0 + 8))
    conn.execute("COMMIT")
    items = REGISTRY["search"](ctx, {"query": "skyline"})["items"]
    clip = [i for i in items if i["asset_id"] == f.asset_id]
    assert len(clip) == 1 and clip[0]["segment_id"] and clip[0]["duration"] == 8
