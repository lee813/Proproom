"""Tests for Task 6: Evaluation runner (recall@10 and nDCG@10)."""
from __future__ import annotations

import pytest
import yaml

from proproom.api.registry import REGISTRY
from proproom.core.eval import dcg_at_k, ndcg_at_k, recall_at_k
from support import ingest


def test_metric_calculations():
    # Perfect retrieval
    gt = {"a": 1.0, "b": 1.0}
    assert recall_at_k(["a", "b", "c"], gt, k=10) == 1.0
    assert ndcg_at_k(["a", "b", "c"], gt, k=10) == 1.0

    # Partial retrieval
    assert recall_at_k(["a", "x", "y"], gt, k=10) == 0.5
    # Suboptimal rank order with graded relevance
    graded_gt = {"best": 2.0, "good": 1.0}
    # Ranking best first gives 1.0
    assert ndcg_at_k(["best", "good"], graded_gt, k=10) == 1.0
    # Ranking good first gives < 1.0
    assert ndcg_at_k(["good", "best"], graded_gt, k=10) < 1.0


def test_eval_operation_runner(ctx, conn, home, tmp_path):
    a1 = ingest(conn, home, slug="tocqueville", title="Alexis de Tocqueville portrait", data=b"portrait tocqueville")
    a2 = ingest(conn, home, slug="parliament", title="Parliament chamber", data=b"parliament debate")

    eval_yaml = tmp_path / "test_queries.yaml"
    eval_yaml.write_text(yaml.dump({
        "queries": [
            {"q": "Tocqueville", "relevant": ["tocqueville"]},
            {"q": "Parliament", "relevant": [a2.asset_id]},
        ]
    }))

    res = REGISTRY["eval"](ctx, {"spec": str(eval_yaml)})
    assert res["queries_count"] == 2
    assert "fts" in res["modes"]
    assert "hybrid" in res["modes"]
    assert "vector" in res["modes"]

    assert res["modes"]["fts"]["recall@10"] == 1.0
    assert res["modes"]["fts"]["ndcg@10"] == 1.0
    assert len(res["details"]) == 2


def test_an_asset_found_twice_counts_once():
    from proproom.core.eval import ndcg_at_k, recall_at_k
    ranked = ["clip", "clip", "clip", "other"]  # three segments of one clip
    assert ndcg_at_k(ranked, {"clip"}) == 1.0
    assert ndcg_at_k(ranked, {"clip", "other"}) <= 1.0
    assert recall_at_k(ranked, {"clip", "other"}, k=2) == 1.0
