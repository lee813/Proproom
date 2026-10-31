"""Plugin data types (handover §11.2, §11.3). Plugins return these; core persists them."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

Basis = Literal["license", "public_domain", "provider_license", "quotation", "own_work", "unknown"]
Origin = Literal["source", "machine", "human"]
Decision = Literal["allow", "review", "deny"]


@dataclass(frozen=True)
class SourceRef:
    source: str
    external_id: str
    revision: str = ""


@dataclass
class RightsEvidence:
    basis: Basis
    original_text: str | None = None
    license_id: str | None = None
    license_url: str | None = None
    pd_reason: str | None = None
    jurisdiction: str | None = None
    attribution: str | None = None
    restrictions: list[str] = field(default_factory=list)
    covers: Literal["work", "recording", "both"] = "work"
    locator: str | None = None
    retrieved: str | None = None
    origin: Origin = "source"


@dataclass
class Candidate:
    ref: SourceRef
    title: str
    thumbnail_url: str | None = None
    rights: RightsEvidence | None = None  # None / basis="unknown" allowed for remote candidates
    width: int | None = None
    height: int | None = None
    duration: float | None = None


@dataclass
class CandidatePage:
    items: list[Candidate]
    next_cursor: str | None = None


@dataclass
class RepresentationOffer:
    role: str  # original | thumb_2000 | vp9_1080 | …
    mime: str
    url: str
    width: int | None = None
    height: int | None = None
    bytes: int | None = None
    duration: float | None = None


@dataclass
class ResolvedSource:
    ref: SourceRef
    title: str
    kind: str
    metadata: dict[str, Any]
    evidence: list[RightsEvidence]
    offers: list[RepresentationOffer]
    source_url: str | None = None
    publisher: str | None = None
    publisher_type: str | None = None
    entities: list[str] = field(default_factory=list)  # Wikidata QIDs (depicts)
    tags: list[str] = field(default_factory=list)


@dataclass
class SearchRequest:
    query: str
    kind: list[str] | None = None
    cursor: str | None = None
    limit: int = 24


@dataclass
class EnumerateRequest:
    category: str | None = None
    collection: str | None = None
    query: str | None = None
    cursor: str | None = None


@dataclass
class TransferRequest:
    ref: SourceRef
    offer: RepresentationOffer


@dataclass
class EmbeddingBatch:
    space_fingerprint: str
    vectors: list[list[float]]
    input_hashes: list[str]


@dataclass
class FlagResult:
    kind: str
    severity: Literal["info", "warn", "block"]
    score: float | None = None
    evidence: dict[str, Any] = field(default_factory=dict)


@dataclass
class AnalysisResult:
    flags: list[FlagResult] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    regions: list[dict[str, Any]] = field(default_factory=list)  # {kind, x, y, w, h, t?, score?}
    evidence: list[RightsEvidence] = field(default_factory=list)


@dataclass
class SegmentProposal:
    t0: float
    t1: float
    algo: str
    algo_version: str
    params: dict[str, Any]


@dataclass
class PolicyDecision:
    decision: Decision
    reasons: list[dict[str, Any]] = field(default_factory=list)
    evidence_ids: list[str] = field(default_factory=list)
    obligations: list[str] = field(default_factory=list)
    code: str | None = None
    detail: str | None = None

    def __post_init__(self):
        if self.code and not self.reasons:
            self.reasons = [{"code": self.code, "detail": self.detail or ""}]


FlagProposal = FlagResult
RegionBox = dict[str, Any]


@dataclass
class RankAdjustment:
    asset_id: str
    delta: float
    reason: str


@dataclass
class ExportPlan:
    manifest_patch: list[dict[str, Any]]  # JSON-patch style ops on the project manifest
    credits: list[dict[str, Any]]
    targets: list[dict[str, Any]]         # {usage_ref, relpath}; relative to the project
