"""Per-type contexts: each plugin type gets only the capabilities it needs (handover §11.2)."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Protocol


class HttpClient(Protocol):
    """Core's HTTP client: rate-limited, UA set, restricted to the plugin's declared [domains]."""
    async def get_json(self, url: str, params: dict[str, Any] | None = None, *, purpose: str = "api") -> Any: ...
    async def get_text(self, url: str, params: dict[str, Any] | None = None, *, purpose: str = "api") -> str: ...


@dataclass
class _Base:
    plugin: str
    log: logging.Logger
    config: dict[str, Any]   # this plugin's section of config.toml, read-only


@dataclass
class SourceContext(_Base):
    http: HttpClient
    keys: dict[str, str]     # this source's entry in keys.toml only


@dataclass
class EmbedContext(_Base):
    device: str              # cuda | xpu | cpu
    dtype: str               # bf16 | fp16 | int8 | fp32
    models_dir: str


@dataclass
class AnalysisContext(_Base):
    open_representation: Any  # callable(rep_id) -> read-only local path


@dataclass
class PolicyContext(_Base):
    policy_version: int


@dataclass
class ExportContext(_Base):
    project: dict[str, Any]
    manifest: Any            # current manifest content, read-only


@dataclass
class HookContext(_Base):
    pass
