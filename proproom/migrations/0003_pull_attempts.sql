-- Count consecutive transient errors without losing earlier pull decisions.
ALTER TABLE pull_seen ADD COLUMN error_attempts INTEGER NOT NULL DEFAULT 0 CHECK (error_attempts >= 0);
ALTER TABLE pull_seen ADD COLUMN last_error TEXT;
UPDATE pull_seen SET error_attempts = 1, last_error = reason WHERE decision = 'error';
