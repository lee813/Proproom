# Proproom — the prop room for your agents

A NAS-native media library: **one container, one folder.** Open-licensed images, video and audio are stored
with their provenance and rights evidence, searched with hybrid multimodal-vector + keyword recall, and
delivered to projects with a dry-run plan and safe retries.

> Status: **skeleton** (stage 1 of the build order in [docs/HANDOVER.md](docs/HANDOVER.md) §15).
> The schema, boot sequence, operation registry, `prop capabilities` and the HTTP shell work;
> everything else is declared with its contract and returns `not_implemented`.

## Run

```bash
docker run -d --name proproom --restart unless-stopped \
  -v /path/on/nas:/nas -p 8765:8765 \
  --gpus all          `# NVIDIA only` \
  --device /dev/dri   `# Intel iGPU only` \
  ghcr.io/lee813/proproom:latest
```

or `compose.yaml` in this repo. On first start the library (default `/nas/proproom/`, or `home` in
`/nas/.proproom.toml`, or env `PROP_HOME`) gets `db.sqlite`, `blobs/ thumbs/ models/ plugins/ tmp/ journal/
backups/`, a commented `config.toml` and `instance.token` (the management API token).

```bash
docker exec proproom prop capabilities
docker exec proproom prop db status
curl -H "Authorization: Bearer $(cat /path/on/nas/proproom/instance.token)" http://nas:8765/v1/capabilities
```

## Layout

```
proproom/
  core/       paths bootstrap config db writer ids cjk hardware  (+ stubs: store rights dedupe download jobs
              ops journal events http search sheet delivery plugins spaces)
  core/sql/   named queries (hybrid_recall.sql = appendix B.2)
  migrations/ 0001_init.sql (appendix B.1), forward-only, PRAGMA user_version
  sdk/        the only import surface for plugins: types, protocols, contexts, errors
  api/        registry.py (operations + schemas) -> cli.py, http.py, mcp.py, share/
  plugins/builtin/   commons wikidata capture archive_org embedgemma2 scenecut ocr_flags loudness regions
                     assets_json credits_basic
docs/ eval/ tests/
```

## Requirements

SQLite ≥ 3.37 (STRICT) with FTS5 trigram, sqlite-vec ≥ 0.1.6 (pinned 0.1.9). The CUDA image ships
SQLite 3.45.1.

## Responsibility, terms, trust

- Rights evidence comes from each source. Proproom records it, enforces the policy you configure and points out
  obligations; it gives no legal advice and the final call is yours.
- Respect every site's terms and rate limits. Sources that need API keys are enabled only when you add your key
  to `keys.toml`.
- Plugins are arbitrary code running in the container — trust them like `pip install`. The SDK boundaries prevent
  mistakes, not malice. `prop plugin list` shows each plugin's hash.
- `-p 8765:8765` listens on every interface. Never forward it to the internet; bind to the LAN IP
  (`-p <LAN-IP>:8765:8765`) or put a reverse proxy with HTTPS / Tailscale in front.

## License

Apache-2.0. No media, model weights or keys are ever committed.
