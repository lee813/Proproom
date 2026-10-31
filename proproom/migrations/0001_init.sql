-- Proproom schema v1 (migrations/0001_init.sql). SQLite >= 3.37 (STRICT), FTS5 with trigram (>= 3.34), sqlite-vec >= 0.1.6.
-- Conventions: ids are ULIDs (26 chars) unless noted; times are UTC ISO-8601 text; JSON columns are validated.

-- ───────────────────────── instance / plugins ─────────────────────────
CREATE TABLE instance (
  key   TEXT PRIMARY KEY,                 -- instance_id, created, schema_note …
  value TEXT NOT NULL
) STRICT;

CREATE TABLE plugin_registry (
  name      TEXT NOT NULL,
  version   TEXT NOT NULL,
  hash      TEXT NOT NULL,                 -- sha256 of the plugin file / directory
  types     TEXT NOT NULL CHECK (json_valid(types)),
  origin    TEXT NOT NULL CHECK (origin IN ('builtin','dir','entrypoint','exec')),
  enabled   INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0,1)),
  loaded_at TEXT NOT NULL,
  PRIMARY KEY (name, version, hash)
) STRICT;

-- ───────────────────────── identity ─────────────────────────
CREATE TABLE asset (
  id          TEXT PRIMARY KEY CHECK (length(id) = 26),
  rid         INTEGER NOT NULL UNIQUE,     -- integer key for FTS rowid
  slug        TEXT NOT NULL UNIQUE,
  kind        TEXT NOT NULL CHECK (kind IN ('photo','portrait','document','screenshot','artwork','footage',
                                            'music','sfx','ambience','font','dataset','graphic')),
  title       TEXT NOT NULL,
  description TEXT,
  synthetic   INTEGER NOT NULL DEFAULT 0 CHECK (synthetic IN (0,1)),   -- AI / procedurally generated
  generator   TEXT CHECK (generator IS NULL OR json_valid(generator)), -- {tool, version, prompt?} for own/synthetic work
  rating      INTEGER CHECK (rating BETWEEN 1 AND 5),
  status      TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','hidden','rejected')),
  created     TEXT NOT NULL,
  updated     TEXT NOT NULL
) STRICT;

CREATE TABLE asset_slug_history (
  slug     TEXT PRIMARY KEY,
  asset_id TEXT NOT NULL REFERENCES asset(id) ON DELETE CASCADE,
  retired  TEXT NOT NULL
) STRICT;

-- ───────────────────────── bytes / versions ─────────────────────────
CREATE TABLE blob (
  sha256       TEXT PRIMARY KEY CHECK (length(sha256) = 64),
  bytes        INTEGER NOT NULL CHECK (bytes >= 0),
  mime         TEXT NOT NULL,
  stored       INTEGER NOT NULL CHECK (stored IN (0,1)),     -- 1 = in <home>/blobs (CAS); 0 = only at external locations
  storage_path TEXT UNIQUE,                                   -- relative to <home>; NULL when stored = 0
  created      TEXT NOT NULL,
  verified_at  TEXT,
  verify_state TEXT NOT NULL DEFAULT 'unverified' CHECK (verify_state IN ('unverified','ok','mismatch','missing')),
  CHECK ((stored = 1) = (storage_path IS NOT NULL))
) STRICT;

CREATE TABLE representation (
  id             TEXT PRIMARY KEY CHECK (length(id) = 26),
  asset_id       TEXT NOT NULL REFERENCES asset(id) ON DELETE CASCADE,
  blob_sha256    TEXT NOT NULL REFERENCES blob(sha256),
  role           TEXT NOT NULL,             -- original | thumb_2000 | vp9_1080 | cut | crop | capture | snapshot …
  width          INTEGER, height INTEGER,
  duration       REAL, fps REAL, codec TEXT,
  audio_channels INTEGER, sample_rate INTEGER,
  loudness_lufs  REAL, loudness_range REAL, true_peak REAL,  -- ebur128, audio only
  phash          TEXT,                      -- images / keyframe; 64-bit hex
  max_display_px INTEGER,                   -- largest on-screen edge without upscaling
  derived_from   TEXT REFERENCES representation(id),
  derivation     TEXT CHECK (derivation IS NULL OR json_valid(derivation)),  -- {op:'cut', t0, t1, exact, tool, args}
  created        TEXT NOT NULL,
  UNIQUE (asset_id, blob_sha256, role)
) STRICT;
CREATE INDEX representation_asset ON representation(asset_id);
CREATE INDEX representation_blob  ON representation(blob_sha256);

CREATE TABLE watch_root (
  id        INTEGER PRIMARY KEY,
  path      TEXT NOT NULL UNIQUE,           -- relative to /nas
  exclude   TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(exclude)),   -- globs, e.g. ["**/tmp/**","**/canvas/**"]
  created   TEXT NOT NULL,
  last_scan TEXT
) STRICT;

CREATE TABLE external_location (
  id                INTEGER PRIMARY KEY,
  representation_id TEXT NOT NULL REFERENCES representation(id) ON DELETE CASCADE,
  root_id           INTEGER NOT NULL REFERENCES watch_root(id),
  relpath           TEXT NOT NULL,
  size              INTEGER NOT NULL,
  mtime_ns          INTEGER NOT NULL,
  sha256_at_index   TEXT NOT NULL,
  state             TEXT NOT NULL DEFAULT 'ok' CHECK (state IN ('ok','changed','missing')),
  checked_at        TEXT NOT NULL,
  UNIQUE (root_id, relpath)
) STRICT;
CREATE INDEX external_location_rep ON external_location(representation_id);

-- ───────────────────────── provenance / rights ─────────────────────────
CREATE TABLE provenance (
  id              TEXT PRIMARY KEY CHECK (length(id) = 26),
  asset_id        TEXT NOT NULL REFERENCES asset(id) ON DELETE CASCADE,
  source          TEXT NOT NULL,            -- plugin source name: commons, capture, archive_org, manual, import …
  external_id     TEXT NOT NULL,            -- e.g. "File:Alexis de Tocqueville.jpg", or a URL for capture
  revision        TEXT NOT NULL DEFAULT '', -- '' when the source has no revisions
  source_url      TEXT,
  final_url       TEXT,                     -- after redirects
  publisher       TEXT,
  publisher_type  TEXT CHECK (publisher_type IN ('official','primary','wire','media','archive','museum','stock','own','community')),
  retrieved       TEXT NOT NULL,
  raw_metadata    TEXT NOT NULL CHECK (json_valid(raw_metadata)),
  snapshot_sha256 TEXT REFERENCES blob(sha256),  -- page snapshot (MHTML/PDF) kept as evidence
  plugin          TEXT NOT NULL, plugin_version TEXT NOT NULL, plugin_hash TEXT NOT NULL,
  UNIQUE (source, external_id, revision)
) STRICT;
CREATE INDEX provenance_asset ON provenance(asset_id);

CREATE TABLE rights_evidence (
  id            TEXT PRIMARY KEY CHECK (length(id) = 26),
  provenance_id TEXT NOT NULL REFERENCES provenance(id) ON DELETE CASCADE,
  basis         TEXT NOT NULL CHECK (basis IN ('license','public_domain','provider_license','quotation','own_work','unknown')),
  license_id    TEXT,                       -- normalized: cc-by-4.0, cc-by-sa-3.0, cc0-1.0, pexels-license …
  license_url   TEXT,
  pd_reason     TEXT,                       -- pd-old | pd-us-gov | pd-mark | cc0 | … (basis = public_domain)
  jurisdiction  TEXT,                       -- where the PD claim holds, e.g. 'US', 'worldwide'
  attribution   TEXT,                       -- author / required credit text as given
  restrictions  TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(restrictions)),  -- ["trademarked","personality","fop", …]
  covers        TEXT NOT NULL DEFAULT 'work' CHECK (covers IN ('work','recording','both')),  -- music: composition vs recording
  original_text TEXT,                       -- licence text exactly as the source states it
  locator       TEXT,                       -- where in the source the evidence sits (field path / CSS selector / page)
  origin        TEXT NOT NULL CHECK (origin IN ('source','machine','human')),
  created_by    TEXT NOT NULL,              -- plugin name or actor
  created       TEXT NOT NULL
) STRICT;
CREATE INDEX rights_evidence_prov ON rights_evidence(provenance_id);

CREATE TABLE policy (
  version    INTEGER PRIMARY KEY,
  rules      TEXT NOT NULL CHECK (json_valid(rules)),   -- core defaults + admin config + plugin restrictions, resolved
  created    TEXT NOT NULL,
  created_by TEXT NOT NULL,
  note       TEXT
) STRICT;

CREATE TABLE rights_assessment (
  id                TEXT PRIMARY KEY CHECK (length(id) = 26),
  asset_id          TEXT NOT NULL REFERENCES asset(id) ON DELETE CASCADE,
  representation_id TEXT REFERENCES representation(id) ON DELETE CASCADE,  -- NULL = whole asset
  policy_version    INTEGER NOT NULL REFERENCES policy(version),
  decision          TEXT NOT NULL CHECK (decision IN ('allow','review','deny')),
  basis             TEXT CHECK (basis IN ('license','public_domain','provider_license','quotation','own_work')),  -- basis the decision rests on
  reasons           TEXT NOT NULL CHECK (json_valid(reasons)),       -- [{code, detail, plugin?}]
  evidence_ids      TEXT NOT NULL CHECK (json_valid(evidence_ids)),
  obligations       TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(obligations)),  -- attribution, share_alike, synthetic_disclosure …
  assessed_at       TEXT NOT NULL,
  assessed_by       TEXT NOT NULL
) STRICT;
CREATE INDEX rights_assessment_asset ON rights_assessment(asset_id, assessed_at);

-- latest whole-asset assessment
CREATE VIEW asset_rights AS
SELECT ra.* FROM rights_assessment ra
WHERE ra.representation_id IS NULL
  AND ra.assessed_at = (SELECT max(assessed_at) FROM rights_assessment x
                        WHERE x.asset_id = ra.asset_id AND x.representation_id IS NULL);

-- ───────────────────────── semantics ─────────────────────────
CREATE TABLE entity (
  qid         TEXT PRIMARY KEY CHECK (qid GLOB 'Q[0-9]*'),
  labels      TEXT NOT NULL CHECK (json_valid(labels)),     -- {"en":"Alexis de Tocqueville","zh":"托克维尔",…}
  aliases     TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(aliases)),
  description TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(description)),
  birth       TEXT, death TEXT,                             -- P569 / P570, as given (year precision allowed)
  fetched     TEXT NOT NULL
) STRICT;

CREATE TABLE asset_entity (
  asset_id TEXT NOT NULL REFERENCES asset(id) ON DELETE CASCADE,
  qid      TEXT NOT NULL REFERENCES entity(qid),
  role     TEXT NOT NULL CHECK (role IN ('depicts','about','author','location')),
  origin   TEXT NOT NULL CHECK (origin IN ('source','machine','human')),
  PRIMARY KEY (asset_id, qid, role)
) STRICT;
CREATE INDEX asset_entity_qid ON asset_entity(qid, role);

CREATE TABLE alias (
  id       INTEGER PRIMARY KEY,
  asset_id TEXT NOT NULL REFERENCES asset(id) ON DELETE CASCADE,
  text     TEXT NOT NULL,
  lang     TEXT,
  origin   TEXT NOT NULL CHECK (origin IN ('source','entity','machine','human')),
  UNIQUE (asset_id, text)
) STRICT;

CREATE TABLE tag (
  asset_id TEXT NOT NULL REFERENCES asset(id) ON DELETE CASCADE,
  tag      TEXT NOT NULL,
  origin   TEXT NOT NULL CHECK (origin IN ('source','machine','human')),
  PRIMARY KEY (asset_id, tag)
) STRICT;

CREATE TABLE note (
  id       INTEGER PRIMARY KEY,
  asset_id TEXT NOT NULL REFERENCES asset(id) ON DELETE CASCADE,
  author   TEXT NOT NULL,                   -- agent name / human
  text     TEXT NOT NULL,
  created  TEXT NOT NULL
) STRICT;

CREATE TABLE region (
  id                INTEGER PRIMARY KEY,
  representation_id TEXT NOT NULL REFERENCES representation(id) ON DELETE CASCADE,
  kind              TEXT NOT NULL CHECK (kind IN ('face','plate','text','subject','logo','highlight')),
  x REAL NOT NULL, y REAL NOT NULL, w REAL NOT NULL, h REAL NOT NULL,   -- fractions of the frame, 0..1
  t     REAL,                               -- video: time of the frame the box was measured on
  score REAL,
  origin TEXT NOT NULL CHECK (origin IN ('machine','human')),
  plugin TEXT,
  created TEXT NOT NULL,
  CHECK (x >= 0 AND y >= 0 AND w > 0 AND h > 0 AND x + w <= 1.0001 AND y + h <= 1.0001)
) STRICT;
CREATE INDEX region_rep ON region(representation_id, kind);

CREATE TABLE segment (
  id                TEXT PRIMARY KEY CHECK (length(id) = 26),
  representation_id TEXT NOT NULL REFERENCES representation(id) ON DELETE CASCADE,
  t0 REAL NOT NULL, t1 REAL NOT NULL,
  thumb_sha256      TEXT REFERENCES blob(sha256),
  algo TEXT NOT NULL, algo_version TEXT NOT NULL,
  params            TEXT NOT NULL CHECK (json_valid(params)),
  CHECK (t0 >= 0 AND t1 > t0),
  UNIQUE (representation_id, algo, algo_version, t0)
) STRICT;
CREATE INDEX segment_rep ON segment(representation_id, t0);

CREATE TABLE flag (
  id            INTEGER PRIMARY KEY,
  target_type   TEXT NOT NULL CHECK (target_type IN ('asset','representation','segment')),
  target_id     TEXT NOT NULL,
  kind          TEXT NOT NULL,              -- watermark|logo|burned_subs|lowres|map|sensitive|disputed|broadcast_origin|restriction|recording_rights|nsfw …
  severity      TEXT NOT NULL CHECK (severity IN ('info','warn','block')),
  score         REAL,
  evidence      TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(evidence)),
  origin        TEXT NOT NULL CHECK (origin IN ('source','machine','human')),
  plugin        TEXT, plugin_version TEXT,
  created       TEXT NOT NULL,
  resolution    TEXT CHECK (resolution IN ('confirmed','dismissed')),
  resolved_by   TEXT, resolved_at TEXT
) STRICT;
CREATE INDEX flag_target ON flag(target_type, target_id);
CREATE INDEX flag_open   ON flag(kind) WHERE resolution IS NULL;

CREATE TABLE similar (
  a        TEXT NOT NULL REFERENCES representation(id) ON DELETE CASCADE,
  b        TEXT NOT NULL REFERENCES representation(id) ON DELETE CASCADE,
  kind     TEXT NOT NULL CHECK (kind IN ('phash','embedding')),
  distance REAL NOT NULL,
  PRIMARY KEY (a, b, kind),
  CHECK (a < b)
) STRICT;

-- ───────────────────────── vectors ─────────────────────────
CREATE TABLE embedding_space (
  id          INTEGER PRIMARY KEY,
  fingerprint TEXT NOT NULL UNIQUE,         -- sha256 of the canonical JSON below
  model       TEXT NOT NULL, revision TEXT NOT NULL,
  modalities  TEXT NOT NULL CHECK (json_valid(modalities)),   -- ["text","image","video"]
  preprocess  TEXT NOT NULL CHECK (json_valid(preprocess)),   -- fps, max frames, image size …
  prompts     TEXT NOT NULL CHECK (json_valid(prompts)),      -- {"query":"SearchQuery","doc":"Document"}
  pooling     TEXT NOT NULL, normalize INTEGER NOT NULL CHECK (normalize IN (0,1)),
  dim         INTEGER NOT NULL, metric TEXT NOT NULL CHECK (metric IN ('cosine','l2')),
  vec_table   TEXT NOT NULL UNIQUE,         -- e.g. vec_s1
  active      INTEGER NOT NULL DEFAULT 0 CHECK (active IN (0,1)),
  created     TEXT NOT NULL
) STRICT;
CREATE UNIQUE INDEX embedding_space_one_active ON embedding_space(active) WHERE active = 1;

CREATE TABLE vec_target (
  rid               INTEGER PRIMARY KEY,    -- = rowid in the space's vec0 table
  space_id          INTEGER NOT NULL REFERENCES embedding_space(id),
  asset_id          TEXT NOT NULL REFERENCES asset(id) ON DELETE CASCADE,
  representation_id TEXT NOT NULL REFERENCES representation(id) ON DELETE CASCADE,
  segment_id        TEXT REFERENCES segment(id) ON DELETE CASCADE,
  input_hash        TEXT NOT NULL,          -- hash of the exact bytes/frames/text that were embedded
  stale             INTEGER NOT NULL DEFAULT 0 CHECK (stale IN (0,1)),
  created           TEXT NOT NULL
) STRICT;
CREATE UNIQUE INDEX vec_target_unit ON vec_target(space_id, representation_id, ifnull(segment_id, ''));
CREATE INDEX vec_target_asset ON vec_target(asset_id);

-- One vec0 table per embedding space, created when the space is registered (dim/metric from embedding_space).
-- Metadata columns are copies kept in sync by core in the same transaction; they let KNN filter during recall.
CREATE VIRTUAL TABLE vec_s1 USING vec0(
  rid        INTEGER PRIMARY KEY,
  embedding  float[256] distance_metric=cosine,
  kind       TEXT,                          -- asset.kind
  rights     TEXT,                          -- allow | review | deny  (latest asset_rights.decision)
  synthetic  INTEGER,
  is_segment INTEGER,
  min_edge   INTEGER,                       -- min(width, height); 0 for audio
  duration   FLOAT,                          -- insert as REAL (0.0, not 0): vec0 rejects INTEGER for FLOAT columns
  stale      INTEGER
);

-- ───────────────────────── full text ─────────────────────────
-- One row per asset, rebuilt by core whenever title/description/tags/notes/aliases/entities change.
CREATE TABLE search_doc (
  rid         INTEGER PRIMARY KEY,          -- = asset.rid
  title       TEXT NOT NULL DEFAULT '',
  description TEXT NOT NULL DEFAULT '',
  artist      TEXT NOT NULL DEFAULT '',
  tags        TEXT NOT NULL DEFAULT '',
  notes       TEXT NOT NULL DEFAULT '',
  aliases     TEXT NOT NULL DEFAULT '',     -- latin-script aliases + entity labels
  cjk         TEXT NOT NULL DEFAULT ''      -- all CJK text (zh/ja aliases, entity labels, CJK descriptions)
) STRICT;

CREATE VIRTUAL TABLE fts_latin USING fts5(
  title, description, artist, tags, notes, aliases,
  content='search_doc', content_rowid='rid', tokenize='unicode61 remove_diacritics 2'
);
CREATE VIRTUAL TABLE fts_cjk USING fts5(
  cjk, content='search_doc', content_rowid='rid', tokenize='trigram'
);

CREATE TRIGGER search_doc_ai AFTER INSERT ON search_doc BEGIN
  INSERT INTO fts_latin(rowid, title, description, artist, tags, notes, aliases)
    VALUES (new.rid, new.title, new.description, new.artist, new.tags, new.notes, new.aliases);
  INSERT INTO fts_cjk(rowid, cjk) VALUES (new.rid, new.cjk);
END;
CREATE TRIGGER search_doc_ad AFTER DELETE ON search_doc BEGIN
  INSERT INTO fts_latin(fts_latin, rowid, title, description, artist, tags, notes, aliases)
    VALUES ('delete', old.rid, old.title, old.description, old.artist, old.tags, old.notes, old.aliases);
  INSERT INTO fts_cjk(fts_cjk, rowid, cjk) VALUES ('delete', old.rid, old.cjk);
END;
CREATE TRIGGER search_doc_au AFTER UPDATE ON search_doc BEGIN
  INSERT INTO fts_latin(fts_latin, rowid, title, description, artist, tags, notes, aliases)
    VALUES ('delete', old.rid, old.title, old.description, old.artist, old.tags, old.notes, old.aliases);
  INSERT INTO fts_cjk(fts_cjk, rowid, cjk) VALUES ('delete', old.rid, old.cjk);
  INSERT INTO fts_latin(rowid, title, description, artist, tags, notes, aliases)
    VALUES (new.rid, new.title, new.description, new.artist, new.tags, new.notes, new.aliases);
  INSERT INTO fts_cjk(rowid, cjk) VALUES (new.rid, new.cjk);
END;

-- ───────────────────────── selections / shares ─────────────────────────
CREATE TABLE selection (
  id         TEXT PRIMARY KEY CHECK (length(id) = 26),
  created    TEXT NOT NULL,
  created_by TEXT NOT NULL,
  query      TEXT NOT NULL CHECK (json_valid(query)),   -- the exact request incl. filters, space, policy_version
  sheet_sha256 TEXT REFERENCES blob(sha256),
  expires    TEXT
) STRICT;

CREATE TABLE selection_item (
  selection_id      TEXT NOT NULL REFERENCES selection(id) ON DELETE CASCADE,
  idx               INTEGER NOT NULL CHECK (idx >= 1),   -- the number printed on the sheet
  asset_id          TEXT NOT NULL REFERENCES asset(id),
  representation_id TEXT NOT NULL REFERENCES representation(id),
  segment_id        TEXT REFERENCES segment(id),
  explain           TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(explain)),  -- score_type, ranks per channel, matched fields
  PRIMARY KEY (selection_id, idx)
) STRICT;

CREATE TABLE share (
  id           TEXT PRIMARY KEY CHECK (length(id) = 26),
  token_hash   TEXT NOT NULL UNIQUE,        -- sha256 of the URL token; the token itself is never stored
  selection_id TEXT REFERENCES selection(id),
  title TEXT, note TEXT,
  created      TEXT NOT NULL,
  expires      TEXT NOT NULL,
  revoked_at   TEXT
) STRICT;

CREATE TABLE share_item (
  share_id          TEXT NOT NULL REFERENCES share(id) ON DELETE CASCADE,
  pos               INTEGER NOT NULL CHECK (pos >= 1),
  asset_id          TEXT NOT NULL REFERENCES asset(id),
  representation_id TEXT NOT NULL REFERENCES representation(id),
  segment_id        TEXT REFERENCES segment(id),
  clip_t0 REAL, clip_t1 REAL,
  caption           TEXT,
  PRIMARY KEY (share_id, pos)
) STRICT;

CREATE TABLE share_feedback (
  id       INTEGER PRIMARY KEY,
  share_id TEXT NOT NULL REFERENCES share(id) ON DELETE CASCADE,
  pos      INTEGER NOT NULL,
  verdict  TEXT NOT NULL CHECK (verdict IN ('want','reject','clear')),
  comment  TEXT,
  created  TEXT NOT NULL,
  FOREIGN KEY (share_id, pos) REFERENCES share_item(share_id, pos)
) STRICT;
CREATE INDEX share_feedback_share ON share_feedback(share_id, pos, created);

-- ───────────────────────── delivery ─────────────────────────
CREATE TABLE project (
  id              TEXT PRIMARY KEY CHECK (length(id) = 26),
  path            TEXT NOT NULL UNIQUE,     -- relative to /nas
  name            TEXT NOT NULL,
  manifest_format TEXT NOT NULL,            -- exporter name, e.g. assets_json, causcape
  created         TEXT NOT NULL
) STRICT;

CREATE TABLE operation (
  id              TEXT PRIMARY KEY CHECK (length(id) = 26),
  kind            TEXT NOT NULL,            -- fetch | link | share | pull | add | import_project …
  idempotency_key TEXT NOT NULL,
  request_hash    TEXT NOT NULL,            -- same key + different request => error idempotency_conflict
  request         TEXT NOT NULL CHECK (json_valid(request)),
  plan            TEXT CHECK (plan IS NULL OR json_valid(plan)),
  state           TEXT NOT NULL CHECK (state IN ('planned','running','succeeded','failed','rolled_back')),
  result          TEXT CHECK (result IS NULL OR json_valid(result)),
  error           TEXT CHECK (error IS NULL OR json_valid(error)),
  journal_path    TEXT,                     -- <home>/journal/<id>.jsonl
  actor           TEXT NOT NULL,
  created TEXT NOT NULL, started TEXT, finished TEXT,
  UNIQUE (kind, idempotency_key)
) STRICT;
CREATE INDEX operation_unfinished ON operation(state) WHERE state IN ('planned','running');

CREATE TABLE usage (
  id                   TEXT PRIMARY KEY CHECK (length(id) = 26),
  project_id           TEXT NOT NULL REFERENCES project(id),
  operation_id         TEXT NOT NULL REFERENCES operation(id),
  asset_id             TEXT NOT NULL REFERENCES asset(id),
  representation_id    TEXT NOT NULL REFERENCES representation(id),
  segment_id           TEXT REFERENCES segment(id),
  as_name              TEXT NOT NULL,       -- id inside the project's manifest
  clip_requested       TEXT CHECK (clip_requested IS NULL OR json_valid(clip_requested)),  -- [t0, t1]
  clip_actual          TEXT CHECK (clip_actual IS NULL OR json_valid(clip_actual)),
  modifications        TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(modifications)),       -- ["trimmed","color_graded"]
  credit_snapshot      TEXT NOT NULL CHECK (json_valid(credit_snapshot)),
  rights_assessment_id TEXT NOT NULL REFERENCES rights_assessment(id),
  policy_version       INTEGER NOT NULL REFERENCES policy(version),
  delivered_relpath    TEXT NOT NULL,       -- relative to the project
  delivered_sha256     TEXT NOT NULL,
  link_mode            TEXT NOT NULL CHECK (link_mode IN ('reflink','copy','hardlink','manifest_only')),
  origin               TEXT NOT NULL DEFAULT 'link' CHECK (origin IN ('link','import')),  -- import = back-filled from old manifests
  created              TEXT NOT NULL,
  removed_at           TEXT
) STRICT;
CREATE INDEX usage_project ON usage(project_id, created);
CREATE INDEX usage_asset   ON usage(asset_id, created);
CREATE UNIQUE INDEX usage_live_name ON usage(project_id, as_name) WHERE removed_at IS NULL;

-- ───────────────────────── jobs / events ─────────────────────────
CREATE TABLE job (
  id           TEXT PRIMARY KEY CHECK (length(id) = 26),
  kind         TEXT NOT NULL,               -- pull | index | verify | import_project | reembed …
  operation_id TEXT REFERENCES operation(id),
  state        TEXT NOT NULL CHECK (state IN ('queued','running','paused','cancelling','cancelled','succeeded','failed')),
  params       TEXT NOT NULL CHECK (json_valid(params)),
  progress     TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(progress)),
  cursor       TEXT CHECK (cursor IS NULL OR json_valid(cursor)),
  budget_bytes INTEGER, used_bytes INTEGER NOT NULL DEFAULT 0,
  pause_reason TEXT,                        -- load | lockfile | user
  error        TEXT CHECK (error IS NULL OR json_valid(error)),
  created TEXT NOT NULL, started TEXT, finished TEXT, heartbeat TEXT
) STRICT;
CREATE INDEX job_active ON job(state) WHERE state IN ('queued','running','paused','cancelling');

CREATE TABLE pull_seen (
  source         TEXT NOT NULL,
  external_id    TEXT NOT NULL,
  decision       TEXT NOT NULL CHECK (decision IN ('taken','rejected','skipped','deferred')),
  reason         TEXT NOT NULL,             -- reason code, e.g. licence_nc, too_short, map, budget
  policy_version INTEGER NOT NULL REFERENCES policy(version),
  job_id         TEXT REFERENCES job(id),
  evaluated_at   TEXT NOT NULL,
  PRIMARY KEY (source, external_id)
) STRICT;

CREATE TABLE event (
  id      INTEGER PRIMARY KEY AUTOINCREMENT,  -- event_id handed to hooks
  kind    TEXT NOT NULL,                      -- ingested | flagged | linked | share_feedback | pull_done …
  payload TEXT NOT NULL CHECK (json_valid(payload)),
  created TEXT NOT NULL
) STRICT;

CREATE TABLE event_delivery (
  event_id     INTEGER NOT NULL REFERENCES event(id) ON DELETE CASCADE,
  subscriber   TEXT NOT NULL,               -- plugin name
  state        TEXT NOT NULL CHECK (state IN ('pending','delivered','failed','dead')),
  attempts     INTEGER NOT NULL DEFAULT 0,
  next_attempt TEXT,
  last_error   TEXT,
  PRIMARY KEY (event_id, subscriber)
) STRICT;
CREATE INDEX event_delivery_due ON event_delivery(state, next_attempt) WHERE state IN ('pending','failed');
