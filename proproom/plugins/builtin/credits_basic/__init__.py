"""Basic credits exporter: markdown / plain text grouped by basis."""
from __future__ import annotations

from typing import Any

from ....sdk.context import ExportContext
from ....sdk.types import ExportPlan


class CreditsBasicExporter:
    def plan(self, ctx: ExportContext, usages: list[dict[str, Any]]) -> ExportPlan:
        lines = ["# Credits\n"]
        for u in usages:
            lines.append(f"- {u.get('title')}: {u.get('artist', 'Unknown')} ({u.get('license', 'All Rights Reserved')})")
        return ExportPlan(
            manifest_relpath="CREDITS.md",
            manifest_patch=None,
            credits_text="\n".join(lines),
        )


Plugin = CreditsBasicExporter
