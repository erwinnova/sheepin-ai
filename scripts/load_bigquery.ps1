# Sheepin AI - create the BigQuery dataset, load the demo CSVs, verify.
# Run from the repo root in PowerShell, after: python scripts/generate_demo_data.py
#
#   .\scripts\load_bigquery.ps1

$PROJECT  = "sheepin-ai"
$DATASET  = "sheepin"
$LOCATION = "asia-southeast2"

# 1. Dataset (skipped if it already exists)
if (-not (bq --project_id=$PROJECT ls -d | Select-String -Quiet "^\s*$DATASET\s*$")) {
  bq --project_id=$PROJECT mk --dataset --location=$LOCATION "$PROJECT`:$DATASET"
}

# 2. Load the three tables. --replace makes the script safe to re-run.
bq --project_id=$PROJECT load --replace --source_format=CSV --skip_leading_rows=1 `
  "$DATASET.animals" data/animals.csv `
  "ear_tag:STRING,sex:STRING,dob:DATE,breed:STRING,farm_id:STRING,kandang:STRING,target_kg:FLOAT64"

bq --project_id=$PROJECT load --replace --source_format=CSV --skip_leading_rows=1 `
  "$DATASET.weights" data/weights.csv `
  "ear_tag:STRING,ts:TIMESTAMP,kg:FLOAT64"

bq --project_id=$PROJECT load --replace --source_format=CSV --skip_leading_rows=1 `
  "$DATASET.gate_events" data/gate_events.csv `
  "ear_tag:STRING,ts:TIMESTAMP,direction:STRING"

bq --project_id=$PROJECT load --replace --source_format=CSV --skip_leading_rows=1 `
  "$DATASET.feed_logs" data/feed_logs.csv `
  "tanggal:DATE,farm_id:STRING,kandang:STRING,ear_tag:STRING,jenis_pakan:STRING,kg_segar:FLOAT64,harga_per_kg_rp:FLOAT64"

# 3. Verify. Multi-line SQL must go through a file and a pipe on Windows
#    (bq.cmd truncates arguments at line breaks).
bq --project_id=$PROJECT query --use_legacy_sql=false --format=pretty "SELECT 'animals' AS tbl, COUNT(*) AS n_rows FROM $DATASET.animals UNION ALL SELECT 'weights', COUNT(*) FROM $DATASET.weights UNION ALL SELECT 'gate_events', COUNT(*) FROM $DATASET.gate_events UNION ALL SELECT 'feed_logs', COUNT(*) FROM $DATASET.feed_logs"
Get-Content (Join-Path $PSScriptRoot "verify.sql") -Raw | bq --project_id=$PROJECT query --use_legacy_sql=false --format=pretty