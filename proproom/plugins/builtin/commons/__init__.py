"""Wikimedia Commons: images, video (vp9 derivatives <=1080p), audio; extmetadata licence evidence.

Handover §12.
"""
from __future__ import annotations

import re
from typing import Any
from urllib.parse import quote

from ....sdk.context import SourceContext
from ....sdk.protocols import Source
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

_HTML_STRIP = re.compile(r"<[^>]+>")


def _clean_text(s: str | None) -> str:
    if not s:
        return ""
    return _HTML_STRIP.sub("", s).strip()


class CommonsSource:
    API_URL = "https://commons.wikimedia.org/w/api.php"

    async def search(self, ctx: SourceContext, req: SearchRequest) -> CandidatePage:
        limit = min(req.limit or 12, 50)
        params: dict[str, Any] = {
            "action": "query",
            "generator": "search",
            "gsrsearch": req.query,
            "gsrlimit": str(limit),
            "gsrnamespace": "6",  # File: namespace
            "prop": "imageinfo",
            "iiprop": "url|extmetadata|size|mime",
            "iiurlwidth": "2000",
            "format": "json",
        }
        if req.cursor:
            params["gsroffset"] = req.cursor

        data = await ctx.http.get_json(self.API_URL, params=params)
        pages = data.get("query", {}).get("pages", {})

        candidates: list[Candidate] = []
        for p in pages.values():
            if "imageinfo" not in p or not p["imageinfo"]:
                continue
            ii = p["imageinfo"][0]
            meta = ii.get("extmetadata", {})

            artist = _clean_text(meta.get("Artist", {}).get("value"))
            lic_short = _clean_text(meta.get("LicenseShortName", {}).get("value"))
            lic_url = meta.get("LicenseUrl", {}).get("value")

            ev = RightsEvidence(
                basis="license" if lic_short else "unknown",
                license_id=lic_short,
                license_url=lic_url,
                attribution=artist,
                origin="source",
            )

            title = p.get("title", "").replace("File:", "")
            thumb_url = ii.get("thumburl") or ii.get("url")

            candidates.append(Candidate(
                ref=SourceRef(source="commons", external_id=p.get("title", "")),
                title=title,
                thumbnail_url=thumb_url,
                rights=ev,
                width=ii.get("width"),
                height=ii.get("height"),
                duration=ii.get("duration"),
            ))

        next_cursor = None
        if "continue" in data and "gsroffset" in data["continue"]:
            next_cursor = str(data["continue"]["gsroffset"])

        return CandidatePage(items=candidates, next_cursor=next_cursor)

    async def enumerate(self, ctx: SourceContext, req: EnumerateRequest) -> CandidatePage:
        # Category enumeration
        limit = min(req.limit or 20, 50)
        category = req.params.get("category", "")
        params: dict[str, Any] = {
            "action": "query",
            "generator": "categorymembers",
            "gcmtitle": f"Category:{category}" if not category.startswith("Category:") else category,
            "gcmlimit": str(limit),
            "gcmtype": "file",
            "prop": "imageinfo",
            "iiprop": "url|extmetadata|size|mime",
            "iiurlwidth": "2000",
            "format": "json",
        }
        if req.cursor:
            params["gcmcontinue"] = req.cursor

        data = await ctx.http.get_json(self.API_URL, params=params)
        pages = data.get("query", {}).get("pages", {})

        candidates: list[Candidate] = []
        for p in pages.values():
            if "imageinfo" not in p or not p["imageinfo"]:
                continue
            ii = p["imageinfo"][0]
            meta = ii.get("extmetadata", {})
            artist = _clean_text(meta.get("Artist", {}).get("value"))
            lic_short = _clean_text(meta.get("LicenseShortName", {}).get("value"))
            lic_url = meta.get("LicenseUrl", {}).get("value")

            candidates.append(Candidate(
                ref=SourceRef(source="commons", external_id=p.get("title", "")),
                title=p.get("title", "").replace("File:", ""),
                thumbnail_url=ii.get("thumburl") or ii.get("url"),
                rights=RightsEvidence(basis="license" if lic_short else "unknown", license_id=lic_short, license_url=lic_url, attribution=artist),
                width=ii.get("width"),
                height=ii.get("height"),
                duration=ii.get("duration"),
            ))

        next_cursor = data.get("continue", {}).get("gcmcontinue")
        return CandidatePage(items=candidates, next_cursor=next_cursor)

    async def resolve(self, ctx: SourceContext, ref: SourceRef) -> ResolvedSource:
        params = {
            "action": "query",
            "titles": ref.external_id,
            "prop": "imageinfo|videoinfo",
            "iiprop": "url|extmetadata|size|mime",
            "iiurlwidth": "2000",
            "viprop": "derivatives|url|extmetadata",
            "format": "json",
        }
        data = await ctx.http.get_json(self.API_URL, params=params)
        pages = data.get("query", {}).get("pages", {})
        page = next(iter(pages.values()))

        meta = {}
        offers: list[RepresentationOffer] = []
        rights_list: list[RightsEvidence] = []

        if "imageinfo" in page and page["imageinfo"]:
            ii = page["imageinfo"][0]
            meta = ii.get("extmetadata", {})

            artist = _clean_text(meta.get("Artist", {}).get("value"))
            lic_short = _clean_text(meta.get("LicenseShortName", {}).get("value"))
            lic_url = meta.get("LicenseUrl", {}).get("value")
            usage_terms = _clean_text(meta.get("UsageTerms", {}).get("value"))
            restr = _clean_text(meta.get("Restrictions", {}).get("value"))
            restrictions = [restr] if restr else []

            basis = "public_domain" if "public domain" in lic_short.lower() or "pd" in lic_short.lower() else "license"
            pd_reason = "pd-old" if basis == "public_domain" else None

            rights_list.append(RightsEvidence(
                basis=basis,
                license_id=lic_short,
                license_url=lic_url,
                pd_reason=pd_reason,
                attribution=artist,
                restrictions=restrictions,
                original_text=usage_terms,
                origin="source",
            ))

            # Original offer
            offers.append(RepresentationOffer(
                role="original",
                mime=ii.get("mime", "image/jpeg"),
                url=ii.get("url"),
                width=ii.get("width"),
                height=ii.get("height"),
                size_bytes=ii.get("size"),
            ))

            # 2000px thumb offer if different
            if ii.get("thumburl") and ii.get("thumbwidth") != ii.get("width"):
                offers.append(RepresentationOffer(
                    role="thumb_2000",
                    mime="image/jpeg",
                    url=ii.get("thumburl"),
                    width=ii.get("thumbwidth"),
                    height=ii.get("thumbheight"),
                ))

        # Check video derivatives
        if "videoinfo" in page and page["videoinfo"]:
            vi = page["videoinfo"][0]
            derivatives = vi.get("derivatives", [])
            vp9_offers = []
            for d in derivatives:
                d_type = d.get("type", "")
                h = d.get("height", 0)
                if "vp9" in d_type or "webm" in d_type:
                    vp9_offers.append((h, d))

            if vp9_offers:
                # Sort by height descending <= 1080
                vp9_offers.sort(key=lambda x: x[0], reverse=True)
                chosen = None
                for h, d in vp9_offers:
                    if h <= 1080:
                        chosen = d
                        break
                if not chosen:
                    chosen = vp9_offers[-1][1]  # smallest

                offers.append(RepresentationOffer(
                    role="vp9_1080",
                    mime=chosen.get("type", "video/webm"),
                    url=chosen.get("src"),
                    width=chosen.get("width"),
                    height=chosen.get("height"),
                    size_bytes=chosen.get("size"),
                ))

        title = page.get("title", "").replace("File:", "")
        desc = _clean_text(meta.get("ImageDescription", {}).get("value"))

        return ResolvedSource(
            ref=ref,
            metadata={"title": title, "description": desc, "raw": meta},
            rights=rights_list,
            offers=offers,
        )


Plugin = CommonsSource
