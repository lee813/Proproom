"""Hybrid local search (vec0 KNN + fts_latin + fts_cjk, RRF fusion).

Handover §7, §8, Appendix B.2.
"""
from __future__ import annotations

import base64
import json
import logging
import sqlite3
import struct
from typing import Any

from . import cjk, db, dedupe, embedding, sheet, store
from .ids import ulid, utcnow
from .paths import Home
from ..sdk.errors import ProproomError

log = logging.getLogger("proproom.search")


def _starts_a_word(text: str, term: str) -> bool:
    """`term` occurs at the start of a word or a camel-case part: "train" in "SkyTrain", not "server" in "Observer"."""
    low, t = text.lower(), term.lower()
    i = low.find(t)
    while i >= 0:
        if i == 0 or not text[i - 1].isalpha() or (text[i - 1].islower() and text[i].isupper()):
            return True
        i = low.find(t, i + 1)
    return False


def search(ctx: Any, args: dict[str, Any]) -> dict[str, Any]:
    """Execute hybrid search pipeline."""
    b = ctx.booted
    pm = ctx.pm()
    conn = db.connect(b.home.db)
    try:
        # Before the write transaction: the first query in a process loads the model, which takes seconds.
        qvec = query_vector(conn, b.home, pm, args["query"])
        return execute_search(
            conn=conn,
            home=b.home,
            query=args["query"],
            kind=args.get("kind"),
            license_filter=args.get("license"),
            anchor=args.get("anchor"),
            entity=args.get("entity"),
            fits=args.get("fits"),
            min_edge=args.get("min_edge"),
            exclude_used_in=args.get("exclude_used_in"),
            include_quotation=args.get("include_quotation", False),
            sheet_requested=args.get("sheet", False),
            explain=args.get("explain", False),
            k=args.get("k", 12),
            cursor=args.get("cursor"),
            actor=ctx.actor,
            qvec=qvec,
        )
    finally:
        conn.close()


def query_vector(conn: sqlite3.Connection, home: Home, pm: Any, query: str) -> bytes | None:
    """The query's vector in the active space, or None when there is no embedder or space yet (keyword search only).
    A lost GPU is raised, not hidden behind keyword results."""
    if pm is None or not conn.execute("SELECT 1 FROM embedding_space WHERE active = 1").fetchone():
        return None
    try:
        plugin = embedding.find(pm)
    except ProproomError as e:
        if e.code == "no_embedder":
            return None
        raise
    try:
        batch = embedding.embed(pm, plugin, embedding.context(home, plugin, log), [{"text": query, "prompt": "query"}])
    except ProproomError as e:
        if e.code == "gpu_lost":
            raise
        log.warning("Embed query failed: %s", e)
        return None
    except Exception as e:  # noqa: BLE001 - search degrades to keyword recall; the warning names the cause
        log.warning("Embed query failed: %s", e)
        return None
    if not batch.vectors:
        return None
    v = batch.vectors[0]
    return struct.pack(f"{len(v)}f", *v)


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
    plugin_manager: Any = None,
    mode: str = "hybrid",
) -> dict[str, Any]:
    """Execute hybrid search, persist selection and selection_items, optionally render sheet."""
    offset = 0
    if cursor:
        try:
            offset = int(base64.b64decode(cursor.encode()).decode())
        except Exception:
            offset = 0

    # 1. Embed query if embedder available and space active
    if qvec is None and plugin_manager is not None:
        qvec = query_vector(conn, home, plugin_manager, query)

    # 2. Resolve entity filter if provided
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

    # Build shared recall filters pushed directly into candidate queries (§7)
    visibility = "(ar.decision = 'allow' OR (ar.decision = 'review' AND ar.basis = 'quotation'))" if include_quotation else "ar.decision = 'allow'"
    filter_clauses = ["a.status = 'active'", visibility]
    filter_params: list[Any] = []

    if not include_quotation:
        filter_clauses.append("ar.basis != 'quotation'")

    if kind:
        placeholders = ",".join("?" for _ in kind)
        filter_clauses.append(f"a.kind IN ({placeholders})")
        filter_params.extend(kind)

    if anchor and anchor.lower() in ("person", "event", "data"):
        filter_clauses.append("a.synthetic = 0")

    if license_filter:
        filter_clauses.append(
            "EXISTS (SELECT 1 FROM provenance prov JOIN rights_evidence re ON re.provenance_id = prov.id "
            "WHERE prov.asset_id = a.id AND (lower(re.license_id) LIKE ? OR lower(re.basis) LIKE ?))"
        )
        filter_params.extend([f"%{license_filter.lower()}%", f"%{license_filter.lower()}%"])

    if min_edge:
        filter_clauses.append(
            "EXISTS (SELECT 1 FROM representation rep WHERE rep.asset_id = a.id AND rep.role NOT LIKE 'thumb%' "
            "AND min(coalesce(rep.width, 0), coalesce(rep.height, 0)) >= ?)"
        )
        filter_params.append(min_edge)

    if exclude_used_in:
        clean_proj = exclude_used_in.strip("/")
        filter_clauses.append(
            "NOT EXISTS (SELECT 1 FROM usage u JOIN project p ON p.id = u.project_id "
            "WHERE u.asset_id = a.id AND u.removed_at IS NULL AND (p.path = ? OR p.path = ? OR p.name = ?))"
        )
        filter_params.extend([clean_proj, f"project/{clean_proj}", clean_proj])

    if matched_entity_assets is not None:
        if not matched_entity_assets:
            filter_clauses.append("0 = 1")
        else:
            placeholders = ",".join("?" for _ in matched_entity_assets)
            filter_clauses.append(f"a.id IN ({placeholders})")
            filter_params.extend(list(matched_entity_assets))

    common_where = " AND ".join(filter_clauses)

    # 4. Search channels: FTS latin, FTS CJK, CJK LIKE fallback, Vector KNN
    # We collect candidate ranks: {asset_id: {"fts": rank, "cjk": rank, "vec": rank}}
    channel_ranks: dict[str, dict[str, int]] = {}
    best_segment: dict[str, str | None] = {}
    vec_distance: dict[str, float] = {}  # cosine distance of the asset's closest vector, for judging relevance

    limit_recall = max(50, (offset + k) * 3)

    # Channel A: fts_latin
    if mode != "vector" and fts_latin_query:
        try:
            sql = (
                "SELECT a.id AS asset_id, row_number() OVER (ORDER BY bm25(fts_latin)) AS r "
                "FROM fts_latin "
                "JOIN asset a ON a.rid = fts_latin.rowid "
                "LEFT JOIN asset_rights ar ON ar.asset_id = a.id "
                f"WHERE fts_latin MATCH ? AND {common_where} LIMIT ?"
            )
            cur = conn.execute(sql, [fts_latin_query] + filter_params + [limit_recall])
            for r in cur:
                aid = r["asset_id"]
                channel_ranks.setdefault(aid, {})["fts"] = r["r"]
        except sqlite3.OperationalError:
            pass

    # Fallback for what the FTS tokenizer misses, such as "train" in "SkyTrain". Like FTS, every term must match,
    # each at the start of a word or a camel-case part (not "server" inside "Observer"), and these hits rank after
    # the FTS hits.
    if mode != "vector" and latin_terms:
        fts_hits = sum(1 for ranks in channel_ranks.values() if "fts" in ranks)
        clause = " AND ".join("(s.title || ' ' || s.description || ' ' || s.artist || ' ' || s.tags) LIKE ?"
                              for _ in latin_terms)
        sql = (
            "SELECT a.id AS asset_id, s.title || ' ' || s.description || ' ' || s.artist || ' ' || s.tags AS text "
            "FROM search_doc s "
            "JOIN asset a ON a.rid = s.rid "
            "LEFT JOIN asset_rights ar ON ar.asset_id = a.id "
            f"WHERE {clause} AND {common_where} LIMIT ?"
        )
        cur = conn.execute(sql, [f"%{t}%" for t in latin_terms] + filter_params + [limit_recall])
        n = fts_hits
        for r in cur:
            aid = r["asset_id"]
            if "fts" in channel_ranks.get(aid, {}) or not all(_starts_a_word(r["text"], t) for t in latin_terms):
                continue
            n += 1
            channel_ranks.setdefault(aid, {})["fts"] = n

    # Channel B: fts_cjk (trigram)
    if mode != "vector" and cjk_q.match:
        try:
            sql = (
                "SELECT a.id AS asset_id, row_number() OVER (ORDER BY bm25(fts_cjk)) AS r "
                "FROM fts_cjk "
                "JOIN asset a ON a.rid = fts_cjk.rowid "
                "LEFT JOIN asset_rights ar ON ar.asset_id = a.id "
                f"WHERE fts_cjk MATCH ? AND {common_where} LIMIT ?"
            )
            cur = conn.execute(sql, [cjk_q.match] + filter_params + [limit_recall])
            for r in cur:
                aid = r["asset_id"]
                channel_ranks.setdefault(aid, {})["cjk"] = r["r"]
        except sqlite3.OperationalError:
            pass

    # Channel C: CJK LIKE fallback for short 1-2 char queries
    if mode != "vector" and cjk_q.like:
        like_terms = cjk_q.like
        clause = " AND ".join("s.cjk LIKE ?" for _ in like_terms)
        params = [f"%{term}%" for term in like_terms]
        sql = (
            "SELECT a.id AS asset_id FROM search_doc s "
            "JOIN asset a ON a.rid = s.rid "
            "LEFT JOIN asset_rights ar ON ar.asset_id = a.id "
            f"WHERE {clause} AND {common_where} LIMIT ?"
        )
        cur = conn.execute(sql, params + filter_params + [limit_recall])
        for i, r in enumerate(cur, start=1):
            aid = r["asset_id"]
            if "cjk" not in channel_ranks.setdefault(aid, {}):
                channel_ranks[aid]["cjk"] = i

    # Channel D: Vector KNN if qvec is provided and active vec table exists
    if qvec:
        active_vec_table = "vec_s1"
        space_row = conn.execute("SELECT vec_table FROM embedding_space WHERE active = 1").fetchone()
        if space_row:
            active_vec_table = space_row["vec_table"]

        vec_where_clauses = ["embedding MATCH ?", "k = ?", "stale = 0"]
        vec_where_params: list[Any] = [qvec, limit_recall]

        if not include_quotation:
            vec_where_clauses.append("rights = 'allow'")
        if kind:
            placeholders = ",".join("?" for _ in kind)
            vec_where_clauses.append(f"kind IN ({placeholders})")
            vec_where_params.extend(kind)
        if anchor and anchor.lower() in ("person", "event", "data"):
            vec_where_clauses.append("synthetic = 0")
        if min_edge:
            vec_where_clauses.append("min_edge >= ?")
            vec_where_params.append(min_edge)

        vec_where_sql = " AND ".join(vec_where_clauses)
        try:
            sql = (
                # SQLite returns the bare segment_id from the row holding min(v.r): the best-matching segment.
                "SELECT t.asset_id, t.segment_id, min(v.r) AS r, min(v.distance) AS d FROM ("
                f"  SELECT rid, distance, row_number() OVER (ORDER BY distance) AS r FROM {active_vec_table} "
                f"  WHERE {vec_where_sql}"
                ") v JOIN vec_target t ON t.rid = v.rid "
                "JOIN asset a ON a.id = t.asset_id "
                "LEFT JOIN asset_rights ar ON ar.asset_id = a.id "
                f"WHERE {common_where} GROUP BY t.asset_id"
            )
            cur = conn.execute(sql, vec_where_params + filter_params)
            for r in cur:
                aid = r["asset_id"]
                channel_ranks.setdefault(aid, {})["vec"] = r["r"]
                best_segment[aid] = r["segment_id"]
                vec_distance[aid] = r["d"]
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
        if aid in seen_assets:
            continue
        seen_assets.add(aid)

        asset_row = conn.execute(
            "SELECT a.*, ar.decision, ar.basis, ar.obligations "
            "FROM asset a LEFT JOIN asset_rights ar ON ar.asset_id = a.id "
            "WHERE a.id = ? AND a.status = 'active'",
            (aid,),
        ).fetchone()

        if not asset_row:
            continue

        decision = asset_row["decision"] or "deny"
        basis = asset_row["basis"]
        if decision != "allow" and not (include_quotation and decision == "review" and basis == "quotation"):
            continue

        # Choose appropriate representation
        reps = conn.execute(
            "SELECT * FROM representation WHERE asset_id = ? ORDER BY role = 'original' DESC",
            (aid,),
        ).fetchall()
        if not reps:
            continue

        chosen_rep = None
        for rep in reps:
            if rep["role"].startswith("thumb"):
                continue
            w = rep["width"] or 0
            h = rep["height"] or 0
            if min_edge and min(w, h) < min_edge:
                continue
            chosen_rep = rep
            break

        if not chosen_rep:
            if min_edge:
                continue
            chosen_rep = reps[0]

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

        # Check segments
        segments = conn.execute(
            "SELECT * FROM segment WHERE representation_id = ? ORDER BY t0",
            (chosen_rep["id"],),
        ).fetchall()

        if segments:
            # One item per asset: the segment the query matched best, else the first.
            seg = next((x for x in segments if x["id"] == best_segment.get(aid)), segments[0])
            surviving_items.append({
                "asset_id": aid,
                "representation_id": chosen_rep["id"],
                "segment_id": seg["id"],
                "slug": asset_row["slug"],
                "title": asset_row["title"],
                "kind": asset_row["kind"],
                "width": chosen_rep["width"],
                "height": chosen_rep["height"],
                "duration": (seg["t1"] - seg["t0"]) if (seg["t1"] is not None and seg["t0"] is not None) else chosen_rep["duration"],
                "blob_sha256": chosen_rep["blob_sha256"],
                "basis": basis,
                "decision": decision,
                "obligations": json.loads(asset_row["obligations"] or "[]"),
                "license_id": ev["license_id"] if ev else None,
                "attribution": ev["attribution"] if ev else None,
                "score": score,
                "ranks": ranks,
                "vec_distance": vec_distance.get(aid),
                "flags": flags,
                "used_in": used_projs,
            })
        else:
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
                "decision": decision,
                "obligations": json.loads(asset_row["obligations"] or "[]"),
                "license_id": ev["license_id"] if ev else None,
                "attribution": ev["attribution"] if ev else None,
                "score": score,
                "ranks": ranks,
                "vec_distance": vec_distance.get(aid),
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
        sheet_sha256 = sheet_sha

    # 9. Persist the selection. Rendering above (thumbnails, video frames) ran without the write lock; this
    # transaction only holds it for the inserts.
    conn.execute("BEGIN IMMEDIATE")
    try:
        if sheet_sha256:
            dedupe.register_blob(conn, sheet_sha256, sheet_size, "image/jpeg", stored=True, storage_path=sheet_relpath)
        conn.execute(
            "INSERT INTO selection(id, created, created_by, query, sheet_sha256) VALUES (?, ?, ?, ?, ?)",
            (sel_id, now, actor, query_json, sheet_sha256),
        )
        for idx, item in enumerate(paged_items, start=1):
            expl_json = json.dumps({
                "score_type": "rrf",
                "score": item["score"],
                "ranks": item["ranks"],
                "vec_distance": item["vec_distance"],
            })
            conn.execute(
                "INSERT INTO selection_item(selection_id, idx, asset_id, representation_id, segment_id, explain) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (sel_id, idx, item["asset_id"], item["representation_id"], item["segment_id"], expl_json),
            )
            item["idx"] = idx
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise

    return {
        "selection_id": sel_id,
        "items": paged_items,
        "score_type": "rrf",
        "next_cursor": next_cursor,
        "sheet": sheet_relpath,
    }
