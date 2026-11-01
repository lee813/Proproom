"""Plugin loader: <home>/plugins/*.py and dirs with plugin.toml, registry hashes,
reload semantics, typed contexts.

Handover §11.
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import hashlib
import importlib.util
import json
import logging
import re
import sqlite3
import sys
import threading
import time
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
from ..sdk import SDK_VERSION
from ..sdk.errors import ProproomError
from ..sdk.protocols import HoldsModel
from . import db
from .http import ANY_PUBLIC_HOST, RateLimitedClient, build_user_agent
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


def sdk_compatible(spec: str | None) -> bool:
    """True when SDK_VERSION satisfies a manifest's `sdk` range, e.g. ">=1.0,<2". No range means any version."""
    if not spec:
        return True
    have = tuple(int(x) for x in SDK_VERSION.split("."))
    for part in spec.split(","):
        m = re.fullmatch(r"\s*(>=|<=|==|>|<)\s*(\d+(?:\.\d+){0,2})\s*", part)
        if not m:
            raise ProproomError("bad_manifest", f"Unreadable sdk range {spec!r}")
        want = tuple(int(x) for x in m.group(2).split("."))
        want += (0,) * (3 - len(want))
        if not {">=": have >= want, "<=": have <= want, "==": have == want, ">": have > want,
                "<": have < want}[m.group(1)]:
            return False
    return True


class PluginManager:
    def __init__(self, home: Home, instance_id: str = ""):
        self.home = home
        self.instance_id = instance_id
        self.plugins: dict[str, LoadedPlugin] = {}
        self.rejected: dict[str, str] = {}  # name -> why it was not loaded (e.g. an incompatible sdk range)
        self._last_used: dict[str, float] = {}  # plugin name -> monotonic time core last called it
        self._used_lock = threading.Lock()

    def mark_used(self, name: str) -> None:
        """Core calls this before it runs a plugin, so release_idle() knows which models are still in use."""
        with self._used_lock:
            self._last_used[name] = time.monotonic()

    def release_idle(self, idle_s: float) -> list[str]:
        """Release the models of plugins unused for idle_s seconds (handover §4.2). Returns the plugins released."""
        now = time.monotonic()
        with self._used_lock:
            idle = [n for n, t in self._last_used.items() if now - t >= idle_s]
            for n in idle:
                del self._last_used[n]
        released = []
        for n in idle:
            inst = self.plugins[n].instance if n in self.plugins else None
            if isinstance(inst, HoldsModel):
                inst.release()
                released.append(n)
        return released

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
                if not sdk_compatible(p_sec.get("sdk")):
                    self.rejected[name] = f"needs sdk {p_sec.get('sdk')}, core has {SDK_VERSION}"
                    continue

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
            if not sdk_compatible(p_sec.get("sdk")):
                self.rejected[name] = f"needs sdk {p_sec.get('sdk')}, core has {SDK_VERSION}"
                logger.warning("Plugin %s not loaded: %s", name, self.rejected[name])
                continue

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

                    if self.home.db.is_file():
                        try:
                            c = sqlite3.connect(self.home.db)
                            c.execute(
                                "INSERT INTO plugin_registry(name, version, hash, types, origin, enabled, loaded_at) "
                                "VALUES (?, ?, ?, ?, ?, 1, ?) "
                                "ON CONFLICT(name, version, hash) DO UPDATE SET types=excluded.types, loaded_at=excluded.loaded_at",
                                (name, version, p_hash, json.dumps(types), "dir", utcnow()),
                            )
                            c.commit()
                            c.close()
                        except Exception as e:
                            logger.debug("Failed to record plugin in plugin_registry: %s", e)
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

        from . import config
        cfg = config.load(self.home)
        contact = cfg.get("http", {}).get("contact") or self.home.boot.get("http", {}).get("contact", "")
        rate = float(cfg.get("http", {}).get("rate_per_source") or self.home.boot.get("http", {}).get("rate_per_source", 1.0))
        ua = build_user_agent(self.instance_id, contact)
        source_config = cfg.get(plugin_name, {})
        if p and p.manifest.get("source", {}).get("configurable_domains"):
            configured = source_config.get("allowed_domains", [])
            if not isinstance(configured, list) or any(not isinstance(d, str) or not d for d in configured):
                raise ProproomError("bad_config", f"Invalid domain list for {plugin_name}")
            # Nothing configured: the plugin may reach any public site, never a private address.
            allowed_domains.extend(configured or [ANY_PUBLIC_HOST])
        http = RateLimitedClient(ua, rate_per_sec=rate, allowed_domains=allowed_domains, rate_key=plugin_name)

        return SourceContext(
            plugin=plugin_name,
            log=logging.getLogger(f"proproom.plugin.{plugin_name}"),
            config={**(p.manifest if p else {}), **source_config},
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


def run_sync(coro: Any) -> Any:
    """Single canonical sync wrapper for async plugin calls (AGENTS.md)."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop and loop.is_running():
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(asyncio.run, coro).result()
    return asyncio.run(coro)


def plugin_list(ctx: Any, args: dict[str, Any]) -> dict[str, Any]:
    pm = getattr(ctx, "plugin_manager", None) or (ctx.pm() if hasattr(ctx, "pm") else None)
    plugins = []
    if pm:
        for p in pm.plugins.values():
            plugins.append({
                "name": p.name,
                "version": p.version,
                "hash": p.hash,
                "types": p.types,
                "origin": p.origin,
                "status": p.manifest.get("plugin", {}).get("status") or p.manifest.get("status", "ready"),
            })
    return {"plugins": plugins}


def plugin_reload(ctx: Any, args: dict[str, Any]) -> dict[str, Any]:
    b = ctx.booted
    pm = getattr(ctx, "plugin_manager", None) or (ctx.pm() if hasattr(ctx, "pm") else None)
    conn = db.connect(b.home.db)
    try:
        conn.execute("BEGIN IMMEDIATE")
        if pm:
            pm.load_all(conn)
        conn.execute("COMMIT")
    finally:
        conn.close()
    return {"reloaded": True, "plugins": [p.name for p in pm.plugins.values()] if pm else []}


def plugin_new(ctx: Any, args: dict[str, Any]) -> dict[str, Any]:
    name = args["name"]
    types = args["types"]
    p_dir = ctx.booted.home.dir("plugins") / name
    p_dir.mkdir(parents=True, exist_ok=True)

    manifest_content = f"""[plugin]
name = "{name}"
version = "0.0.1"
sdk = ">=1.0,<2"
types = {json.dumps(types)}
status = "stub"
"""
    (p_dir / "plugin.toml").write_text(manifest_content, encoding="utf-8")
    (p_dir / "__init__.py").write_text(f'"""Plugin {name}."""\n\nclass Plugin:\n    pass\n', encoding="utf-8")
    return {"created": str(p_dir)}


def plugin_test(ctx: Any, args: dict[str, Any]) -> dict[str, Any]:
    name = args["name"]
    pm = getattr(ctx, "plugin_manager", None) or (ctx.pm() if hasattr(ctx, "pm") else None)
    p = pm.plugins.get(name) if pm else None
    if not p:
        raise ProproomError("plugin_not_found", f"Plugin {name} not found")
    raise ProproomError("not_implemented", "The plugin contract runner is not implemented")


