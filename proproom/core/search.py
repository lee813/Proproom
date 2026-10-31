"""Hybrid local search (vec0 KNN + fts_latin + fts_cjk, RRF fusion).

Handover §7, §8, Appendix B.2.
"""
from __future__ import annotations

import base64
import json
import sqlite3
from typing import Any

from . import cjk, db, dedupe, sheet, store
from .ids import ulid, utcnow
from .paths import Home


def execute_search(
    conn: sqlite3.Connection,
    home: Home,
    query: str,
    *,
    kind: list[str] | None = None,
    license_filter: str | None = None,
    anchor: str | None = None,
    entity: str | None = None,
    fits: str | None = None,
    min_edge: int | None = None,
    exclude_used_in: str | None = None,
    include_quotation: bool = False,
    sheet_requested: bool = False,
    explain: bool = False,
    k: int = 12,
    cursor: str | None = None,
    actor: str = "cli",
    qvec: bytes | None = None,
) -> dict[str, Any]:
    """Execute hybrid search, persist selection and selection_items, optionally render sheet."""
    offset = 0
    if cursor:
        try:
            offset = int(base64.b64decode(cursor.encode()).decode())
        except Exception:
            offset = 0

    # 1. Resolve entity filter if provided
    matched_entity_assets: set[str] | None = None
    if entity:
        # Search entity table by labels/aliases/qid
        rows = conn.execute(
            "SELECT qid FROM entity WHERE qid = ? OR labels LIKE ? OR aliases LIKE ?",
            (entity, f'%"{entity}"%', f'%"{entity}"%'),
        ).fetchall()
        qids = [r["qid"] for r in rows]
        if qids:
            placeholders = ",".join("?" for _ in qids)
            a_rows = conn.execute(
                f"SELECT asset_id FROM asset_entity WHERE qid IN ({placeholders})", qids
            ).fetchall()
            matched_entity_assets = {r["asset_id"] for r in a_rows}
        else:
            matched_entity_assets = set()

    # 2. CJK query rewrite
    cjk_q = cjk.rewrite(query)

    # 3. FTS latin query preparation
    # Clean query of special FTS characters for safe MATCH
    latin_terms = [t.strip("\"'") for t in query.split() if not cjk.has_cjk(t) and t.strip("\"'")]
    fts_latin_query = " ".join(f'"{t}"*' for t in latin_terms) if latin_terms else None

    # 4. Search channels: FTS latin, FTS CJK, CJK LIKE fallback, Vector KNN
    # We collect candidate ranks: {asset_id: {"fts": rank, "cjk": rank, "vec": rank}}
    channel_ranks: dict[str, dict[str, int]] = {}

    limit_recall = max(50, (offset + k) * 3)

    # Channel A: fts_latin
    if fts_latin_query:
        try:
            cur = conn.execute(
                "SELECT a.id AS asset_id, row_number() OVER (ORDER BY bm25(fts_latin)) AS r "
                "FROM fts_latin JOIN asset a ON a.rid = fts_latin.rowid "
                "WHERE fts_latin MATCH ? LIMIT ?",
                (fts_latin_query, limit_recall),
            )
            for r in cur:
                aid = r["asset_id"]
                channel_ranks.setdefault(aid, {})["fts"] = r["r"]
        except sqlite3.OperationalError:
            pass

    # Fallback LIKE for latin terms if FTS missed
    if latin_terms:
        for term in latin_terms:
            cur = conn.execute(
                "SELECT a.id AS asset_id FROM search_doc s JOIN asset a ON a.rid = s.rid "
                "WHERE s.title LIKE ? OR s.description LIKE ? OR s.artist LIKE ? OR s.tags LIKE ? LIMIT ?",
                (f"%{term}%", f"%{term}%", f"%{term}%", f"%{term}%", limit_recall),
            )
            for i, r in enumerate(cur, start=1):
                aid = r["asset_id"]
                if "fts" not in channel_ranks.setdefault(aid, {}):
                    channel_ranks[aid]["fts"] = i

    # Channel B: fts_cjk (trigram)
    if cjk_q.match:
        try:
            cur = conn.execute(
                "SELECT a.id AS asset_id, row_number() OVER (ORDER BY bm25(fts_cjk)) AS r "
                "FROM fts_cjk JOIN asset a ON a.rid = fts_cjk.rowid "
                "WHERE fts_cjk MATCH ? LIMIT ?",
                (cjk_q.match, limit_recall),
            )
            for r in cur:
                aid = r["asset_id"]
                channel_ranks.setdefault(aid, {})["cjk"] = r["r"]
        except sqlite3.OperationalError:
            pass

    # Channel C: CJK LIKE fallback for short 1-2 char queries
    if cjk_q.like:
        like_terms = cjk_q.like
        clause = " AND ".join("s.cjk LIKE ?" for _ in like_terms)
        params = [f"%{term}%" for term in like_terms]
        cur = conn.execute(
            f"SELECT a.id AS asset_id FROM search_doc s JOIN asset a ON a.rid = s.rid WHERE {clause} LIMIT ?",
            params + [limit_recall],
        )
        for i, r in enumerate(cur, start=1):
            aid = r["asset_id"]
            if "cjk" not in channel_ranks.setdefault(aid, {}):
                channel_ranks[aid]["cjk"] = i

    # Channel D: Vector KNN if qvec is provided and vec_s1 exists
    if qvec:
        try:
            cur = conn.execute(
                "SELECT t.asset_id, min(v.r) AS r FROM ("
                "  SELECT rid, row_number() OVER (ORDER BY distance) AS r FROM vec_s1 "
                "  WHERE embedding MATCH ? AND k = ? AND stale = 0"
                ") v JOIN vec_target t ON t.rid = v.rid GROUP BY t.asset_id",
                (qvec, limit_recall),
            )
            for r in cur:
                aid = r["asset_id"]
                channel_ranks.setdefault(aid, {})["vec"] = r["r"]
        except sqlite3.OperationalError:
            pass

    # 5. RRF Fusion: sum(1.0 / (60 + r))
    rrf_scores: list[tuple[str, float, dict[str, int]]] = []
    for aid, ranks in channel_ranks.items():
        score = sum(1.0 / (60.0 + r) for r in ranks.values())
        rrf_scores.append((aid, score, ranks))

    # Sort descending by RRF score
    rrf_scores.sort(key=lambda x: x[1], reverse=True)

    # 6. Apply post-retrieval filters and diversity
    surviving_items = []
    seen_assets = set()

    for aid, score, ranks in rrf_scores:
        if matched_entity_assets is not None and aid not in matched_entity_assets:
            continue

        # Asset metadata & rights
        asset_row = conn.execute(
            "SELECT a.*, ar.decision, ar.basis, ar.obligations "
            "FROM asset a LEFT JOIN asset_rights ar ON ar.asset_id = a.id "
            "WHERE a.id = ? AND a.status = 'active'",
            (aid,),
        ).fetchone()

        if not asset_row:
            continue

        # Rights filter
        decision = asset_row["decision"] or "deny"
        basis = asset_row["basis"]
        if decision != "allow":
            continue
        if basis == "quotation" and not include_quotation:
            continue

        # Kind filter
        if kind and asset_row["kind"] not in kind:
            continue

        # Exclude used in project filter
        if exclude_used_in:
            used = conn.execute(
                "SELECT 1 FROM usage u JOIN project p ON p.id = u.project_id "
                "WHERE u.asset_id = ? AND (p.id = ? OR p.path LIKE ?) AND u.removed_at IS NULL LIMIT 1",
                (aid, exclude_used_in, f"%{exclude_used_in}%"),
            ).fetchone()
            if used:
                continue

        # Representations and min_edge check
        reps = conn.execute(
            "SELECT * FROM representation WHERE asset_id = ? ORDER BY role = 'original' DESC",
            (aid,),
        ).fetchall()
        if not reps:
            continue

        chosen_rep = None
        for rep in reps:
            w = rep["width"] or 0
            h = rep["height"] or 0
            edge = min(w, h)
            if min_edge and edge < min_edge and rep["role"] == "original":
                continue
            chosen_rep = rep
            break

        if not chosen_rep:
            continue

        # Fetch provenance info
        prov = conn.execute(
            "SELECT * FROM provenance WHERE asset_id = ? ORDER BY retrieved DESC LIMIT 1",
            (aid,),
        ).fetchone()

        # Fetch rights evidence
        ev = None
        if prov:
            ev = conn.execute(
                "SELECT * FROM rights_evidence WHERE provenance_id = ? ORDER BY created DESC LIMIT 1",
                (prov["id"],),
            ).fetchone()

        # Check flags
        flags = [f["kind"] for f in conn.execute(
            "SELECT kind FROM flag WHERE target_id = ? AND resolution IS NULL", (aid,)
        ).fetchall()]

        # Check recent usage projects
        used_projs = [u["name"] for u in conn.execute(
            "SELECT p.name FROM usage u JOIN project p ON p.id = u.project_id "
            "WHERE u.asset_id = ? AND u.removed_at IS NULL GROUP BY p.id ORDER BY max(u.created) DESC LIMIT 2",
            (aid,),
        ).fetchall()]

        surviving_items.append({
            "asset_id": aid,
            "representation_id": chosen_rep["id"],
            "segment_id": None,
            "slug": asset_row["slug"],
            "title": asset_row["title"],
            "kind": asset_row["kind"],
            "width": chosen_rep["width"],
            "height": chosen_rep["height"],
            "duration": chosen_rep["duration"],
            "blob_sha256": chosen_rep["blob_sha256"],
            "basis": basis,
            "license_id": ev["license_id"] if ev else None,
            "attribution": ev["attribution"] if ev else None,
            "source": prov["source"] if prov else "local",
            "source_url": prov["source_url"] if prov else None,
            "score": round(score, 5),
            "ranks": ranks,
            "flags": flags,
            "used_in": used_projs,
        })

    # Paginate
    paged_items = surviving_items[offset : offset + k]
    next_cursor = base64.b64encode(str(offset + k).encode()).decode() if (offset + k) < len(surviving_items) else None

    # 7. Create Selection and Selection Items
    sel_id = ulid()
    now = utcnow()
    query_json = json.dumps({
        "query": query, "kind": kind, "license": license_filter, "anchor": anchor,
        "entity": entity, "min_edge": min_edge, "exclude_used_in": exclude_used_in,
        "include_quotation": include_quotation,
    }, sort_keys=True)

    sheet_sha256 = None
    sheet_relpath = None

    # 8. Render sheet if requested
    if sheet_requested and paged_items:
        cells = []
        for idx, item in enumerate(paged_items, start=1):
            blob_path = store.blob_abspath(home, item["blob_sha256"])
            cells.append(sheet.SheetCell(
                idx=idx,
                image_path=blob_path if blob_path.is_file() else None,
                kind=item["kind"],
                width=item["width"],
                height=item["height"],
                duration=item["duration"],
                license_id=item["license_id"],
                flags=item["flags"],
                used_in=item["used_in"],
                title=item["title"],
            ))
        tmp_sheet = home.dir("tmp") / f"sheet_{sel_id}.jpg"
        sheet.render_contact_sheet(cells, tmp_sheet)
        sheet_sha, sheet_size, sheet_relpath = store.put_file(home, tmp_sheet, mime="image/jpeg", move=True)
        dedupe.register_blob(conn, sheet_sha, sheet_size, "image/jpeg", stored=True, storage_path=sheet_relpath)
        sheet_sha256 = sheet_sha

    conn.execute(
        "INSERT INTO selection(id, created, created_by, query, sheet_sha256) VALUES (?, ?, ?, ?, ?)",
        (sel_id, now, actor, query_json, sheet_sha256),
    )

    for idx, item in enumerate(paged_items, start=1):
        expl_json = json.dumps({
            "score_type": "rrf",
            "score": item["score"],
            "ranks": item["ranks"],
        })
        conn.execute(
            "INSERT INTO selection_item(selection_id, idx, asset_id, representation_id, segment_id, explain) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (sel_id, idx, item["asset_id"], item["representation_id"], item["segment_id"], expl_json),
        )
        item["idx"] = idx

    return {
        "selection_id": sel_id,
        "items": paged_items,
        "score_type": "rrf",
        "next_cursor": next_cursor,
        "sheet": sheet_relpath,
    }
