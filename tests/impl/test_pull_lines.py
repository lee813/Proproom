"""A pull spreads its budget over the spec's lines by `share`, keeps to each line's `kind`, skips a line whose source
is missing, says why a candidate failed, and queues indexing when it's done (§13)."""
from __future__ import annotations

from support import install_source

from proproom.api.registry import REGISTRY
from proproom.core import db, worker
from proproom.core.plugins import LoadedPlugin
from proproom.sdk.types import (Candidate, CandidatePage, EmbeddingBatch, RepresentationOffer, ResolvedSource,
                                RightsEvidence, SourceRef)


class _Src:
    """Each candidate's offer states its size, equal to what the fake downloader writes."""

    def __init__(self, name, n, kind="photo", fail=None):
        self.name, self.ids, self.kind, self.fail = name, [f"{name}{i}" for i in range(n)], kind, fail
        self.resolved: list[str] = []

    async def search(self, ctx_, req):
        return CandidatePage(items=[Candidate(ref=SourceRef(self.name, x), title=x) for x in self.ids])

    async def resolve(self, ctx_, ref):
        self.resolved.append(ref.external_id)
        if self.fail:
            raise self.fail
        url = f"https://example.org/{ref.external_id}.jpg"
        return ResolvedSource(ref=ref, title=ref.external_id, kind=self.kind, metadata={},
                              evidence=[RightsEvidence(basis="license", license_id="CC BY 4.0")],
                              offers=[RepresentationOffer("original", "image/jpeg", url,
                                                          bytes=len(f"downloaded:{url}"))])


def _install(ctx, *sources):
    pm = install_source(ctx, sources[0].name, sources[0])
    for s in sources[1:]:
        pm.plugins[s.name] = LoadedPlugin(name=s.name, version="0.0.1", hash="0" * 64, types=["source"],
                                          origin="dir", manifest={"domains": {"download": ["example.org"]}}, instance=s)
    return pm


def _run(ctx, nas, spec, budget):
    (nas / "pull.yaml").write_text(spec)
    job = REGISTRY["pull"](ctx, {"spec": "pull.yaml", "budget": budget})
    for _ in range(500):
        if not worker.tick(ctx):
            break
    return db.connect(ctx.booted.home.db).execute("SELECT * FROM job WHERE id = ?", (job["job_id"],)).fetchone()


def _taken(ctx, source):
    return db.connect(ctx.booted.home.db).execute(
        "SELECT count(*) FROM pull_seen WHERE source = ? AND decision = 'taken'", (source,)).fetchone()[0]


def test_the_budget_is_split_by_share_and_a_spent_line_stops_looking(ctx, nas, fake_download):
    a, b = _Src("a", 20), _Src("b", 20)
    _install(ctx, a, b)
    size = len("downloaded:https://example.org/a0.jpg")
    job = _run(ctx, nas, "jobs:\n  - {source: a, search: x, share: 0.5}\n  - {source: b, search: x, share: 0.5}\n",
               str(size * 4))
    assert job["state"] == "succeeded"
    assert _taken(ctx, "a") == 2 and _taken(ctx, "b") == 2
    assert set(a.resolved) == {"a0", "a1"}  # its share spent, line a never looks at the other 18


def test_a_line_takes_only_its_kind(ctx, nas, fake_download):
    _install(ctx, _Src("v", 3, kind="footage"))
    _run(ctx, nas, "jobs:\n  - {source: v, kind: photo, search: x}\n", "1M")
    c = db.connect(ctx.booted.home.db)
    assert _taken(ctx, "v") == 0
    assert {r[0] for r in c.execute("SELECT reason FROM pull_seen WHERE source = 'v'")} == {"kind_footage"}


def test_a_missing_source_skips_its_line_only(ctx, nas, fake_download):
    _install(ctx, _Src("a", 2))
    job = _run(ctx, nas, "jobs:\n  - {source: met, search: x}\n  - {source: a, search: x}\n", "1M")
    assert job["state"] == "succeeded" and _taken(ctx, "a") == 2
    assert '"source": "met"' in job["progress"]


def test_an_error_without_a_message_still_says_what_it_was(ctx, nas, fake_download):
    _install(ctx, _Src("t", 1, fail=TimeoutError()))
    _run(ctx, nas, "jobs:\n  - {source: t, search: x}\n", "1M")
    err = db.connect(ctx.booted.home.db).execute("SELECT last_error FROM pull_seen").fetchone()[0]
    assert err.startswith("TimeoutError")


class _Embedder:
    fingerprint = {"model": "probe", "revision": "1", "modalities": ["image"], "preprocess": {}, "prompts": {},
                   "pooling": "mean", "normalize": 1, "truncation": 4}
    dim = 4

    async def embed(self, ctx, inputs):
        return EmbeddingBatch(space_fingerprint=self.fingerprint, vectors=[[1.0, 0, 0, 0] for _ in inputs],
                              input_hashes=[str(i) for i, _ in enumerate(inputs)])


def test_a_finished_pull_queues_indexing(ctx, nas, fake_download):
    pm = _install(ctx, _Src("a", 2))
    pm.plugins["emb"] = LoadedPlugin("emb", "0", "0" * 64, ["embedder"], "dir", {}, _Embedder())
    _run(ctx, nas, "jobs:\n  - {source: a, search: x}\n", "1M")
    kinds = [r[0] for r in db.connect(ctx.booted.home.db).execute("SELECT kind FROM job ORDER BY created")]
    assert kinds == ["pull", "index"]


def test_a_file_of_unknown_size_that_does_not_fit_is_not_downloaded(ctx, nas, monkeypatch):
    """The offer gives no size; the download sees the server's Content-Length and refuses what doesn't fit."""
    from proproom.core import download, store
    from proproom.sdk.errors import ProproomError
    sizes = {"big": 500, "small": 50}
    written: list[str] = []

    async def _dl(home_, url, *, max_bytes=None, **_):
        name = url.rsplit("/", 1)[-1].split(".")[0]
        if max_bytes and sizes[name] > max_bytes:
            raise ProproomError("budget_exceeded", "Remote file size exceeds budget")
        written.append(name)
        p = home_.dir("tmp") / f"dl_{name}"
        p.write_bytes(b"x" * sizes[name])
        return p, store.hash_bytes(p.read_bytes())[0], sizes[name]

    monkeypatch.setattr(download, "download_file", _dl)

    class _Unsized(_Src):
        async def resolve(self, ctx_, ref):
            r = await super().resolve(ctx_, ref)
            r.offers[0].bytes = None
            return r

    src = _Unsized("u", 0)
    src.ids = ["big", "small"]
    _install(ctx, src)
    job = _run(ctx, nas, "jobs:\n  - {source: u, search: x}\n", "100")
    assert written == [] and job["used_bytes"] == 0
    reason = db.connect(ctx.booted.home.db).execute("SELECT decision, reason FROM pull_seen WHERE external_id = 'big'")
    assert tuple(reason.fetchone()) == ("deferred", "budget")
