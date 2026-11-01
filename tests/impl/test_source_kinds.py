"""Sources narrow a search to the requested kind, and archive.org lists a collection (§12, §13)."""
from __future__ import annotations

import asyncio
import json
import logging

from support import FakeHttp

from proproom.plugins.builtin.archive_org import Plugin as ArchiveOrg
from proproom.plugins.builtin.commons import Plugin as Commons
from proproom.sdk.context import SourceContext
from proproom.sdk.types import EnumerateRequest, SearchRequest


def _ctx(http):
    return SourceContext(plugin="t", log=logging.getLogger("t"), config={}, http=http, keys={})


def test_archive_org_lists_a_collection_page_by_page():
    docs = [{"identifier": f"film{i}", "title": f"Film {i}"} for i in range(50)]
    http = FakeHttp({"collection:prelinger": {"response": {"docs": docs}}})
    page = asyncio.run(ArchiveOrg().enumerate(_ctx(http), EnumerateRequest(collection="prelinger", query="industry")))
    assert len(page.items) == 50 and page.next_cursor == "2"
    assert "collection:prelinger AND (industry)" in json.dumps(http.calls[0][1])


def test_searches_ask_the_source_for_the_kind():
    http = FakeHttp({"": {"response": {"docs": []}, "query": {"pages": {}}}})
    asyncio.run(ArchiveOrg().search(_ctx(http), SearchRequest(query="city", kind=["footage"])))
    asyncio.run(Commons().search(_ctx(http), SearchRequest(query="city", kind=["footage"])))
    asyncio.run(Commons().search(_ctx(http), SearchRequest(query="city", kind=["footage", "photo"])))
    sent = [json.dumps(params) for _, params in http.calls]
    assert "mediatype:movies" in sent[0] and "filetype:video" in sent[1] and "filetype" not in sent[2]
