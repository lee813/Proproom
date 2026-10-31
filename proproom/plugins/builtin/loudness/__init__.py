"""EBU R128 loudness measurement (integrated LUFS, loudness range LRA, true peak).

Handover §13.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

from ....sdk.context import AnalysisContext
from ....sdk.types import AnalysisResult


class LoudnessAnalyzer:
    async def analyze(self, ctx: AnalysisContext, representation_id: str) -> AnalysisResult:
        path = ctx.open_representation(representation_id)
        if not path or not Path(path).is_file():
            return AnalysisResult()

        cmd = [
            "ffmpeg", "-hide_banner", "-nostats",
            "-i", str(path),
            "-filter:a", "ebur128=framelog=verbose",
            "-f", "null", "-",
        ]
        try:
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
            m_lufs = re.search(r"Integrated loudness:\s+I:\s+([-\d.]+)\s+LUFS", res.stderr)
            m_lra = re.search(r"Loudness range:\s+LRA:\s+([-\d.]+)\s+LU", res.stderr)
            m_peak = re.search(r"True peak:\s+Peak:\s+([-\d.]+)\s+dBFS", res.stderr)

            lufs = float(m_lufs.group(1)) if m_lufs else None
            lra = float(m_lra.group(1)) if m_lra else None
            peak = float(m_peak.group(1)) if m_peak else None

            evidence = {"lufs": lufs, "lra": lra, "true_peak": peak}
            return AnalysisResult(evidence=evidence)
        except Exception:
            return AnalysisResult()


Plugin = LoudnessAnalyzer
