"""FFmpeg scene detection: threshold 0.3, min 2s, max 10s.

Handover §13.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

from ....sdk.context import AnalysisContext
from ....sdk.media import decode_timeout
from ....sdk.types import SegmentProposal


class ScenecutSegmenter:
    async def segment(self, ctx: AnalysisContext, representation_id: str) -> list[SegmentProposal]:
        path = ctx.open_representation(representation_id)
        if not path or not Path(path).is_file():
            raise FileNotFoundError(f"Representation not found: {representation_id}")

        p_res = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
            capture_output=True, text=True, timeout=10, check=True)
        dur = float(p_res.stdout.strip())
        cmd = ["ffmpeg", "-hide_banner", "-nostats", "-i", str(path), "-map", "0:v:0", "-an",
               "-filter:v", "select='gt(scene,0.3)',showinfo", "-f", "null", "-"]
        pts_times = [0.0]
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=decode_timeout(dur), check=True)
        for m in re.finditer(r"pts_time:([0-9.]+)", res.stderr):
            t = float(m.group(1))
            if t - pts_times[-1] >= 2.0:
                pts_times.append(t)

        if dur is not None and dur > pts_times[-1]:
            pts_times.append(dur)

        # Build segments: min 2s, max 10s
        proposals: list[SegmentProposal] = []
        for i in range(len(pts_times) - 1):
            t0 = pts_times[i]
            t1 = pts_times[i + 1]
            # If segment > 10s, split
            while t1 - t0 > 10.0:
                proposals.append(SegmentProposal(
                    t0=t0, t1=t0 + 10.0, algo="scenecut", algo_version="0.1", params={"threshold": 0.3}
                ))
                t0 += 10.0
            if t1 - t0 >= 2.0:
                proposals.append(SegmentProposal(
                    t0=t0, t1=t1, algo="scenecut", algo_version="0.1", params={"threshold": 0.3}
                ))
        return proposals


Plugin = ScenecutSegmenter
