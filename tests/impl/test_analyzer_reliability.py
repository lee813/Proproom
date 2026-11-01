"""Analyze failures without spending asset retries on a lost GPU."""
import asyncio
import json
import logging
import os
import subprocess
from pathlib import Path

import pytest
from PIL import Image

from proproom.core import analysis
from proproom.core.plugins import LoadedPlugin
from proproom.plugins.builtin.ocr_flags import OcrFlagsAnalyzer, _words
from proproom.sdk.context import AnalysisContext
from proproom.sdk.errors import ProproomError
from proproom.sdk.types import AnalysisResult
from tests.handover.support import ingest, install_source


def test_ocr_bounds_size_and_tesseract_threads(monkeypatch):
    def run(cmd, **kwargs):
        from io import BytesIO
        with Image.open(BytesIO(kwargs["input"])) as im:
            assert max(im.size) <= 2048
        assert kwargs["env"]["OMP_THREAD_LIMIT"] == "1"
        return subprocess.CompletedProcess(cmd, 0, stdout=b"text\tconf\twidth\theight\nhello\t99\t20\t10\n", stderr=b"")

    monkeypatch.setattr(subprocess, "run", run)
    words, density = _words(Image.new("RGB", (5000, 1000)), "eng", 6)
    assert words == ["hello"] and density > 0


@pytest.mark.parametrize("previous", [False, True])
def test_gpu_loss_restores_previous_attempt(ctx, conn, home, monkeypatch, previous):
    class Fails:
        async def analyze(self, ctx_, rep_id):
            raise RuntimeError("accelerator disappeared")

    ingest(conn, home, slug="gpu", title="GPU")
    pm = install_source(ctx, "fake", object())
    pm.plugins["device"] = LoadedPlugin("device", "1", "0" * 64, ["analyzer"], "dir",
                                        {"plugin": {"status": "ready"}}, Fails())
    rep = conn.execute("SELECT id FROM representation").fetchone()[0]
    if previous:
        conn.execute("INSERT INTO analysis_run VALUES (?, 'device', '1', 'failed', 2, ?, ?, ?)",
                     (rep, json.dumps({"code": "old", "message": "previous"}), "2000-01-01T00:00:00Z", "2000-01-01T00:00:00Z"))
    before = conn.execute("SELECT * FROM analysis_run WHERE plugin = 'device'").fetchone()

    def lost():
        raise ProproomError("gpu_lost", "GPU is not accessible", retryable=True)

    monkeypatch.setattr(analysis.hardware, "require_gpu", lost)
    with pytest.raises(ProproomError) as exc:
        analysis.step(ctx, conn, pm)
    assert exc.value.code == "gpu_lost"
    after = conn.execute("SELECT * FROM analysis_run WHERE plugin = 'device'").fetchone()
    assert tuple(after) == tuple(before) if previous else after is None


@pytest.mark.skipif(os.environ.get("PROP_OCR_LIVE_TEST") != "1", reason="requires read-only OCR fixture mounts")
@pytest.mark.parametrize("path", ["/fixtures/first", "/fixtures/second"])
def test_ocr_on_read_only_originals(path):
    ctx = AnalysisContext(plugin="ocr_flags", log=logging.getLogger("ocr-test"), config={},
                          open_representation=lambda _: Path(path))
    result = asyncio.run(OcrFlagsAnalyzer().analyze(ctx, "fixture"))
    assert isinstance(result, AnalysisResult)
