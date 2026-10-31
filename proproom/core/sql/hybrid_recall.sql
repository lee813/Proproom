-- Hybrid recall: vector KNN (filters pushed into vec0) + latin FTS + CJK trigram FTS, fused with RRF (k=60).
-- Filters that vec0 cannot express (project usage, licence family …) are applied in every channel; core widens
-- :kv / :kf adaptively until :k results survive or a cap is reached.
WITH
vec_hits AS (
  SELECT rid, distance FROM vec_s1
  WHERE embedding MATCH :qvec AND k = :kv
    AND rights = 'allow' AND synthetic = 0 AND stale = 0 AND min_edge >= :min_edge
),
vec_rank AS (                                   -- collapse segments to their asset, keep best rank
  SELECT t.asset_id, min(r) AS r FROM (
    SELECT rid, row_number() OVER (ORDER BY distance) AS r FROM vec_hits) v
  JOIN vec_target t ON t.rid = v.rid
  GROUP BY t.asset_id
),
fts_rank AS (
  SELECT a.id AS asset_id,
         row_number() OVER (ORDER BY bm25(fts_latin, 10.0, 3.0, 2.0, 2.0, 1.0, 5.0)) AS r
  FROM fts_latin JOIN asset a ON a.rid = fts_latin.rowid
  WHERE fts_latin MATCH :q
  LIMIT :kf
),
cjk_rank AS (
  SELECT a.id AS asset_id, row_number() OVER (ORDER BY bm25(fts_cjk)) AS r
  FROM fts_cjk JOIN asset a ON a.rid = fts_cjk.rowid
  WHERE fts_cjk MATCH :qcjk
  LIMIT :kf
),
fused AS (
  SELECT asset_id, sum(1.0 / (60 + r)) AS rrf,
         json_group_object(ch, r) AS ranks
  FROM (SELECT asset_id, r, 'vec' AS ch FROM vec_rank
        UNION ALL SELECT asset_id, r, 'fts' FROM fts_rank
        UNION ALL SELECT asset_id, r, 'cjk' FROM cjk_rank)
  GROUP BY asset_id
)
SELECT f.asset_id, a.slug, round(f.rrf, 5) AS score, 'rrf' AS score_type, f.ranks
FROM fused f
JOIN asset a ON a.id = f.asset_id AND a.status = 'active'
JOIN asset_rights ar ON ar.asset_id = f.asset_id AND ar.decision = 'allow'
WHERE (ar.basis IS NOT 'quotation' OR :include_quotation = 1)
  AND (:project IS NULL OR NOT EXISTS (
        SELECT 1 FROM usage u WHERE u.asset_id = f.asset_id AND u.project_id = :project AND u.removed_at IS NULL))
ORDER BY f.rrf DESC
LIMIT :k;
