# Handover conformance suite

Tests that check the implementation against [docs/HANDOVER.md](../../docs/HANDOVER.md), one file per section
(`test_s07_search.py` ↔ §7 …). Each test's docstring names the rule it checks.

Known gaps are marked `@gap("…")` — a **strict xfail**:

- the suite stays green while the gap exists (`deploy/fnos/deploy.sh test` → `N passed, M xfailed`);
- once a fix makes the test pass it turns **red (XPASS)**: the reviewer deletes the `@gap` line, and the rule is then enforced.

This folder is owned by the reviewer. Implementers don't edit it; their own tests go in `tests/impl/` (see `AGENTS.md`).
`test_review_findings.py` holds gaps found by probing after the first fix round. The file docstrings of it and of
`test_s07_embeddings.py` state the contracts they add beyond the handover (`core.worker.tick`, the `index` operation, the embedder inputs).

```bash
# list every open gap with its reason
python -m pytest tests/handover -q -rx
# run the gaps as ordinary failures (what a fix has to make green)
python -m pytest tests/handover --runxfail -q --tb=line
```

All tests are offline: sources get a `FakeHttp` fixture, downloads go through the `fake_download` fixture,
and in-memory source plugins are attached with `support.install_source()`. Nothing touches the live library.

`test_round3_findings.py` holds the gaps from the tasks 1b–7 review: fake job success, hook retry, pull budget
and steps. Its docstring states the job contract those tests rely on.

`test_round4_findings.py` holds the gaps from the task R review: the stepped pull re-queries its source, and
a transient error rejects a candidate for good.

`test_s13_analysis.py` states the analysis contract: analyzers and segmenters run from the worker's backlog
(never at ingest), once per plugin version, validated by core, retried with backoff; and the SDK 1.0 range check.
