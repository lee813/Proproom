"""OCR flags: burned-in subtitles and watermarks (handover §13), measured with Tesseract.

- `burned_subs` (video): text in the bottom quarter of at least two sampled frames, and the text changes between
  frames. Subtitles change; a fixed caption bar or channel name does not.
- `watermark`: text in a frame corner that is the same in every sampled frame. A still has one frame, so there the
  text must also be alone: in exactly one corner, with no other text in the bottom band (a page of a book or a
  captioned print has text everywhere and is not watermarked).

Only confident words count (Tesseract confidence >= 80, at least 3 letters or digits). On the CAUSCAPE library
(2026-10-31, 140 items) a looser cut (60, 2 letters) read photo texture as words and flagged 52 stills and 11 clips,
none of them watermarked or subtitled.

Flags are for review, not rejection. Evidence keeps the words, frame times and text density so an agent can judge.
"""
from __future__ import annotations

import csv
import io
import re
import subprocess

from ....sdk.context import AnalysisContext
from ....sdk.errors import ProproomError
from ....sdk.media import frames
from ....sdk.types import AnalysisResult, FlagResult

POSITIONS = [0.2, 0.4, 0.6, 0.8]
MIN_CONF = 80
MIN_WORD_CHARS = 3
MIN_WORDS = 2
MIN_MARK_CHARS = 4
CORNERS = {"top_left": (0, 0), "top_right": (0.75, 0), "bottom_left": (0, 0.85), "bottom_right": (0.75, 0.85)}


def _words(image, languages: str, psm: int) -> tuple[list[str], float]:
    """Confident words and the fraction of the image their boxes cover."""
    scale = 2 if image.height < 600 else 1
    img = image.resize((image.width * scale, image.height * scale)) if scale > 1 else image
    buf = io.BytesIO()
    img.convert("L").save(buf, format="PNG")
    res = subprocess.run(["tesseract", "stdin", "stdout", "-l", languages, "--psm", str(psm), "tsv"],
                         input=buf.getvalue(), capture_output=True, timeout=120)
    if res.returncode != 0:
        raise ProproomError("ocr_failed", res.stderr.decode(errors="replace")[:300])
    words, area = [], 0.0
    for row in csv.DictReader(io.StringIO(res.stdout.decode(errors="replace")), delimiter="\t", quoting=csv.QUOTE_NONE):
        text = (row.get("text") or "").strip()
        try:
            conf = float(row.get("conf") or -1)
        except ValueError:
            continue
        if conf >= MIN_CONF and len(re.sub(r"\W", "", text)) >= MIN_WORD_CHARS:
            words.append(text)
            area += int(row["width"]) * int(row["height"])
    return words, area / (img.width * img.height)


def _norm(words: list[str]) -> str:
    return " ".join(re.sub(r"\W", "", w).lower() for w in words)


class OcrFlagsAnalyzer:
    async def analyze(self, ctx: AnalysisContext, representation_id: str) -> AnalysisResult:
        languages = ctx.config.get("languages", "eng")
        sampled = frames(ctx.open_representation(representation_id), POSITIONS)
        video = sampled[0][0] is not None
        bands, corners = [], {name: [] for name in CORNERS}
        for t, im in sampled:
            w, h = im.size
            words, density = _words(im.crop((0, int(h * 0.75), w, h)), languages, 6)
            bands.append({"t": t, "words": words, "density": round(density, 4)})
            for name, (fx, fy) in CORNERS.items():
                box = (int(w * fx), int(h * fy), int(w * (fx + 0.25)), int(h * (fy + 0.15)))
                corners[name].append(_norm(_words(im.crop(box), languages, 11)[0]))

        flags = []
        if video:
            with_text = [b for b in bands if len(b["words"]) >= MIN_WORDS]
            if len(with_text) >= 2 and len({_norm(b["words"]) for b in with_text}) >= 2:
                flags.append(FlagResult(kind="burned_subs", severity="warn",
                                        score=round(len(with_text) / len(bands), 2),
                                        evidence={"frames": with_text, "sampled": len(bands)}))
        marked = {name: texts[0] for name, texts in corners.items()
                  if len(texts[0]) >= MIN_MARK_CHARS and len(set(texts)) == 1}
        if not video:
            band = set(_norm(bands[0]["words"]).split())
            alone = len(marked) == 1 and band <= set(next(iter(marked.values()), "").split())
            marked = marked if alone else {}
        for name, text in marked.items():
            flags.append(FlagResult(kind="watermark", severity="warn", score=1.0,
                                    evidence={"corner": name, "text": text, "frames": len(corners[name])}))
            break
        return AnalysisResult(flags=flags)


Plugin = OcrFlagsAnalyzer
