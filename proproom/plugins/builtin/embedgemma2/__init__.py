"""EmbeddingGemma 2 embedder plugin (768d truncated to 256d via MRL).

Handover §4.2, §7.
"""
from __future__ import annotations

import hashlib
from pathlib import Path
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

    def __init__(self) -> None:
        self._model = None

    async def embed(self, ctx: EmbedContext, inputs: list[dict[str, Any]]) -> EmbeddingBatch:
        from ....sdk.errors import ProproomError
        try:
            import sentence_transformers
            from sentence_transformers import SentenceTransformer
        except ImportError as e:
            raise ProproomError("model_unavailable", f"sentence_transformers unavailable: {e}")

        for inp in inputs:
            if not any(key in inp for key in ("text", "image", "video", "audio")):
                raise ProproomError("bad_argument", "Embedding input must contain text or media")

        # Keep loaded model between batches
        if self._model is None:
            self._model = SentenceTransformer(
                self.fingerprint["model"],
                cache_folder=ctx.models_dir,
                device=ctx.device,
            )

        items_to_encode = []
        hashes = []
        for inp in inputs:
            prompt_req = inp.get("prompt", "document")
            prompt_name = "SearchQuery" if prompt_req == "query" else "Document"

            if "text" in inp:
                raw_text = inp["text"]
                items_to_encode.append((raw_text, prompt_name))
                hashes.append(hashlib.sha256(raw_text.encode("utf-8")).hexdigest())
            else:
                found_media = False
                for media_key in ("image", "video", "audio"):
                    if media_key in inp:
                        p_val = inp[media_key]
                        raw_bytes = Path(p_val).read_bytes()
                        items_to_encode.append(({media_key: p_val}, prompt_name))
                        hashes.append(hashlib.sha256(raw_bytes).hexdigest())
                        found_media = True
                        break
                if not found_media:
                    raise ProproomError("bad_argument", "Embedding input must contain text or media")

        embeddings = []
        for item, prompt_name in items_to_encode:
            emb = self._model.encode(item, prompt_name=prompt_name, normalize_embeddings=True)
            # Truncate to self.dim (MRL) and renormalize
            vec = emb[:self.dim].tolist() if hasattr(emb, "tolist") else list(emb[:self.dim])
            norm = sum(x * x for x in vec) ** 0.5 or 1.0
            vec = [x / norm for x in vec]
            embeddings.append(vec)

        return EmbeddingBatch(space_fingerprint=self.fingerprint, vectors=embeddings, input_hashes=hashes)


Plugin = EmbedGemma2Embedder
