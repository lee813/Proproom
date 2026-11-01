"""Evaluation runner: recall@10 and nDCG@10 comparing FTS, vector, and hybrid search.

Handover §14.
"""
from __future__ import annotations

import math
from pathlib import Path
import sqlite3
import struct
from typing import Any
import yaml

from . import db, search
from ..sdk.errors import ProproomError


def dcg_at_k(relevance_scores: list[float], k: int = 10) -> float:
    """Compute Discounted Cumulative Gain at rank k."""
    dcg = 0.0
    for i, rel in enumerate(relevance_scores[:k], start=1):
        dcg += (2.0 ** rel - 1.0) / math.log2(i + 1)
    return dcg


def ndcg_at_k(ranked_ids: list[str], ground_truth: dict[str, float] | set[str] | list[str], k: int = 10) -> float:
    """Compute Normalized Discounted Cumulative Gain at rank k."""
    if not ground_truth:
        raise ValueError("Ground truth is empty")
    if isinstance(ground_truth, (set, list)):
        gt_dict = {str(aid): 1.0 for aid in ground_truth}
    else:
        gt_dict = {str(k_id): float(score) for k_id, score in ground_truth.items()}

    # An asset found again (another segment of the same clip) earns nothing the second time.
    actual_scores = [gt_dict.get(aid, 0.0) for aid in list(dict.fromkeys(ranked_ids))[:k]]
    actual_dcg = dcg_at_k(actual_scores, k=k)

    ideal_scores = sorted(gt_dict.values(), reverse=True)[:k]
    ideal_dcg = dcg_at_k(ideal_scores, k=k)

    if ideal_dcg <= 0.0:
        return 1.0
    return actual_dcg / ideal_dcg


def recall_at_k(ranked_ids: list[str], ground_truth: dict[str, float] | set[str] | list[str], k: int = 10) -> float:
    """Compute Recall at rank k."""
    gt_set = set(ground_truth.keys()) if isinstance(ground_truth, dict) else set(ground_truth)
    if not gt_set:
        raise ValueError("Ground truth is empty")
    retrieved_relevant = set(list(dict.fromkeys(ranked_ids))[:k]) & gt_set
    return len(retrieved_relevant) / len(gt_set)


def run_evaluation(ctx: Any, args: dict[str, Any]) -> dict[str, Any]:
    """Run evaluation over queries in yaml file."""
    b = ctx.booted
    spec_path_str = args.get("spec", "eval/queries.yaml")
    spec_p = Path(spec_path_str)
    if not spec_p.is_absolute():
        if (b.home.root / spec_path_str).is_file():
            spec_p = b.home.root / spec_path_str
        elif (b.home.nas / spec_path_str).is_file():
            spec_p = b.home.nas / spec_path_str
        else:
            # Check current working directory
            cur_p = Path.cwd() / spec_path_str
            if cur_p.is_file():
                spec_p = cur_p
            else:
                raise ProproomError("spec_not_found", f"Queries file not found: {spec_path_str}")

    data = yaml.safe_load(spec_p.read_text(encoding="utf-8")) or {}
    queries = data.get("queries", [])
    if not queries:
        return {"queries_count": 0, "modes": {}}

    pm = ctx.pm()
    conn = db.connect(b.home.db)
    try:
        # Pre-resolve any slugs to asset ids in ground truth
        def _resolve_gt(rel_list: list[Any] | dict[str, Any]) -> dict[str, float]:
            res: dict[str, float] = {}
            if isinstance(rel_list, dict):
                items = rel_list.items()
            else:
                items = [(x, 1.0) for x in rel_list]

            for key, score in items:
                row = conn.execute("SELECT id FROM asset WHERE id = ? OR slug = ?", (str(key), str(key))).fetchone()
                if row:
                    res[row["id"]] = float(score)
                else:
                    res[str(key)] = float(score)
            return res

        mode_metrics: dict[str, dict[str, list[float]]] = {
            "fts": {"recall@10": [], "ndcg@10": []},
            "vector": {"recall@10": [], "ndcg@10": []},
            "hybrid": {"recall@10": [], "ndcg@10": []},
        }

        details = []
        skipped = 0

        for q_item in queries:
            query_text = q_item.get("q", "")
            kind = q_item.get("kind")
            anchor = q_item.get("anchor")
            raw_rel = q_item.get("relevant", [])
            if not raw_rel:
                skipped += 1
                continue
            gt = _resolve_gt(raw_rel)

            # 1. Hybrid search
            res_hyb = search.execute_search(
                conn, b.home, query_text, kind=kind, anchor=anchor, k=10,
                plugin_manager=pm,
            )
            ids_hyb = [it["asset_id"] for it in res_hyb.get("items", [])]
            r_hyb = recall_at_k(ids_hyb, gt, k=10)
            n_hyb = ndcg_at_k(ids_hyb, gt, k=10)
            mode_metrics["hybrid"]["recall@10"].append(r_hyb)
            mode_metrics["hybrid"]["ndcg@10"].append(n_hyb)

            # 2. FTS only (disable vector by forcing qvec=None and no plugin_manager)
            res_fts = search.execute_search(
                conn, b.home, query_text, kind=kind, anchor=anchor, k=10,
                qvec=None, plugin_manager=None,
            )
            ids_fts = [it["asset_id"] for it in res_fts.get("items", [])]
            r_fts = recall_at_k(ids_fts, gt, k=10)
            n_fts = ndcg_at_k(ids_fts, gt, k=10)
            mode_metrics["fts"]["recall@10"].append(r_fts)
            mode_metrics["fts"]["ndcg@10"].append(n_fts)

            # 3. Vector only: retrieve items with vector rank
            res_vec = search.execute_search(conn, b.home, query_text, kind=kind, anchor=anchor, k=10,
                                             plugin_manager=pm, mode="vector")
            ids_vec = [it["asset_id"] for it in res_vec.get("items", [])]
            r_vec = recall_at_k(ids_vec, gt, k=10)
            n_vec = ndcg_at_k(ids_vec, gt, k=10)
            mode_metrics["vector"]["recall@10"].append(r_vec)
            mode_metrics["vector"]["ndcg@10"].append(n_vec)

            details.append({
                "query": query_text,
                "ground_truth_count": len(gt),
                "fts": {"recall@10": round(r_fts, 4), "ndcg@10": round(n_fts, 4)},
                "vector": {"recall@10": round(r_vec, 4), "ndcg@10": round(n_vec, 4)},
                "hybrid": {"recall@10": round(r_hyb, 4), "ndcg@10": round(n_hyb, 4)},
            })

        summary: dict[str, dict[str, float]] = {}
        for m, m_data in mode_metrics.items():
            summary[m] = {
                "recall@10": round(sum(m_data["recall@10"]) / len(m_data["recall@10"]), 4) if m_data["recall@10"] else None,
                "ndcg@10": round(sum(m_data["ndcg@10"]) / len(m_data["ndcg@10"]), 4) if m_data["ndcg@10"] else None,
            }

        return {
            "queries_count": len(queries),
            "skipped_count": skipped,
            "evaluated_count": len(details),
            "modes": summary,
            "details": details,
        }
    finally:
        conn.close()
