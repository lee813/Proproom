"""Region analyzer: face, number-plate, logo, text and main-subject boxes (handover §13).

- Faces: OpenCV YuNet (MIT), a dedicated face detector. On the live library it found every face at 0.93-0.95
  with no false positives, where OWLv2's "face" query scored real faces 0.12-0.33 and boxed half a face.
- Plates, logos, text, main subject: OWLv2 zero-shot detection (Apache-2.0) with text queries. Thresholds were
  calibrated on the live library's photos (2026-10-31).

Boxes are fractions of the frame, so thumbnails can be blurred at any size. A logo also raises a `logo` flag for
review. Videos are measured on their middle frame, the one the thumbnail uses.
"""
from __future__ import annotations

import gc
import threading

from ....sdk.context import AnalysisContext
from ....sdk.media import frames
from ....sdk.types import AnalysisResult, FlagResult

MODEL = "google/owlv2-base-patch16-ensemble"
FACE_MODEL = ("opencv/face_detection_yunet", "face_detection_yunet_2023mar.onnx")
FACE_MAX_SIDE = 1920
QUERIES = [
    ("plate", "a vehicle license plate"),
    ("logo", "a company logo"),
    ("text", "a block of printed text"),
    ("subject", "a person"),
    ("subject", "an animal"),
    ("subject", "a vehicle"),
    ("subject", "a ship"),
    ("subject", "a building"),
]
THRESHOLDS = {"face": 0.75, "plate": 0.2, "logo": 0.3, "text": 0.2, "subject": 0.15}
NMS_IOU = 0.3


def _iou(a: tuple, b: tuple) -> float:
    ix = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
    inter = ix * iy
    union = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / union if union > 0 else 0.0


class RegionsAnalyzer:
    def __init__(self) -> None:
        self._model = None
        self._face_model = None
        self._lock = threading.Lock()

    def faces(self, image, threshold: float) -> list[dict]:
        import cv2
        import numpy as np
        with self._lock:
            if self._face_model is None:
                from huggingface_hub import hf_hub_download
                self._face_model = hf_hub_download(*FACE_MODEL)
        w, h = image.size
        scale = min(1.0, FACE_MAX_SIDE / max(w, h))
        iw, ih = max(1, int(w * scale)), max(1, int(h * scale))
        frame = cv2.cvtColor(np.asarray(image.resize((iw, ih)) if scale < 1 else image), cv2.COLOR_RGB2BGR)
        detector = cv2.FaceDetectorYN.create(self._face_model, "", (iw, ih), threshold, 0.3, 5000)
        _, found = detector.detect(frame)
        out = []
        for f in [] if found is None else found.tolist():
            x0, y0 = max(0.0, f[0]) / iw, max(0.0, f[1]) / ih
            x1, y1 = min(float(iw), f[0] + f[2]) / iw, min(float(ih), f[1] + f[3]) / ih
            if x1 > x0 and y1 > y0:
                out.append({"kind": "face", "x": x0, "y": y0, "w": x1 - x0, "h": y1 - y0, "score": round(f[14], 4)})
        return out

    def _load(self):
        with self._lock:
            if self._model is None:
                import torch
                from transformers import Owlv2ForObjectDetection, Owlv2Processor
                device = "cuda" if torch.cuda.is_available() else "cpu"
                processor = Owlv2Processor.from_pretrained(MODEL)
                model = Owlv2ForObjectDetection.from_pretrained(MODEL).to(device).eval()
                self._model = (torch, processor, model, device)
            return self._model

    def release(self) -> None:
        """Drop OWLv2 and hand the GPU memory back when idle (handover §4.2). YuNet is a file path, not a model."""
        with self._lock:
            if self._model is None:
                return
            torch = self._model[0]
            self._model = None
            gc.collect()
            if torch.cuda.is_available():
                torch.cuda.empty_cache()

    def detect(self, image, thresholds: dict[str, float]) -> list[dict]:
        torch, processor, model, device = self._load()
        w, h = image.size
        inputs = processor(text=[[q for _, q in QUERIES]], images=image, return_tensors="pt").to(device)
        with torch.no_grad():
            outputs = model(**inputs)
        side = max(w, h)  # OWLv2 pads to a square; boxes come back in padded coordinates
        res = processor.post_process_grounded_object_detection(
            outputs=outputs, threshold=min(v for k, v in thresholds.items() if k != "face"),
            target_sizes=torch.tensor([[side, side]]))[0]
        found: dict[str, list] = {}
        for score, label, box in zip(res["scores"].tolist(), res["labels"].tolist(), res["boxes"].tolist()):
            kind = QUERIES[label][0]
            if score < thresholds[kind]:
                continue
            x0, y0 = max(0.0, box[0]) / w, max(0.0, box[1]) / h
            x1, y1 = min(float(w), box[2]) / w, min(float(h), box[3]) / h
            if x1 > x0 and y1 > y0:
                found.setdefault(kind, []).append((score, (x0, y0, x1, y1)))
        out = []
        for kind, boxes in found.items():
            kept: list = []
            for score, b in sorted(boxes, reverse=True):
                if all(_iou(b, k[1]) < NMS_IOU for k in kept):
                    kept.append((score, b))
            if kind == "subject":
                kept = kept[:1]  # the main subject only
            out += [{"kind": kind, "x": b[0], "y": b[1], "w": b[2] - b[0], "h": b[3] - b[1], "score": round(s, 4)}
                    for s, b in kept]
        return out

    async def analyze(self, ctx: AnalysisContext, representation_id: str) -> AnalysisResult:
        thresholds = {**THRESHOLDS, **ctx.config.get("thresholds", {})}
        (t, image), = frames(ctx.open_representation(representation_id), [0.5])
        found = self.faces(image, thresholds["face"]) + self.detect(image, thresholds)
        regions = [{**r, "t": t} for r in found]
        logos = [r for r in regions if r["kind"] == "logo"]
        flags = [FlagResult(kind="logo", severity="warn", score=max(r["score"] for r in logos),
                            evidence={"model": MODEL, "boxes": len(logos), "t": t})] if logos else []
        return AnalysisResult(regions=regions, flags=flags)


Plugin = RegionsAnalyzer
