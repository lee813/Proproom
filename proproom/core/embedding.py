"""The one way core calls an embedder (handover §4.2, §7): find it, give it the tier's device and dtype, refuse to
run on the CPU when the GPU went away, and record the use so the model is released when idle."""
from __future__ import annotations

import logging
from typing import Any

from . import hardware
from .paths import Home
from .plugins import LoadedPlugin, PluginManager, run_sync
from ..sdk.context import EmbedContext
from ..sdk.errors import ProproomError
from ..sdk.types import EmbeddingBatch


def loaded(pm: PluginManager) -> LoadedPlugin | None:
    for plugin in pm.plugins.values():
        if "embedder" in plugin.types and plugin.instance and plugin.manifest.get("plugin", {}).get("status") != "stub":
            return plugin
    return None


def find(pm: PluginManager) -> LoadedPlugin:
    plugin = loaded(pm)
    if plugin is None:
        raise ProproomError("no_embedder", "No embedder plugin is loaded")
    return plugin


def context(home: Home, plugin: LoadedPlugin, log: logging.Logger) -> EmbedContext:
    device, dtype = hardware.embed_device()
    return EmbedContext(plugin=plugin.name, log=log, config={}, device=device, dtype=dtype,
                        models_dir=str(home.dir("models")))


def embed(pm: PluginManager, plugin: LoadedPlugin, ectx: EmbedContext, inputs: list[dict[str, Any]]) -> EmbeddingBatch:
    hardware.require_gpu()
    pm.mark_used(plugin.name)
    return run_sync(plugin.instance.embed(ectx, inputs))
