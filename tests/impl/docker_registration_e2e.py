"""Run inside a disposable Proproom container; never print its temporary management token."""
import json
from pathlib import Path
import subprocess
import time
import urllib.error
import urllib.request

from PIL import Image

BASE = "http://127.0.0.1:8765"


def request(path, *, token=None, body=None, headers=None):
    values = dict(headers or {})
    if token:
        values["Authorization"] = "Bearer " + token
    if body is not None:
        values["Content-Type"] = "application/json"
    req = urllib.request.Request(BASE + path, headers=values,
                                 data=json.dumps(body).encode() if body is not None else None)
    with urllib.request.urlopen(req, timeout=20) as response:
        return response.status, response.read()


def main():
    for _ in range(90):
        try:
            _, raw = request("/healthz")
            health = json.loads(raw)
            if health["ok"]:
                break
        except (OSError, urllib.error.URLError):
            pass
        time.sleep(1)
    else:
        raise AssertionError("Disposable server did not become healthy")

    # The token is read from the temporary container's local CLI and remains in memory.
    credential = json.loads(subprocess.check_output(["prop", "credential"], text=True))
    assert credential["instance_id"] == health["instance_id"]
    token = credential["token"]
    assert len(token) >= 32
    _, raw = request("/v1/capabilities", token=token)
    assert isinstance(json.loads(raw), dict)
    try:
        request("/v1/capabilities")
        raise AssertionError("Management API accepted an unauthenticated request")
    except urllib.error.HTTPError as e:
        assert e.code == 401

    image_path = Path("/work/test_us_dollar.png")
    Image.new("RGB", (40, 30), "green").save(image_path)
    _, raw = request("/v1/ops/add", token=token, body={"paths": [str(image_path)], "basis": "own_work"})
    assert json.loads(raw)["count"] == 1
    _, raw = request("/v1/ops/search", token=token, body={"query": "test us dollar"})
    selection = json.loads(raw)
    assert selection["selection_id"] and len(selection["items"]) == 1
    _, raw = request("/v1/ops/share", token=token, body={"selection_id": selection["selection_id"]})
    share = json.loads(raw)
    _, raw = request(f"/s/{share['token']}/items")
    items = json.loads(raw)["items"]
    assert len(items) == 1
    status, body = request(f"/s/{share['token']}/media/{items[0]['representation_id']}",
                           headers={"Range": "bytes=0-7"})
    assert status == 206 and body == b"\x89PNG\r\n\x1a\n"
    print("PASS: Docker boot, credential, API auth, temporary ingest, search, share and Range")


if __name__ == "__main__":
    main()
