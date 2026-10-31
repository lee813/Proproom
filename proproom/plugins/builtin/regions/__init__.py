"""Composition / Region analyzer: face, plate, text, subject, logo bounding boxes.

Handover §7, §13.
"""
from __future__ import annotations

from ....sdk.context import AnalysisContext
from ....sdk.types import AnalysisResult, RegionBox


class RegionsAnalyzer:
    async def analyze(self, ctx: AnalysisContext, representation_id: str) -> AnalysisResult:
        # Returns region proposals
        return AnalysisResult(regions=[])


Plugin = RegionsAnalyzer
