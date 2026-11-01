-- One row per (representation, analyzer/segmenter plugin, plugin version): the worker's analysis backlog is every
-- pair without a `done` row. A failure is retried with backoff until `attempts` reaches the limit in core/analysis.py.
CREATE TABLE analysis_run (
  representation_id TEXT NOT NULL REFERENCES representation(id) ON DELETE CASCADE,
  plugin            TEXT NOT NULL,
  plugin_version    TEXT NOT NULL,
  state             TEXT NOT NULL CHECK (state IN ('done','failed')),
  attempts          INTEGER NOT NULL DEFAULT 1 CHECK (attempts >= 1),
  error             TEXT CHECK (error IS NULL OR json_valid(error)),
  next_attempt      TEXT,
  updated           TEXT NOT NULL,
  PRIMARY KEY (representation_id, plugin, plugin_version)
) STRICT;
CREATE INDEX analysis_run_failed ON analysis_run(state, next_attempt) WHERE state = 'failed';
