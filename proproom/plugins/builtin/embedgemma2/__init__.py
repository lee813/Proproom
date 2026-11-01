"""EmbeddingGemma 2 embedder plugin (768d truncated to 256d via MRL).

Handover §4.2, §7.
"""
from __future__ import annotations

import gc
import hashlib
import threading
from pathlib import Path
from typing import Any

from ....sdk.context import EmbedContext
from ....sdk.types import EmbeddingBatch

# EmbedContext.dtype -> torch dtype name. The tier picks it (handover §4.2): bf16 on Ampere+, fp16 on Turing, fp32 on CPU.
TORCH_DTYPES = {"bf16": "bfloat16", "fp16": "float16", "fp32": "float32"}


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
        self._loaded_as: tuple[str, str] | None = None  # (device, dtype) the model was loaded with
        self._lock = threading.Lock()  # one process shares this model between the worker and request threads

    def _load(self, ctx: EmbedContext) -> Any:
        from ....sdk.errors import ProproomError
        try:
            import torch
            from sentence_transformers import SentenceTransformer
        except ImportError as e:
            raise ProproomError("model_unavailable", f"sentence_transformers unavailable: {e}")
        if ctx.dtype not in TORCH_DTYPES:
            raise ProproomError("unsupported_dtype", f"EmbeddingGemma 2 has no {ctx.dtype} build here",
                                hint=f"one of {sorted(TORCH_DTYPES)}")
        if self._model is None or self._loaded_as != (ctx.device, ctx.dtype):
            self._model = None
            self._model = SentenceTransformer(
                self.fingerprint["model"],
                cache_folder=ctx.models_dir,
                device=ctx.device,
                model_kwargs={"dtype": getattr(torch, TORCH_DTYPES[ctx.dtype]), "low_cpu_mem_usage": True},
            )
            self._loaded_as = (ctx.device, ctx.dtype)
        return self._model

    def release(self) -> None:
        """Drop the weights and hand the GPU memory back (handover §4.2: unload after idle_unload_s)."""
        with self._lock:
            if self._model is None:
                return
            self._model, self._loaded_as = None, None
            gc.collect()
            import torch
            if torch.cuda.is_available():
                torch.cuda.empty_cache()

    async def embed(self, ctx: EmbedContext, inputs: list[dict[str, Any]]) -> EmbeddingBatch:
        from ....sdk.errors import ProproomError

        for inp in inputs:
            if not any(key in inp for key in ("text", "image", "video", "audio")):
                raise ProproomError("bad_argument", "Embedding input must contain text or media")

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
        with self._lock:
            # Kept loaded between batches; release() frees it when idle.
            model = self._load(ctx)
            encoded = [model.encode(item, prompt_name=prompt_name, normalize_embeddings=True)
                       for item, prompt_name in items_to_encode]
        for emb in encoded:
            # Truncate to self.dim (MRL) and renormalize
            vec = emb[:self.dim].tolist() if hasattr(emb, "tolist") else list(emb[:self.dim])
            norm = sum(x * x for x in vec) ** 0.5 or 1.0
            vec = [x / norm for x in vec]
            embeddings.append(vec)

        return EmbeddingBatch(space_fingerprint=self.fingerprint, vectors=embeddings, input_hashes=hashes)


Plugin = EmbedGemma2Embedder
