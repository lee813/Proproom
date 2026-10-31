"""Tests for API operations, HTTP endpoints, and share flow."""
import json
from pathlib import Path
from fastapi.testclient import TestClient
from proproom.api.http import app, _state
from proproom.api.registry import REGISTRY, OpContext
from proproom.core import bootstrap, db, dedupe, store
from proproom.core.paths import Home


def test_full_api_workflow(tmp_path, monkeypatch):
    monkeypatch.setenv("PROP_NAS", str(tmp_path))
    monkeypatch.setenv("PROP_HOME", str(tmp_path / "lib"))

    home = Home(tmp_path / "lib", tmp_path, "env", {})
    booted = bootstrap.boot(home)
    ctx = OpContext(booted, actor="test")

    # 1. Add local assets
    p1 = tmp_path / "photo1.jpg"
    p1.write_bytes(b"sample photo one image content")
    add_res = REGISTRY["add"](ctx, {"paths": [str(p1)], "basis": "own_work"})
    assert len(add_res["added"]) == 1
    aid = add_res["added"][0]["asset_id"]

    # 2. Search
    search_res = REGISTRY["search"](ctx, {"query": "photo", "sheet": True})
    assert search_res["selection_id"]
    assert len(search_res["items"]) >= 1
    assert search_res["sheet"] is not None  # sheet generated!

    sel_id = search_res["selection_id"]

    # 3. Share
    share_res = REGISTRY["share"](ctx, {"selection_id": sel_id, "title": "Review batch"})
    token = share_res["token"]
    assert token

    # 4. HTTP client tests
    client = TestClient(app)
    # Re-initialize http state
    _state["booted"] = booted
    _state["token"] = bootstrap.read_token(home)

    # Healthz
    r = client.get("/healthz")
    assert r.status_code == 200

    # Auth check on /v1/capabilities
    r_unauth = client.get("/v1/capabilities")
    assert r_unauth.status_code == 401

    r_auth = client.get("/v1/capabilities", headers={"Authorization": f"Bearer {_state['token']}"})
    assert r_auth.status_code == 200
    caps = r_auth.json()
    assert "operations" in caps
    assert "sources" in caps

    # Share page GET
    r_page = client.get(f"/s/{token}")
    assert r_page.status_code == 200
    assert "Proproom Share" in r_page.text

    # Share items GET
    r_items = client.get(f"/s/{token}/items")
    assert r_items.status_code == 200
    items_data = r_items.json()
    assert len(items_data["items"]) == 1
    rep_id = items_data["items"][0]["representation_id"]

    # Share media GET with Range header
    r_media = client.get(f"/s/{token}/media/{rep_id}", headers={"Range": "bytes=0-10"})
    assert r_media.status_code in (200, 206)
    assert len(r_media.content) <= 11

    # Share feedback POST
    r_fb = client.post(f"/s/{token}/feedback", json={"pos": 1, "verdict": "want", "comment": "Great photo!"})
    assert r_fb.status_code == 200

    # Read feedback back via operation
    fb_res = REGISTRY["share_feedback"](ctx, {"share": token})
    assert len(fb_res["feedback"]) == 1
    assert fb_res["feedback"][0]["verdict"] == "want"
    assert fb_res["feedback"][0]["comment"] == "Great photo!"
