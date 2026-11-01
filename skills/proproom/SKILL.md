---
name: proproom
description: Find, fetch and deliver footage, photos, music and sound for a video project from a Docker-hosted Proproom media library. Use when an episode needs stock or archive material, when the user asks for "a link of X", when importing a project's media, or when restocking the library from Wikimedia Commons or archive.org.
---

# Proproom

Proproom is a Docker-hosted media library for agents. It searches local media by text and by picture, fetches from
remote sources (Wikimedia Commons, archive.org, web captures), copies picks into a project folder, and writes the credits.

## How to call it

Every operation is a `prop` command that prints JSON.

- On the Docker host: `docker exec proproom prop <operation> ...`
- From another machine, use the direct HTTP client from a Proproom checkout:
  `python3 tools/prop_http.py <operation> --json '<arguments>'`.
  For example: `python3 tools/prop_http.py search --json '{"query":"US dollar"}'`.
  Set `PROP_SERVER` to the server's base URL (or pass `--server`), e.g. `http://host:8765`. It connects without SSH.
  On first use it registers against the same Docker instance as described below. Do not fall back to SSH or a
  separate local library.
- As MCP: `docker exec -i proproom prop mcp` (same operations as tools).

### First-time Docker registration

The container generates an instance token on its first start and keeps it across restarts. On the first HTTP call,
the client checks the Mac Keychain by instance ID. If absent, and Docker is available through an authorized non-SSH
context, it reads the token directly from the matching container using `docker exec proproom prop credential`, checks
the instance ID, verifies the token with the server, and saves it to Keychain. It captures the credential privately:
never print it in chat or logs. Subsequent calls use Keychain without Docker, SSH or a mounted share.

When Docker is not directly available on this Mac, register once with
`python3 tools/prop_http.py register`; enter the Docker-issued token into the hidden
prompt, never in chat or a command argument. The token can be retrieved by an authorized operator from the container
with `docker exec proproom prop credential`. Do not rotate it, bypass a denied browser permission or silently create
another library. `PROP_DOCKER_CONTAINER` selects a different container name. SSH Docker contexts are not used.

The `prop credential` command must be present in the running image; if an older image does not have it, upgrade the
container or use the existing Docker-issued token to register. Verify the connection with `capabilities --json '{}'`.

With an `http://` endpoint the bearer token travels unencrypted on that network. Only use it on the
user's trusted LAN; use an appropriately secured HTTPS endpoint if remote access is required.

Arguments are `--name value`. Lists and objects are JSON: `--pick '[3,7]'`, `--assets '["01M6...","01M6..."]'`,
`--kind '["footage"]'`. `prop capabilities` lists every operation and its schema.

Errors come back as `{"error": {"code", "message", "hint"}}`. A bare `500 Internal Server Error` usually means a
required argument is missing (known gap).

Paths in results (`sheet`, `file`) are relative to the configured library root. In this deployment, projects are
relative to the Causcape root, e.g. `project/Episode12`.

## The normal flow

```bash
# 1. Search the library. --sheet renders a numbered contact sheet you can look at.
prop search "oil tanker in a canal" --k 12 --sheet
#    -> selection_id S1, items[] with idx, title, kind, duration, license_id, attribution, ranks, vec_distance

# 2. Deliver picks into the project: preview, then run the plan.
prop link --project project/Episode12 --selection_id S1 --pick '[3,7]' --plan
prop link --plan_id P1

# 3. Credits for everything delivered to the project.
prop credits project/Episode12
```

Look at the contact sheet before picking. `ranks` says which channel found an item: `fts` is a text match, `vec`
is picture similarity only. A low `vec_distance` (about 0.20–0.25) is close; vector-only hits beyond that are often
unrelated, because search always fills the list.

## Rules

- **Give the raw result.** When the user asks for "a link of X", run `prop search "X"` and share that selection as
  it is: `prop share --selection_id S1`. Don't add, drop or reorder items yourself. The links are how the user
  tests search. Say afterwards what looks wrong, and offer a curated link only as an extra.
- **Use the library first.** Fetch from a remote source only when the user asks, or after telling them the library
  is thin and they agree. Don't default to Commons; ask which source if it isn't clear.
- **Material is usable by default.** Search only returns allowed items. Only real evidence blocks something (an
  NC/ND/editorial license, a news-agency photo, a deletion or copyvio tag). Never invent where a file came from or
  what its license is.
- **Credit what you use.** `link` records each item's attribution; finish with `prop credits`.
- Never write into the library folder yourself, and never delete from it.

## Sharing a selection

```bash
prop share --selection_id S1 --title "Trains"      # or --assets '["<asset_id>", ...]'
```

Returns `url` (`<server>/s/<token>`). The page is view-only. `prop share_revoke --share <token>`
turns a link off.

For a share URL `<url>`, its token also authorizes these HTTP endpoints:

- `GET <url>/items` returns the shared items as JSON, including each `representation_id`.
- `GET <url>/media/<representation_id>` returns that shared file and supports HTTP Range requests; append
  `?download=1` to request an attachment with a filename.
- `GET <url>/download.zip` streams all files in the share as a ZIP, ending with a `CREDITS.txt` for the files in it. For selected items, pass repeated item positions,
  for example `GET <url>/download.zip?pos=1&pos=3`; these positions come from `GET <url>/items`.
- On the share page, use each card's Download file button, or select cards and choose Download selected (ZIP).
  Download all (ZIP) includes every shared item. Selections on the page are for download only, not project usage.

The attachment option and ZIP controls require a server version with share-download support; if they are absent or
the ZIP endpoint returns 404 for an otherwise valid share, that server may not have the update deployed yet.

These URLs work only while the share is valid. Treat the token-bearing URL as a secret. Downloading files does not
record project usage or deliver them to a project: use `prop link` and then `prop credits` for that workflow.

## Remote sources

```bash
prop remote commons "freight train" --kind '["footage"]' --sheet   # candidates only, nothing stored
prop fetch --selection_id S2 --pick '[2]'                          # rights check, download, add to library
prop index                                                         # make fetched items searchable by picture
```

Sources: `commons`, `archive_org`, `capture` (screenshot of a web page). `--kind` narrows the search to video,
photo or audio.

## Adding the user's own files

```bash
prop add --paths '["/nas/assets/broll/clip.mp4"]'   # local files; usable by default
prop import_project --project project/Episode12     # assets.json entries + files under assets/ and images/
prop index
```

`import_project` works with or without `assets.json`, and its result lists anything blocked under `blocked_items`.
Tell the user about those.

## Restocking in bulk

```bash
prop pull --spec assets/library/pull.yaml --budget 1G     # background job
prop job --action get --job_id <id>                       # also pause / resume / cancel
```

A spec is a list of lines:

```yaml
jobs:
  - {source: commons, kind: footage, search: "aerial city skyline", share: 0.15}
  - {source: archive_org, kind: footage, collection: prelinger, search: "industry OR city", share: 0.15}
  - {source: commons, kind: photo, category: "Government buildings", share: 0.10}
```

The budget is split by `share`; what one line doesn't use passes to the next. A file that doesn't fit its line's
share is skipped (`deferred`) and the line ends. Items of the wrong kind are skipped. A line whose source doesn't
exist is skipped (`lines_skipped`). When the pull finishes it queues indexing. A 1080p video is often 50–300 MB, so
a small budget brings in photos and sound but little footage.
