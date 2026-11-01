"""Send an operation to the running propd instead of running it in this process (handover §8).

propd holds the models, the worker and the plugin manager. A CLI or MCP process that ran operations itself would
load a second copy of every model, which a 4 GB GPU cannot hold next to propd's. So `prop <operation>` and the MCP
server forward to propd when one serves the same library, and run the operation themselves only when none is
running (`PROP_LOCAL=1` forces that). `PROP_SERVER` overrides the address.
"""
from __future__ import annotations

import json
import os
import sqlite3
import urllib.error
import urllib.request
from typing import Any

from ..core.paths import Home, resolve_home
from ..sdk.errors import ProproomError

DEFAULT_URL = "http://127.0.0.1:8765"


def _instance_id(home: Home) -> str | None:
    if not home.db.exists():
        return None
    conn = sqlite3.connect(f"file:{home.db}?mode=ro", uri=True)
    try:
        row = conn.execute("SELECT value FROM instance WHERE key = 'instance_id'").fetchone()
    finally:
        conn.close()
    return row[0] if row else None


def _error(raw: bytes, status: int) -> ProproomError:
    try:
        body = json.loads(raw)
    except ValueError:
        return ProproomError("server_error", f"propd answered {status}: {raw[:200]!r}")
    err = body.get("error") or body.get("detail")
    if isinstance(err, dict) and "code" in err and "message" in err:
        return ProproomError(**err)
    return ProproomError("server_error", f"propd answered {status}: {body}")


class Server:
    def __init__(self, url: str, token: str, actor: str) -> None:
        self.url, self.token, self.actor = url, token, actor

    def call(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        req = urllib.request.Request(
            f"{self.url}/v1/ops/{name}", data=json.dumps(args).encode("utf-8"), method="POST",
            headers={"Authorization": f"Bearer {self.token}", "Content-Type": "application/json",
                     "X-Prop-Actor": self.actor})
        try:
            with urllib.request.urlopen(req) as r:  # no timeout: an import or link can legitimately take minutes
                return json.load(r)
        except urllib.error.HTTPError as e:
            raise _error(e.read(), e.code) from None
        except (urllib.error.URLError, OSError) as e:
            # Not retried here: propd may have run part of the operation before it went away.
            raise ProproomError("server_unavailable", f"propd stopped answering during {name}: {e}",
                                hint="check `docker ps`; repeat the call with the same idempotency_key",
                                retryable=True) from None


def find(actor: str) -> Server | None:
    """The propd serving this library, or None when none is running here (the caller then runs the operation)."""
    if os.environ.get("PROP_LOCAL") == "1":
        return None
    home = resolve_home()
    if not home.token.exists():
        return None
    url = os.environ.get("PROP_SERVER", DEFAULT_URL).rstrip("/")
    try:
        with urllib.request.urlopen(f"{url}/healthz", timeout=2) as r:
            health = json.load(r)
    except urllib.error.HTTPError as e:
        if e.code != 503:
            return None
        health = json.loads(e.read())
    except (urllib.error.URLError, OSError, ValueError):
        return None
    if health.get("instance_id") != _instance_id(home):
        return None  # a propd for another library listens there
    if not health.get("ok"):
        raise ProproomError("server_unavailable", f"propd is restarting (GPU lost: {health.get('gpu_lost')})",
                            retryable=True, retry_after=30)
    return Server(url, home.token.read_text().strip(), actor)
