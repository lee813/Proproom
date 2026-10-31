"""Library config (<home>/config.toml) merged over packaged defaults."""
from __future__ import annotations

import os
import tomllib
from importlib import resources
from typing import Any

from .paths import Home


def default_text() -> str:
    return resources.files("proproom").joinpath("default_config.toml").read_text(encoding="utf-8")


def _merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for k, v in over.items():
        out[k] = _merge(base[k], v) if isinstance(v, dict) and isinstance(base.get(k), dict) else v
    return out


def load(home: Home) -> dict[str, Any]:
    cfg = tomllib.loads(default_text())
    if home.config.is_file():
        cfg = _merge(cfg, tomllib.loads(home.config.read_text()))
    if url := os.environ.get("PROP_PUBLIC_URL"):
        cfg["server"]["public_url"] = url
    return cfg


def load_keys(home: Home) -> dict[str, Any]:
    """API keys per source. A source whose key is absent stays disabled."""
    return tomllib.loads(home.keys.read_text()) if home.keys.is_file() else {}


def policy_rules(cfg: dict) -> dict:
    """Resolved core policy. Plugin restrictions are layered on by core.rights at load time."""
    return {"rights": cfg["rights"]}
