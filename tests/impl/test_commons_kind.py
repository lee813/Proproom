"""Commons answers `prop=imageinfo|videoinfo` with a videoinfo block for photos too; a photo stays a photo."""
from __future__ import annotations

import asyncio
import logging

from support import FakeHttp

from proproom.plugins.builtin.commons import Plugin as Commons
from proproom.sdk.context import SourceContext
from proproom.sdk.types import SourceRef


def _page(mime, derivatives):
    meta = {"Artist": {"value": "Someone"}, "LicenseShortName": {"value": "CC BY 2.0"},
            "LicenseUrl": {"value": "https://creativecommons.org/licenses/by/2.0"}}
    return {"query": {"pages": {"1": {
        "title": "File:X", "imageinfo": [{"url": "https://upload.wikimedia.org/x", "width": 4000, "height": 6000,
                                          "mime": mime, "size": 1, "extmetadata": meta}],
        "videoinfo": [{"url": "https://upload.wikimedia.org/x", "derivatives": derivatives}]}}}}


def _kind(mime, derivatives):
    ctx = SourceContext(plugin="commons", log=logging.getLogger("t"), config={}, http=FakeHttp({"": _page(mime, derivatives)}),
                        keys={})
    return asyncio.run(Commons().resolve(ctx, SourceRef("commons", "File:X"))).kind


def test_a_photo_with_an_empty_videoinfo_block_is_a_photo():
    assert _kind("image/jpeg", []) == "photo"


def test_a_video_is_footage():
    vp9 = [{"type": "video/webm; codecs=\"vp9, opus\"", "src": "https://upload.wikimedia.org/x.webm", "width": 1920,
            "height": 1080, "transcodekey": "1080p.vp9.webm"}]
    assert _kind("video/webm", vp9) == "footage"


def _ogg_kind(mediatype):
    page = _page("application/ogg", [])
    page["query"]["pages"]["1"]["imageinfo"][0]["mediatype"] = mediatype
    ctx = SourceContext(plugin="commons", log=logging.getLogger("t"), config={}, http=FakeHttp({"": page}), keys={})
    return asyncio.run(Commons().resolve(ctx, SourceRef("commons", "File:X.ogv"))).kind


def test_ogg_video_is_footage_and_ogg_audio_is_music():
    """Commons gives both the MIME type application/ogg; its `mediatype` tells them apart."""
    assert _ogg_kind("VIDEO") == "footage" and _ogg_kind("AUDIO") == "music"
