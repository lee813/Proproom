"""Internet Archive source plugin: historical footage, audio, documents.

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


class ArchiveOrgSource:
    SEARCH_URL = "https://archive.org/advancedsearch.php"
    META_URL = "https://archive.org/metadata/"

    async def search(self, ctx: SourceContext, req: SearchRequest) -> CandidatePage:
        limit = min(req.limit or 10, 50)
        params = {
            "q": req.query,
            "fl[]": "identifier,title,description,mediatype,licenseurl",
            "rows": str(limit),
            "output": "json",
        }
        if req.cursor:
            params["page"] = req.cursor

        data = await ctx.http.get_json(self.SEARCH_URL, params=params)
        docs = data.get("response", {}).get("docs", [])

        candidates: list[Candidate] = []
        for d in docs:
            ident = d.get("identifier")
            title = d.get("title") or ident
            lic_url = d.get("licenseurl")
            thumb_url = f"https://archive.org/services/img/{ident}"

            candidates.append(Candidate(
                ref=SourceRef(source="archive_org", external_id=ident),
                title=title,
                thumbnail_url=thumb_url,
                rights=RightsEvidence(basis="license" if lic_url else "unknown", license_url=lic_url),
            ))

        return CandidatePage(items=candidates)

    async def enumerate(self, ctx: SourceContext, req: EnumerateRequest) -> CandidatePage:
        return CandidatePage(items=[])

    async def resolve(self, ctx: SourceContext, ref: SourceRef) -> ResolvedSource:
        url = f"{self.META_URL}{ref.external_id}"
        data = await ctx.http.get_json(url)
        meta = data.get("metadata", {})
        files = data.get("files", [])

        title = meta.get("title") or ref.external_id
        desc = meta.get("description", "")
        lic_url = meta.get("licenseurl")

        offers: list[RepresentationOffer] = []
        server = data.get("server")
        dir_path = data.get("dir")

        if server and dir_path:
            for f in files:
                format_type = f.get("format", "").lower()
                name = f.get("name", "")
                if format_type in ("mp4", "webm", "h.264", "jpeg", "png", "mp3", "vbr mp3"):
                    f_url = f"https://{server}{dir_path}/{name}"
                    mime = "video/mp4" if "mp4" in format_type or "h.264" in format_type else (
                        "image/jpeg" if "jpeg" in format_type else "audio/mpeg"
                    )
                    role = "original" if "original" in f.get("source", "") else "derivative"
                    offers.append(RepresentationOffer(
                        role=role,
                        mime=mime,
                        url=f_url,
                        width=int(f.get("width")) if f.get("width") else None,
                        height=int(f.get("height")) if f.get("height") else None,
                        size_bytes=int(f.get("size")) if f.get("size") else None,
                    ))

        return ResolvedSource(
            ref=ref,
            metadata={"title": title, "description": desc, "raw": meta},
            rights=[RightsEvidence(basis="license" if lic_url else "public_domain", license_url=lic_url, origin="source")],
            offers=offers,
        )


Plugin = ArchiveOrgSource
