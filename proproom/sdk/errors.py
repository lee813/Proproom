"""Uniform error shape for CLI / HTTP / MCP (handover §8). Codes are listed in docs/errors.md."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class ProproomError(Exception):
    code: str
    message: str
    hint: str | None = None
    retryable: bool = False
    retry_after: float | None = None
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def not_implemented(op: str) -> ProproomError:
    return ProproomError("not_implemented", f"operation '{op}' is declared but not implemented yet",
                         hint="see docs/HANDOVER.md §15 for the build order")
