-- Sheepin AI - verify the demo data in BigQuery.
-- Run from PowerShell:
--   Get-Content scripts\verify.sql -Raw | bq query --use_legacy_sql=false --format=pretty

WITH latest AS (
  SELECT ear_tag, MAX(ts) AS last_ts
  FROM `sheepin-ai.sheepin.weights`
  GROUP BY ear_tag
),
w_now AS (
  SELECT w.ear_tag, w.kg AS kg_now, l.last_ts
  FROM `sheepin-ai.sheepin.weights` w
  JOIN latest l ON w.ear_tag = l.ear_tag AND w.ts = l.last_ts
),
w_6d AS (
  SELECT w.ear_tag, w.kg AS kg_6d_ago
  FROM `sheepin-ai.sheepin.weights` w
  JOIN latest l ON w.ear_tag = l.ear_tag
   AND DATE(w.ts) = DATE_SUB(DATE(l.last_ts), INTERVAL 6 DAY)
),
gate48 AS (
  SELECT g.ear_tag, COUNT(*) AS passes_48h
  FROM `sheepin-ai.sheepin.gate_events` g
  JOIN latest l ON g.ear_tag = l.ear_tag
   AND DATE(g.ts) >= DATE_SUB(DATE(l.last_ts), INTERVAL 1 DAY)
  GROUP BY g.ear_tag
)
SELECT n.ear_tag,
       kg_6d_ago,
       kg_now,
       ROUND(100 * (kg_now - kg_6d_ago) / kg_6d_ago, 1) AS change_pct,
       IFNULL(passes_48h, 0) AS passes_48h
FROM w_now n
JOIN w_6d USING (ear_tag)
LEFT JOIN gate48 USING (ear_tag)
WHERE n.ear_tag IN ('0931', '0266', '0582', '0744', '0417')
ORDER BY change_pct;