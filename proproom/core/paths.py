"""Boot-time library location (handover §4.1).

Resolution order:
  1. env PROP_HOME
  2. `home` in /nas/.proproom.toml (relative paths are relative to /nas)
  3. /nas/proproom

The library location is never stored inside the library itself.
"""
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path

LIBRARY_DIRS = ("blobs", "thumbs", "models", "plugins", "tmp", "journal", "backups")


def nas_root() -> Path:
    return Path(os.environ.get("PROP_NAS", "/nas"))


@dataclass(frozen=True)
class Home:
    root: Path
    nas: Path
    source: str  # env | boot_file | default
    boot: dict

    @property
    def db(self) -> Path:
        return self.root / "db.sqlite"

    @property
    def config(self) -> Path:
        return self.root / "config.toml"

    @property
    def keys(self) -> Path:
        return self.root / "keys.toml"

    @property
    def token(self) -> Path:
        return self.root / "instance.token"

    def dir(self, name: str) -> Path:
        assert name in LIBRARY_DIRS, name
        return self.root / name

    def nas_path(self, rel: str | Path) -> Path:
        """Resolve a /nas-relative path and refuse anything that escapes /nas."""
        p = (self.nas / rel).resolve()
        if p != self.nas.resolve() and self.nas.resolve() not in p.parents:
            raise ValueError(f"path escapes {self.nas}: {rel}")
        return p


def resolve_home() -> Home:
    nas = nas_root()
    boot_file = nas / ".proproom.toml"
    boot: dict = {}
    if boot_file.is_file():
        boot = tomllib.loads(boot_file.read_text())

    if env := os.environ.get("PROP_HOME"):
        return Home(Path(env), nas, "env", boot)
    if home := boot.get("home"):
        p = Path(home)
        return Home(p if p.is_absolute() else nas / p, nas, "boot_file", boot)
    return Home(nas / "proproom", nas, "default", boot)
