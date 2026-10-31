"""OCR flags analyzer: detect watermark, burned_subs, logo flags."""
from __future__ import annotations

from ....sdk.context import AnalysisContext
from ....sdk.types import AnalysisResult, FlagProposal


class OcrFlagsAnalyzer:
    async def analyze(self, ctx: AnalysisContext, representation_id: str) -> AnalysisResult:
        return AnalysisResult(flags=[])


Plugin = OcrFlagsAnalyzer
