"""The only surface plugins may import (semver). Frozen at 1.0 (2026-10-31): commons, archive_org and capture pass
the source contract tests. Breaking changes need 2.0."""
SDK_VERSION = "1.0.0"

from .errors import ProproomError  # noqa: E402
from .protocols import Analyzer, Embedder, Exporter, Hook, Policy, Ranker, Segmenter, Source  # noqa: E402
from .types import *  # noqa: E402,F401,F403

__all__ = ["SDK_VERSION", "ProproomError", "Source", "Embedder", "Analyzer", "Segmenter", "Policy", "Ranker",
           "Exporter", "Hook"]
