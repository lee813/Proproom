"""§7 search (and the §8 selection contract it produces)."""
from __future__ import annotations

import json

from support import gap, ingest

from proproom.api.registry import REGISTRY


def _search(ctx, **args):
    return REGISTRY["search"](ctx, args)


def test_scores_are_typed_rrf_not_probabilities(ctx, conn, home):
    """§7: score_type 'rrf', never a probability-looking number."""
    ingest(conn, home, slug="toc", title="Alexis de Tocqueville portrait")
    res = _search(ctx, query="Tocqueville")
    assert res["score_type"] == "rrf"
    assert all(0 < it["score"] < 0.1 for it in res["items"])  # RRF k=60: one channel at rank 1 = 1/61


def test_cjk_query_variants(ctx, conn, home):
    """§7 / B note 4: '托克维尔', '托克维尔肖像', '托克维尔的肖像' hit; 2-char '勒庞' falls back to LIKE."""
    t = ingest(conn, home, slug="toc", title="Tocqueville", cjk_text="托克维尔 肖像")
    lp = ingest(conn, home, slug="lepen", title="Le Pen", cjk_text="勒庞")
    for q in ("托克维尔", "托克维尔肖像", "托克维尔的肖像"):
        assert t.asset_id in [i["asset_id"] for i in _search(ctx, query=q)["items"]], q
    assert [i["asset_id"] for i in _search(ctx, query="勒庞")["items"]] == [lp.asset_id]


def test_denied_and_inactive_assets_never_returned(ctx, conn, home):
    ok = ingest(conn, home, slug="ok", title="parliament chamber")
    ingest(conn, home, slug="nc", title="parliament chamber nc", license_id="cc-by-nc-4.0")
    hidden = ingest(conn, home, slug="hidden", title="parliament chamber hidden")
    conn.execute("UPDATE asset SET status='hidden' WHERE id = ?", (hidden.asset_id,))
    assert [i["asset_id"] for i in _search(ctx, query="parliament")["items"]] == [ok.asset_id]


def test_selection_pins_numbers(ctx, conn, home):
    """§8: a selection maps the printed numbers to fixed items; later searches never renumber it."""
    for n in range(5):
        ingest(conn, home, slug=f"p{n}", title=f"parliament {n}")
    res = _search(ctx, query="parliament")
    third = res["items"][2]["asset_id"]
    ingest(conn, home, slug="p-new", title="parliament parliament parliament")
    row = conn.execute("SELECT asset_id FROM selection_item WHERE selection_id = ? AND idx = 3",
                       (res["selection_id"],)).fetchone()
    assert row["asset_id"] == third
    assert json.loads(conn.execute("SELECT explain FROM selection_item WHERE selection_id = ? AND idx = 1",
                                   (res["selection_id"],)).fetchone()[0])["score_type"] == "rrf"


def test_filters_apply_during_recall(ctx, conn, home):
    """§7: 过滤条件在召回阶段就生效 — 60 better-matching footage items must not crowd out the only photo."""
    for n in range(60):
        ingest(conn, home, slug=f"f{n}", title="parliament", kind="footage")
    photo = ingest(conn, home, slug="photo", title="parliament chamber interior wide view during a session")
    assert [i["asset_id"] for i in _search(ctx, query="parliament", kind=["photo"])["items"]] == [photo.asset_id]


def test_license_filter(ctx, conn, home):
    ingest(conn, home, slug="sa", title="harbour cranes", license_id="cc-by-sa-4.0")
    zero = ingest(conn, home, slug="zero", title="harbour cranes at dusk", license_id="cc0-1.0")
    assert [i["asset_id"] for i in _search(ctx, query="harbour", license="cc0")["items"]] == [zero.asset_id]


def test_min_edge_never_satisfied_by_a_smaller_representation(ctx, conn, home):
    ingest(conn, home, slug="small", title="factory floor", width=800, height=600,
           extra_reps=[{"role": "thumb_400", "data": b"thumb", "width": 400, "height": 300}])
    assert _search(ctx, query="factory", min_edge=1080)["items"] == []


def test_exclude_used_in_matches_the_exact_project(ctx, conn, home, nas):
    a = ingest(conn, home, slug="a", title="ice hockey arena")
    (nas / "project" / "SiennaCanadaV2" / "assets").mkdir(parents=True)
    REGISTRY["link"](ctx, {"assets": [a.asset_id], "project": "project/SiennaCanadaV2"})
    (nas / "project" / "SiennaCanada").mkdir(parents=True)
    assert [i["asset_id"] for i in _search(ctx, query="hockey", exclude_used_in="project/SiennaCanada")["items"]] \
        == [a.asset_id]


def test_person_anchor_excludes_synthetic(ctx, conn, home):
    """§6 / §7: person/event/data anchors never return generated content."""
    real = ingest(conn, home, slug="real", title="Tocqueville portrait", kind="portrait")
    ingest(conn, home, slug="ai", title="Tocqueville portrait illustration", kind="portrait", basis="own_work",
           license_id=None, synthetic=True)
    assert [i["asset_id"] for i in _search(ctx, query="Tocqueville", anchor="person")["items"]] == [real.asset_id]


def test_long_footage_cannot_fill_every_slot(ctx, conn, home):
    """§7: at most 2 segments of the same parent by default."""
    from proproom.core.ids import ulid
    f = ingest(conn, home, slug="long", title="city skyline aerial", kind="footage", mime="video/webm")
    for n in range(6):
        conn.execute("INSERT INTO segment(id, representation_id, t0, t1, algo, algo_version, params) "
                     "VALUES (?, ?, ?, ?, 'scenecut', '1', '{}')", (ulid(), f.rep_id, n * 10.0, n * 10.0 + 8))
    res = _search(ctx, query="skyline")
    segs = [i for i in res["items"] if i["asset_id"] == f.asset_id and i["segment_id"]]
    assert 1 <= len(segs) <= 2
