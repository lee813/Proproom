"""Fixtures for the handover-conformance suite (docs/HANDOVER.md).

Every test cites the handover section it checks. Known gaps are marked with `gap(...)` — a strict xfail:
the suite stays green while the gap exists, and turns red (XPASS) once it is fixed so the mark gets removed.
Run the gaps as real failures with `pytest --runxfail tests/handover`.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from proproom.api.registry import OpContext
from proproom.core import bootstrap, db, store


@pytest.fixture()
def nas(tmp_path, monkeypatch) -> Path:
    nas = tmp_path / "nas"
    nas.mkdir()
    monkeypatch.setenv("PROP_NAS", str(nas))
    monkeypatch.delenv("PROP_HOME", raising=False)
    monkeypatch.delenv("PROP_PUBLIC_URL", raising=False)
    # bootstrap writes these into os.environ; registering them here makes monkeypatch restore them.
    for k in ("TMPDIR", "SQLITE_TMPDIR", "HF_HOME"):
        monkeypatch.setenv(k, str(tmp_path))
    return nas


@pytest.fixture()
def lib(nas, monkeypatch):
    monkeypatch.setenv("PROP_HOME", str(nas / "lib"))
    return bootstrap.boot()


@pytest.fixture()
def home(lib):
    return lib.home


@pytest.fixture()
def ctx(lib) -> OpContext:
    return OpContext(lib, actor="test")


@pytest.fixture()
def conn(lib):
    c = db.connect(lib.home.db)
    yield c
    c.close()


@pytest.fixture()
def fake_download(monkeypatch, home):
    """Replace the network downloader; records every URL fetched."""
    from proproom.core import download

    calls: list[str] = []

    async def _dl(home_, url, **_):
        calls.append(url)
        p = home_.dir("tmp") / f"fake_{len(calls)}"
        data = f"downloaded:{url}".encode()
        p.write_bytes(data)
        sha, n = store.hash_bytes(data)
        return p, sha, n

    monkeypatch.setattr(download, "download_file", _dl)
    return calls
