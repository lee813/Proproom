"""Pull operations: validates spec and enqueues background pull job."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from . import db, jobs
from ..sdk.errors import ProproomError
import re


def parse_budget(value: Any) -> int:
    match = re.fullmatch(r"(\d+(?:\.\d+)?)\s*([KMG]?)", str(value).strip().upper())
    if not match:
        raise ProproomError("bad_argument", "Invalid byte budget")
    amount = float(match.group(1)) * {"": 1, "K": 1024, "M": 1024**2, "G": 1024**3}[match.group(2)]
    if not 0 < amount <= 2**63 - 1:
        raise ProproomError("bad_argument", "Byte budget must be positive and fit in 64 bits")
    return int(amount)


def pull(ctx: Any, args: dict[str, Any]) -> dict[str, Any]:
    """Bulk pull from a pull.yaml as a background job with a byte budget."""
    b = ctx.booted
    spec_path_str = args["spec"]
    budget_arg = args.get("budget", "1G")

    budget_bytes = parse_budget(budget_arg)

    spec_p = Path(spec_path_str)
    if not spec_p.is_absolute():
        spec_p = b.home.nas / spec_path_str

    conn = db.connect(b.home.db)
    try:
        conn.execute("BEGIN IMMEDIATE")
        job_id = jobs.create_job(
            conn, "pull", {"spec": spec_path_str, "budget": budget_arg}, budget_bytes=budget_bytes
        )
        conn.execute("COMMIT")

        if not spec_p.is_file():
            conn.execute("BEGIN IMMEDIATE")
            jobs.finish_job(conn, job_id, "failed", {"error": f"Spec file not found: {spec_path_str}"})
            conn.execute("COMMIT")
            return {"job_id": job_id, "state": "failed", "progress": {"error": "spec_file_not_found"}}

        return {"job_id": job_id, "state": "queued"}
    finally:
        conn.close()
