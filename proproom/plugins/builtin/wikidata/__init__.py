"""Wikidata: entity resolution only (name -> QID, multilingual labels, P569/P570, depicts reverse lookup).

Handover §7, §12.
"""
from __future__ import annotations

from typing import Any

from ....sdk.context import SourceContext
from ....sdk.types import (
    Candidate,
    CandidatePage,
    EnumerateRequest,
    ResolvedSource,
    SearchRequest,
    SourceRef,
)


class WikidataSource:
    API_URL = "https://www.wikidata.org/w/api.php"

    async def search(self, ctx: SourceContext, req: SearchRequest) -> CandidatePage:
        params = {
            "action": "wbsearchentities",
            "search": req.query,
            "language": "en",
            "uselang": "en",
            "limit": str(min(req.limit or 10, 20)),
            "format": "json",
        }
        data = await ctx.http.get_json(self.API_URL, params=params)
        results = data.get("search", [])

        candidates: list[Candidate] = []
        for r in results:
            qid = r.get("id")
            label = r.get("label", qid)
            desc = r.get("description", "")
            candidates.append(Candidate(
                ref=SourceRef(source="wikidata", external_id=qid),
                title=f"{label} ({qid})" if desc else label,
            ))
        return CandidatePage(items=candidates)

    async def enumerate(self, ctx: SourceContext, req: EnumerateRequest) -> CandidatePage:
        return CandidatePage(items=[])

    async def resolve(self, ctx: SourceContext, ref: SourceRef) -> ResolvedSource:
        qid = ref.external_id
        params = {
            "action": "wbgetentities",
            "ids": qid,
            "props": "labels|aliases|descriptions|claims",
            "languages": "en|zh|ja|fr|de|es",
            "format": "json",
        }
        data = await ctx.http.get_json(self.API_URL, params=params)
        entity = data.get("entities", {}).get(qid, {})

        labels = {k: v.get("value") for k, v in entity.get("labels", {}).items() if "value" in v}
        aliases = {}
        for k, vlist in entity.get("aliases", {}).items():
            aliases[k] = [a.get("value") for a in vlist if "value" in a]
        descriptions = {k: v.get("value") for k, v in entity.get("descriptions", {}).items() if "value" in v}

        # Claims: P569 (birth), P570 (death)
        claims = entity.get("claims", {})
        birth = None
        death = None
        if "P569" in claims and claims["P569"]:
            time_val = claims["P569"][0].get("mainsnak", {}).get("datavalue", {}).get("value", {}).get("time")
            if time_val:
                birth = time_val.lstrip("+")[:4]
        if "P570" in claims and claims["P570"]:
            time_val = claims["P570"][0].get("mainsnak", {}).get("datavalue", {}).get("value", {}).get("time")
            if time_val:
                death = time_val.lstrip("+")[:4]

        return ResolvedSource(
            ref=ref,
            metadata={
                "qid": qid,
                "labels": labels,
                "aliases": aliases,
                "descriptions": descriptions,
                "birth": birth,
                "death": death,
            },
            rights=[],
            offers=[],
        )


Plugin = WikidataSource
