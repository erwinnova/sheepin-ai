# Sheepin AI - create the BigQuery dataset, load the demo CSVs, verify.
# Run from the repo root in PowerShell, after: python scripts/generate_demo_data.py
#
#   .\scripts\load_bigquery.ps1

$PROJECT  = "sheepin-ai"
$DATASET  = "sheepin"
$LOCATION = "asia-southeast2"

# 1. Dataset (ignore the error if it already exists)
bq --project_id=$PROJECT mk --dataset --location=$LOCATION "$PROJECT`:$DATASET"

# 2. Load the three tables. --replace makes the script safe to re-run.
bq --project_id=$PROJECT load --replace --source_format=CSV --skip_leading_rows=1 `
  "$DATASET.animals" data/animals.csv `
  "ear_tag:STRING,sex:STRING,dob:DATE,breed:STRING,farm_id:STRING"

bq --project_id=$PROJECT load --replace --source_format=CSV --skip_leading_rows=1 `
  "$DATASET.weights" data/weights.csv `
  "ear_tag:STRING,ts:TIMESTAMP,kg:FLOAT64"

bq --project_id=$PROJECT load --replace --source_format=CSV --skip_leading_rows=1 `
  "$DATASET.gate_events" data/gate_events.csv `
  "ear_tag:STRING,ts:TIMESTAMP,direction:STRING"

# 3. Verify: row counts, then the five patterned animals.
bq --project_id=$PROJECT query --use_legacy_sql=false --format=pretty `
"SELECT 'animals' AS tbl, COUNT(*) AS rows FROM ``$PROJECT.$DATASET.animals``
 UNION ALL SELECT 'weights', COUNT(*) FROM ``$PROJECT.$DATASET.weights``
 UNION ALL SELECT 'gate_events', COUNT(*) FROM ``$PROJECT.$DATASET.gate_events``"

bq --project_id=$PROJECT query --use_legacy_sql=false --format=pretty `
"WITH latest AS (
   SELECT ear_tag, MAX(ts) AS last_ts FROM ``$PROJECT.$DATASET.weights`` GROUP BY ear_tag
 ),
 w_now AS (
   SELECT w.ear_tag, w.kg AS kg_now, l.last_ts
   FROM ``$PROJECT.$DATASET.weights`` w JOIN latest l
     ON w.ear_tag = l.ear_tag AND w.ts = l.last_ts
 ),
 w_6d AS (
   SELECT w.ear_tag, w.kg AS kg_6d_ago
   FROM ``$PROJECT.$DATASET.weights`` w JOIN latest l
     ON w.ear_tag = l.ear_tag
    AND DATE(w.ts) = DATE_SUB(DATE(l.last_ts), INTERVAL 6 DAY)
 ),
 gate48 AS (
   SELECT g.ear_tag, COUNT(*) AS passes_48h
   FROM ``$PROJECT.$DATASET.gate_events`` g JOIN latest l
     ON g.ear_tag = l.ear_tag
    AND g.ts >= TIMESTAMP_SUB(l.last_ts, INTERVAL 48 HOUR)
   GROUP BY g.ear_tag
 )
 SELECT n.ear_tag, kg_6d_ago, kg_now,
        ROUND(100 * (kg_now - kg_6d_ago) / kg_6d_ago, 1) AS change_pct,
        IFNULL(passes_48h, 0) AS passes_48h
 FROM w_now n JOIN w_6d USING (ear_tag) LEFT JOIN gate48 USING (ear_tag)
 WHERE n.ear_tag IN ('0931','0266','0582','0744','0417')
 ORDER BY change_pct"