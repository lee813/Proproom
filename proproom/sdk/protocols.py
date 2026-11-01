"""Plugin type protocols (handover §11.2). Plugins never get a DB connection; each type gets its own Context."""
from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from .context import AnalysisContext, EmbedContext, ExportContext, HookContext, PolicyContext, SourceContext
from .types import (
    AnalysisResult, CandidatePage, EmbeddingBatch, EnumerateRequest, ExportPlan, PolicyDecision,
    RankAdjustment, ResolvedSource, SearchRequest, SegmentProposal, SourceRef, TransferRequest,
)


class Sink(Protocol):
    """Created by core; counts bytes, owns the destination path."""
    async def write(self, chunk: bytes) -> None: ...


@runtime_checkable
class Source(Protocol):
    async def search(self, ctx: SourceContext, req: SearchRequest) -> CandidatePage: ...
    async def enumerate(self, ctx: SourceContext, req: EnumerateRequest) -> CandidatePage: ...
    async def resolve(self, ctx: SourceContext, ref: SourceRef) -> ResolvedSource: ...
    # optional: async def transfer(self, ctx, req: TransferRequest, sink: Sink) -> None


class Embedder(Protocol):
    fingerprint: dict[str, Any]  # model, revision, preprocess, prompts, pooling, normalize, truncation
    dim: int
    async def embed(self, ctx: EmbedContext, inputs: list[dict[str, Any]]) -> EmbeddingBatch: ...


class Analyzer(Protocol):
    async def analyze(self, ctx: AnalysisContext, representation_id: str) -> AnalysisResult: ...


class Segmenter(Protocol):
    async def segment(self, ctx: AnalysisContext, representation_id: str) -> list[SegmentProposal]: ...


class Policy(Protocol):
    def decide(self, ctx: PolicyContext, asset: dict[str, Any]) -> PolicyDecision: ...


class Ranker(Protocol):
    def rerank(self, ctx: PolicyContext, candidates: list[dict[str, Any]]) -> list[RankAdjustment]: ...


class Exporter(Protocol):
    def plan(self, ctx: ExportContext, usages: list[dict[str, Any]]) -> ExportPlan: ...


class Hook(Protocol):
    events: list[str]
    async def handle(self, ctx: HookContext, event_id: int, kind: str, payload: dict[str, Any]) -> None: ...


@runtime_checkable
class HoldsModel(Protocol):
    """Optional, for any plugin type that keeps model weights loaded between calls (handover §4.2). Core calls
    release() once the plugin has gone unused for `embedding.idle_unload_s`; the next call loads the model again."""
    def release(self) -> None: ...


__all__ = ["Source", "Embedder", "Analyzer", "Segmenter", "Policy", "Ranker", "Exporter", "Hook", "HoldsModel", "Sink",
           "TransferRequest"]
