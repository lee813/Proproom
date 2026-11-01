"""Background worker: ticks through due jobs and event deliveries.

Contract (§11.2 / §13):
- worker.tick(ctx) -> bool runs one unit of work (an event delivery or a job step)
  using ctx.pm() / ctx.plugin_manager, returning True if work was performed, False if idle.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import logging
import sqlite3
from pathlib import Path
from typing import Any

from . import db, hardware, jobs
from .fetch import evaluate_source_rights, fetch
from .ids import utcnow
from .plugins import run_sync
from ..sdk.context import HookContext
from ..sdk.errors import ProproomError
from ..sdk.types import EnumerateRequest, RepresentationOffer, RightsEvidence, SearchRequest, SourceRef

logger = logging.getLogger("proproom.worker")
MAX_PULL_ERRORS = 5
# Job kinds the worker advances step by step. Other kinds (import_project, verify) run inside the operation that
# created them; that call owns the job row while it is `running`, and the worker must not touch it.
STEPPED_KINDS = ("pull", "index")


def tick(ctx: Any) -> bool:
    """Run one unit of background work (event delivery or job progress). Returns True if work was done."""
    b = ctx.booted
    hardware.require_gpu()
    pm = getattr(ctx, "plugin_manager", None) or (ctx.pm() if hasattr(ctx, "pm") else None)
    conn = db.connect(b.home.db)
    try:
        # 1. Check for pending event deliveries
        deliv_row = conn.execute(
            "SELECT ed.event_id, ed.subscriber, ed.attempts, e.kind, e.payload "
            "FROM event_delivery ed "
            "JOIN event e ON e.id = ed.event_id "
            "WHERE ed.state IN ('pending', 'failed') AND (ed.next_attempt IS NULL OR ed.next_attempt <= ?) "
            "ORDER BY ed.event_id ASC LIMIT 1", (utcnow(),)
        ).fetchone()

        if deliv_row:
            event_id = deliv_row["event_id"]
            sub = deliv_row["subscriber"]
            attempts = deliv_row["attempts"] + 1
            kind = deliv_row["kind"]
            payload = json.loads(deliv_row["payload"])

            plugin = pm.plugins.get(sub) if pm else None
            inst = plugin.instance if plugin else None
            err = None

            try:
                if not inst:
                    raise ProproomError("hook_unavailable", f"Hook {sub} is unavailable")
                hook_ctx = HookContext(plugin=sub, log=logging.getLogger(f"proproom.plugin.{sub}"),
                                       config=plugin.manifest)
                run_sync(inst.handle(hook_ctx, event_id, kind, payload))
            except Exception as e:
                err = str(e)

            conn.execute("BEGIN IMMEDIATE")
            if err is None:
                conn.execute(
                    "UPDATE event_delivery SET state = 'delivered', attempts = ? WHERE event_id = ? AND subscriber = ?",
                    (attempts, event_id, sub),
                )
            else:
                new_state = "dead" if attempts >= 5 else "failed"
                conn.execute(
                    "UPDATE event_delivery SET state = ?, attempts = ?, last_error = ?, next_attempt = ? WHERE event_id = ? AND subscriber = ?",
                    (new_state, attempts, err, (datetime.now(timezone.utc) + timedelta(seconds=min(3600, 2 ** attempts))).isoformat(timespec="milliseconds").replace("+00:00", "Z"), event_id, sub),
                )
            conn.execute("COMMIT")
            return True

        # Check pause conditions for background jobs
        paused, why = jobs.should_pause(b.home, b.config)

        # If not paused, check if any paused job (paused by load/lockfile) can resume
        if not paused:
            resumed_job = conn.execute(
                "SELECT id FROM job WHERE state = 'paused' AND pause_reason IN ('load', 'lockfile') "
                "ORDER BY created ASC LIMIT 1"
            ).fetchone()
            if resumed_job:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute("UPDATE job SET state = 'running', pause_reason = NULL WHERE id = ?", (resumed_job["id"],))
                conn.execute("COMMIT")

        # 2. Check for queued or running jobs
        # A queued job of a kind nobody steps is failed below, never left waiting forever.
        job_row = conn.execute(
            "SELECT id, kind, params, state FROM job WHERE state = 'queued' "
            f"OR (state = 'running' AND kind IN ({', '.join('?' * len(STEPPED_KINDS))})) "
            "ORDER BY created ASC LIMIT 1", STEPPED_KINDS
        ).fetchone()

        if job_row:
            job_id = job_row["id"]
            kind = job_row["kind"]
            params = json.loads(job_row["params"]) if isinstance(job_row["params"], str) else (job_row["params"] or {})

            if paused and kind in ("pull", "index", "reembed"):
                reason_code = why
                conn.execute("BEGIN IMMEDIATE")
                conn.execute("UPDATE job SET state = 'paused', pause_reason = ? WHERE id = ?", (reason_code, job_id))
                conn.execute("COMMIT")
                return False

            try:
                conn.execute("BEGIN IMMEDIATE")
                jobs.resume_job(conn, job_id)
                conn.execute("COMMIT")
                if kind == "pull":
                    _execute_pull_job(ctx, conn, job_id, params, pm)
                elif kind == "index":
                    from .indexing import step_index
                    step_index(ctx, conn, job_id, params)
                else:
                    raise ProproomError("not_implemented", f"No worker step for job kind {kind}")
            except Exception as e:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                conn.execute("BEGIN IMMEDIATE")
                jobs.finish_job(conn, job_id, "failed", {"code": getattr(e, "code", "job_failed"), "message": str(e)})
                conn.execute("COMMIT")
                logger.exception("Job %s failed", job_id)
            return True

        # 3. The analysis backlog: heavy (models, ffmpeg), so it yields to the pause rules like pull and index.
        if paused or pm is None:
            return False
        from . import analysis
        return analysis.step(ctx, conn, pm)
    finally:
        conn.close()


def _execute_pull_job(ctx: Any, conn: sqlite3.Connection, job_id: str, params: dict[str, Any], pm: Any) -> None:
    """Process one candidate, persisting the page and offset after every step."""
    import yaml
    b = ctx.booted
    row = conn.execute("SELECT cursor, progress, used_bytes, budget_bytes FROM job WHERE id = ?", (job_id,)).fetchone()
    cursor = json.loads(row["cursor"]) if row["cursor"] else {"job": 0, "page": None, "offset": 0}
    progress = json.loads(row["progress"])
    if row["budget_bytes"] is None:
        raise ProproomError("bad_argument", "Pull job has no byte budget")
    if row["used_bytes"] >= row["budget_bytes"]:
        conn.execute("BEGIN IMMEDIATE")
        jobs.finish_job(conn, job_id, "succeeded")
        conn.execute("COMMIT")
        return
    spec_path = Path(params["spec"])
    if not spec_path.is_absolute():
        spec_path = b.home.nas / spec_path
    content = spec_path.read_text(encoding="utf-8")
    spec = yaml.safe_load(content) if spec_path.suffix in (".yaml", ".yml") else json.loads(content)
    if not isinstance(spec, dict) or not isinstance(spec.get("jobs"), list):
        raise ProproomError("bad_argument", "Invalid pull specification")
    if cursor["job"] >= len(spec["jobs"]):
        conn.execute("BEGIN IMMEDIATE")
        jobs.finish_job(conn, job_id, "succeeded")
        conn.execute("COMMIT")
        return
    pjob = spec["jobs"][cursor["job"]]
    source = pjob["source"]
    plugin = pm.plugins.get(source) if pm else None
    if not plugin or not plugin.instance:
        raise ProproomError("source_unavailable", f"Source {source} is unavailable")
    if plugin.manifest.get("source", {}).get("bulk_allowed") is False:
        raise ProproomError("bulk_not_allowed", f"Source {source} does not permit bulk pulls")
    sctx = pm.get_source_context(source)
    # Persist the source's page once, before processing any candidate.  A restart
    # resumes this exact snapshot even when the remote search order changes.
    if "refs" not in cursor:
        if pjob.get("category") or pjob.get("collection"):
            req = EnumerateRequest(category=pjob.get("category"), collection=pjob.get("collection"),
                                   query=pjob.get("search") or pjob.get("query"), cursor=cursor["page"])
            page = run_sync(plugin.instance.enumerate(sctx, req))
        else:
            req = SearchRequest(query=pjob.get("search") or pjob.get("query") or "", kind=pjob.get("kind"), cursor=cursor["page"])
            page = run_sync(plugin.instance.search(sctx, req))
        if page.next_cursor and page.next_cursor == cursor["page"]:
            raise ProproomError("invalid_cursor", "Source returned the same page cursor")
        cursor["refs"] = [{"source": item.ref.source, "external_id": item.ref.external_id,
                           "revision": item.ref.revision} for item in page.items]
        cursor["next_page"] = page.next_cursor
        conn.execute("BEGIN IMMEDIATE")
        jobs.update_job_progress(conn, job_id, progress, cursor=cursor)
        conn.execute("COMMIT")
    if cursor["offset"] >= len(cursor["refs"]):
        next_page = cursor.pop("next_page")
        cursor.pop("refs")
        if next_page:
            cursor.update(page=next_page, offset=0)
        else:
            cursor.update(job=cursor["job"] + 1, page=None, offset=0)
        conn.execute("BEGIN IMMEDIATE")
        jobs.update_job_progress(conn, job_id, progress, cursor=cursor)
        conn.execute("COMMIT")
        return
    ref = SourceRef(**cursor["refs"][cursor["offset"]])
    cursor["offset"] += 1
    ext_id = ref.external_id
    seen = conn.execute("SELECT decision, policy_version, error_attempts FROM pull_seen WHERE source = ? AND external_id = ?", (source, ext_id)).fetchone()
    final_seen = seen and (
        seen["decision"] == "taken"
        or (seen["decision"] == "rejected" and seen["policy_version"] == b.policy_version)
        or (seen["decision"] == "error" and seen["error_attempts"] >= MAX_PULL_ERRORS)
    )
    decision, reason, downloaded = None, None, 0
    attempts, last_error = 0, None
    if not final_seen:
        try:
            resolved = run_sync(plugin.instance.resolve(sctx, ref))
            policy = conn.execute("SELECT rules FROM policy WHERE version = ?", (b.policy_version,)).fetchone()
            if not policy:
                raise ProproomError("policy_missing", "Pull policy is missing")
            verdict, reasons, _, _, _ = evaluate_source_rights(ctx=ctx, pm=pm, policy_version=b.policy_version,
                policy_rules=json.loads(policy["rules"]), source_name=source, external_id=ext_id, resolved=resolved)
            offer = resolved.offers[0] if resolved.offers else None
            if verdict != "allow":
                decision, reason = "rejected", reasons[0]["code"] if reasons else "policy"
            elif not offer:
                decision, reason = "skipped", "no_offer"
            elif offer.bytes is not None and row["used_bytes"] + offer.bytes > row["budget_bytes"]:
                decision, reason = "deferred", "budget"
            else:
                result = fetch(ctx, {"source": source, "external_id": ext_id, "revision": ref.revision})
                blob = conn.execute("SELECT bytes FROM blob WHERE sha256 = ?", (result["sha256"],)).fetchone()
                if not blob:
                    raise ProproomError("blob_missing", "Fetched blob was not recorded")
                downloaded = blob["bytes"]
                decision, reason = "taken", "ok"
        except Exception as e:
            logger.warning("Pull candidate %s:%s failed: %s", source, ext_id, e)
            attempts = (seen["error_attempts"] if seen and seen["decision"] == "error" else 0) + 1
            last_error = str(e)[:1000]
            decision, reason = "error", "too_many_errors" if attempts >= MAX_PULL_ERRORS else last_error[:100]
    conn.execute("BEGIN IMMEDIATE")
    if decision:
        conn.execute("INSERT INTO pull_seen(source, external_id, decision, reason, policy_version, job_id, evaluated_at, error_attempts, last_error) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(source, external_id) DO UPDATE SET decision=excluded.decision, reason=excluded.reason, policy_version=excluded.policy_version, job_id=excluded.job_id, evaluated_at=excluded.evaluated_at, error_attempts=excluded.error_attempts, last_error=excluded.last_error",
                     (source, ext_id, decision, reason, b.policy_version, job_id, utcnow(), attempts, last_error))
        progress[decision] = progress.get(decision, 0) + 1
    jobs.update_job_progress(conn, job_id, progress, used_bytes=downloaded, cursor=cursor)
    conn.execute("COMMIT")
