-- Keep failed attempts retryable across independent pull jobs.
CREATE TABLE pull_seen_new (
  source TEXT NOT NULL,
  external_id TEXT NOT NULL,
  decision TEXT NOT NULL CHECK (decision IN ('taken','rejected','skipped','deferred','error')),
  reason TEXT NOT NULL,
  policy_version INTEGER NOT NULL REFERENCES policy(version),
  job_id TEXT REFERENCES job(id),
  evaluated_at TEXT NOT NULL,
  PRIMARY KEY (source, external_id)
) STRICT;
INSERT INTO pull_seen_new SELECT source, external_id, decision, reason, policy_version, job_id, evaluated_at FROM pull_seen;
DROP TABLE pull_seen;
ALTER TABLE pull_seen_new RENAME TO pull_seen;
