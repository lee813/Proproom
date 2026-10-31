"""Generic assets.json exporter plugin.

Generates manifest patches and credits for project deliveries.
"""
from __future__ import annotations

from typing import Any

from ....sdk.context import ExportContext
from ....sdk.types import ExportPlan


class AssetsJsonExporter:
    def plan(self, ctx: ExportContext, usages: list[dict[str, Any]]) -> ExportPlan:
        manifest_entries = []
        for u in usages:
            entry = {
                "id": u.get("as_name"),
                "kind": u.get("kind", "photo"),
                "file": u.get("delivered_relpath"),
                "desc": u.get("title", ""),
                "source": u.get("source_url", ""),
                "license": u.get("license", ""),
                "artist": u.get("artist", ""),
            }
            manifest_entries.append(entry)

        return ExportPlan(
            manifest_relpath="assets.json",
            manifest_patch={"assets": manifest_entries},
            credits_text="",
        )


Plugin = AssetsJsonExporter
