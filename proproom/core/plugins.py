"""Plugin loader: <home>/plugins/*.py and dirs with plugin.toml, registry hashes,
reload semantics, typed contexts.

Handover §11.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import logging
import sqlite3
import sys
import tomllib
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Any

from ..sdk.context import (
    AnalysisContext,
    EmbedContext,
    ExportContext,
    HookContext,
    PolicyContext,
    SourceContext,
)
from .http import RateLimitedClient, build_user_agent
from .ids import utcnow
from .paths import Home

logger = logging.getLogger("proproom.plugins")


@dataclass
class LoadedPlugin:
    name: str
    version: str
    hash: str
    types: list[str]
    origin: str
    manifest: dict[str, Any]
    instance: Any = None
    path: Path | None = None


def hash_path(path: Path) -> str:
    """Compute sha256 hash of a file or directory tree."""
    if path.is_file():
        return hashlib.sha256(path.read_bytes()).hexdigest()
    h = hashlib.sha256()
    for child in sorted(path.rglob("*")):
        if child.is_file() and not child.name.startswith((".", "__pycache__")):
            rel = str(child.relative_to(path))
            h.update(rel.encode("utf-8"))
            h.update(child.read_bytes())
    return h.hexdigest()


class PluginManager:
    def __init__(self, home: Home, instance_id: str = ""):
        self.home = home
        self.instance_id = instance_id
        self.plugins: dict[str, LoadedPlugin] = {}

    def load_all(self, conn: sqlite3.Connection | None = None) -> None:
        """Scan and load builtin and directory plugins."""
        self._load_builtins()
        self._load_directory_plugins()
        if conn:
            self._sync_registry(conn)

    def _sync_registry(self, conn: sqlite3.Connection) -> None:
        now = utcnow()
        for p in self.plugins.values():
            types_json = json.dumps(p.types)
            conn.execute(
                "INSERT INTO plugin_registry(name, version, hash, types, origin, enabled, loaded_at) "
                "VALUES (?, ?, ?, ?, ?, 1, ?) "
                "ON CONFLICT(name, version, hash) DO UPDATE SET loaded_at = excluded.loaded_at",
                (p.name, p.version, p.hash, types_json, p.origin, now),
            )

    def _load_builtins(self) -> None:
        root = resources.files("proproom").joinpath("plugins", "builtin")
        for d in sorted(root.iterdir(), key=lambda p: p.name):
            manifest_res = d.joinpath("plugin.toml")
            if not manifest_res.is_file():
                continue
            try:
                manifest = tomllib.loads(manifest_res.read_text(encoding="utf-8"))
                p_sec = manifest.get("plugin", {})
                name = p_sec.get("name", d.name)
                version = p_sec.get("version", "0.0.1")
                types = p_sec.get("types", [])

                # Load module
                mod_name = f"proproom.plugins.builtin.{d.name}"
                mod = importlib.import_module(mod_name)
                instance = getattr(mod, "Plugin", None) or mod
                if callable(instance) and isinstance(instance, type):
                    instance = instance()

                # Hash builtin dir if on disk
                try:
                    p_path = Path(str(d))
                    p_hash = hash_path(p_path) if p_path.exists() else "0" * 64
                except Exception:
                    p_hash = "0" * 64

                self.plugins[name] = LoadedPlugin(
                    name=name,
                    version=version,
                    hash=p_hash,
                    types=types,
                    origin="builtin",
                    manifest=manifest,
                    instance=instance,
                )
            except Exception as e:
                logger.warning("Failed to load builtin plugin %s: %s", d.name, e)

    def _load_directory_plugins(self) -> None:
        p_dir = self.home.dir("plugins")
        if not p_dir.is_dir():
            return

        for entry in sorted(p_dir.iterdir(), key=lambda p: p.name):
            if entry.name.startswith((".", "__pycache__")):
                continue

            manifest_file = entry / "plugin.toml" if entry.is_dir() else None
            py_file = entry / "__init__.py" if entry.is_dir() else (entry if entry.suffix == ".py" else None)

            if not py_file or not py_file.is_file():
                continue

            manifest = {}
            if manifest_file and manifest_file.is_file():
                manifest = tomllib.loads(manifest_file.read_text(encoding="utf-8"))

            p_sec = manifest.get("plugin", {})
            name = p_sec.get("name", entry.stem)
            version = p_sec.get("version", "0.0.1")
            types = p_sec.get("types", [])

            p_hash = hash_path(entry)

            # Load python file
            spec = importlib.util.spec_from_file_location(f"proproom_dir_plugin_{name}", py_file)
            if spec and spec.loader:
                mod = importlib.util.module_from_spec(spec)
                sys.modules[spec.name] = mod
                try:
                    spec.loader.exec_module(mod)
                    instance = getattr(mod, "Plugin", None) or mod
                    if callable(instance) and isinstance(instance, type):
                        instance = instance()

                    self.plugins[name] = LoadedPlugin(
                        name=name,
                        version=version,
                        hash=p_hash,
                        types=types,
                        origin="dir",
                        manifest=manifest,
                        instance=instance,
                        path=entry,
                    )
                except Exception as e:
                    logger.warning("Failed to exec plugin %s: %s", name, e)

    def get_source_context(self, plugin_name: str) -> SourceContext:
        p = self.plugins.get(plugin_name)
        allowed_domains = []
        if p:
            doms = p.manifest.get("domains", {})
            for lst in doms.values():
                if isinstance(lst, list):
                    allowed_domains.extend(lst)

        contact = self.home.boot.get("http", {}).get("contact", "")
        ua = build_user_agent(self.instance_id, contact)
        rate = float(self.home.boot.get("http", {}).get("rate_per_source", 1.0))
        http = RateLimitedClient(ua, rate_per_sec=rate, allowed_domains=allowed_domains)

        return SourceContext(
            plugin=plugin_name,
            log=logging.getLogger(f"proproom.plugin.{plugin_name}"),
            config=p.manifest if p else {},
            http=http,
            keys={},
        )

    def get_export_context(self, plugin_name: str, project_data: dict[str, Any], manifest_data: Any = None) -> ExportContext:
        p = self.plugins.get(plugin_name)
        return ExportContext(
            plugin=plugin_name,
            log=logging.getLogger(f"proproom.plugin.{plugin_name}"),
            config=p.manifest if p else {},
            project=project_data,
            manifest=manifest_data,
        )

    def get_policy_context(self, plugin_name: str, policy_version: int) -> PolicyContext:
        p = self.plugins.get(plugin_name)
        return PolicyContext(
            plugin=plugin_name,
            log=logging.getLogger(f"proproom.plugin.{plugin_name}"),
            config=p.manifest if p else {},
            policy_version=policy_version,
        )
