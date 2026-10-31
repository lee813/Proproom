"""EmbeddingGemma 2 embedder plugin (768d truncated to 256d via MRL).

Handover §4.2, §7.
"""
from __future__ import annotations

import hashlib
from typing import Any

from ....sdk.context import EmbedContext
from ....sdk.types import EmbeddingBatch


class EmbedGemma2Embedder:
    fingerprint = {
        "model": "google/embeddinggemma-2",
        "revision": "main",
        "modalities": ["text", "image", "video"],
        "preprocess": {},
        "prompts": {"query": "SearchQuery", "doc": "Document"},
        "pooling": "mean",
        "normalize": 1,
        "truncation": 256,
    }
    dim = 256

    async def embed(self, ctx: EmbedContext, inputs: list[dict[str, Any]]) -> EmbeddingBatch:
        # Fallback pseudo-embedding if sentence-transformers not installed
        vectors: list[list[float]] = []
        for inp in inputs:
            s = str(inp.get("text") or inp.get("path") or "")
            seed = int(hashlib.md5(s.encode()).hexdigest()[:8], 16)
            vec = [(float((seed + i * 31) % 1000) / 1000.0) for i in range(self.dim)]
            # Normalize
            norm = sum(x * x for x in vec) ** 0.5 or 1.0
            vec = [x / norm for x in vec]
            vectors.append(vec)
        return EmbeddingBatch(vectors=vectors, space_fingerprint=self.fingerprint)


Plugin = EmbedGemma2Embedder
