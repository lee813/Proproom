"""Webpage screenshot source plugin: facts / quotations / official data.

Handover §12.
"""
from __future__ import annotations

from typing import Any

from ....sdk.context import SourceContext
from ....sdk.types import (
    Candidate,
    CandidatePage,
    EnumerateRequest,
    RepresentationOffer,
    ResolvedSource,
    RightsEvidence,
    SearchRequest,
    SourceRef,
)


class CaptureSource:
    async def search(self, ctx: SourceContext, req: SearchRequest) -> CandidatePage:
        return CandidatePage(items=[])

    async def enumerate(self, ctx: SourceContext, req: EnumerateRequest) -> CandidatePage:
        return CandidatePage(items=[])

    async def resolve(self, ctx: SourceContext, ref: SourceRef) -> ResolvedSource:
        url = ref.external_id
        return ResolvedSource(
            ref=ref,
            metadata={"title": f"Capture of {url}", "source_url": url},
            rights=[RightsEvidence(basis="quotation", locator=url, origin="machine")],
            offers=[],
        )


Plugin = CaptureSource
