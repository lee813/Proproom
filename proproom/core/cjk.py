"""CJK query rewriting for the trigram FTS table (handover §7, appendix B note 4).

trigram only matches contiguous substrings of >= 3 chars, so each CJK run is cut into overlapping
3-grams joined by OR ("托克维尔肖像" -> "托克维" OR "克维尔" OR "维尔肖" OR "尔肖像"), ranked by bm25.
Runs of <= 2 chars cannot go through MATCH; they are returned for a LIKE on search_doc.cjk.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

# CJK unified ideographs (+ ext A), compatibility ideographs, hiragana, katakana, halfwidth katakana, hangul.
_CJK_RUN = re.compile(r"[㐀-䶿一-鿿豈-﫿぀-ゟ゠-ヿｦ-ﾟ가-힯]+")


@dataclass
class CjkQuery:
    match: str | None = None                       # FTS5 MATCH expression for fts_cjk, or None
    like: list[str] = field(default_factory=list)  # short runs -> search_doc.cjk LIKE '%…%'


def rewrite(text: str) -> CjkQuery:
    grams: list[str] = []
    like: list[str] = []
    for run in _CJK_RUN.findall(text):
        if len(run) < 3:
            like.append(run)
            continue
        for i in range(len(run) - 2):
            g = run[i:i + 3]
            if g not in grams:
                grams.append(g)
    return CjkQuery(" OR ".join(f'"{g}"' for g in grams) or None, like)


def has_cjk(text: str) -> bool:
    return bool(_CJK_RUN.search(text))
