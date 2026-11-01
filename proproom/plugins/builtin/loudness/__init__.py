"""EBU R128 loudness measurement (integrated LUFS, loudness range LRA, true peak).

Handover §13. A file with no audio stream, or whose audio is digital silence
(true peak -inf), has nothing to measure and returns no measurements.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

from ....sdk.context import AnalysisContext
from ....sdk.media import decode_timeout, probe
from ....sdk.types import AnalysisResult


class LoudnessAnalyzer:
    async def analyze(self, ctx: AnalysisContext, representation_id: str) -> AnalysisResult:
        path = ctx.open_representation(representation_id)
        if not path or not Path(path).is_file():
            raise FileNotFoundError(f"Representation not found: {representation_id}")

        info = probe(Path(path))
        if not info["audio"]:
            return AnalysisResult()
        cmd = [
            "ffmpeg", "-hide_banner", "-nostats",
            "-i", str(path), "-map", "0:a:0", "-vn",
            "-filter:a", "ebur128=peak=true:framelog=verbose",
            "-f", "null", "-",
        ]
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=decode_timeout(info["duration"]), check=True)
        m_lufs = re.search(r"Integrated loudness:\s+I:\s+([-\d.]+)\s+LUFS", res.stderr)
        m_lra = re.search(r"Loudness range:\s+LRA:\s+([-\d.]+)\s+LU", res.stderr)
        m_peak = re.search(r"True peak:\s+Peak:\s+([-\d.]+|-inf)\s+dBFS", res.stderr)
        if m_peak and m_peak.group(1) == "-inf":
            return AnalysisResult()
        if not m_lufs or not m_lra or not m_peak:
            raise ValueError("ffmpeg did not report complete loudness measurements")
        return AnalysisResult(measurements={"lufs": float(m_lufs.group(1)), "lra": float(m_lra.group(1)),
                                            "true_peak": float(m_peak.group(1))})


Plugin = LoudnessAnalyzer
