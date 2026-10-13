"""
Sheepin AI - rule-based detection.

Reads per-animal features from BigQuery, applies three deterministic rules and
returns one record per animal. Gemini never decides whether an animal has a
problem; it only explains the alert this module produces.

Rules (all relative to the latest weighing date in the dataset, "today"):
  R1  weight change over 7 days   <= -5%  -> alert (segera)
                                   -3..-5% -> alert (waspada)
  R2  14-day average daily gain   < 60% of TARGET_ADG -> alert (waspada)
  R3  no gate passes today and yesterday (2 calendar days) -> alert (segera)

Run locally (after `gcloud auth application-default login`):
  python -m app.detect            # all animals as JSON
  python -m app.detect 0931       # one animal
"""

import json
import os
import sys
from datetime import date

from google.cloud import bigquery

PROJECT = os.environ.get("GCP_PROJECT", "sheepin-ai")
DATASET = os.environ.get("BQ_DATASET", "sheepin")

TARGET_ADG = 0.12            # kg/day growth target used by R2
DROP_SEGERA_PCT = -5.0       # R1 threshold for "segera"
DROP_WASPADA_PCT = -3.0      # R1 threshold for "waspada"
SLOW_GROWTH_FRACTION = 0.6   # R2: ADG below this fraction of target
GATE_DAYS = 2                # R3: calendar days with zero passes

FEATURES_SQL = """
WITH today AS (
  SELECT MAX(DATE(ts)) AS d FROM `{p}.{d}.weights`
),
w AS (
  SELECT ear_tag, DATE(ts) AS day, kg FROM `{p}.{d}.weights`
),
g AS (
  SELECT ear_tag, DATE(ts) AS day, COUNT(*) AS n
  FROM `{p}.{d}.gate_events` GROUP BY ear_tag, day
)
SELECT
  a.ear_tag, a.sex, a.dob, a.breed, a.farm_id, t.d AS today,
  (SELECT kg FROM w WHERE w.ear_tag = a.ear_tag AND w.day = t.d) AS kg_now,
  (SELECT kg FROM w WHERE w.ear_tag = a.ear_tag
     AND w.day = DATE_SUB(t.d, INTERVAL 6 DAY)) AS kg_6d,
  (SELECT kg FROM w WHERE w.ear_tag = a.ear_tag
     AND w.day = DATE_SUB(t.d, INTERVAL 14 DAY)) AS kg_14d,
  (SELECT IFNULL(SUM(n), 0) FROM g WHERE g.ear_tag = a.ear_tag
     AND g.day >= DATE_SUB(t.d, INTERVAL {gate_days_minus_1} DAY)) AS passes_recent,
  (SELECT IFNULL(SUM(n), 0) / 14 FROM g WHERE g.ear_tag = a.ear_tag
     AND g.day BETWEEN DATE_SUB(t.d, INTERVAL 15 DAY)
                   AND DATE_SUB(t.d, INTERVAL 2 DAY)) AS avg_passes_per_day
FROM `{p}.{d}.animals` a
CROSS JOIN today t
ORDER BY a.ear_tag
"""


def fetch_features(client: bigquery.Client | None = None) -> list[dict]:
    client = client or bigquery.Client(project=PROJECT)
    sql = FEATURES_SQL.format(p=PROJECT, d=DATASET, gate_days_minus_1=GATE_DAYS - 1)
    return [dict(row) for row in client.query(sql).result()]


def evaluate(f: dict) -> dict:
    """Apply the three rules to one feature row and build the animal record."""
    reasons: list[str] = []
    level = "normal"

    kg_now, kg_6d, kg_14d = f.get("kg_now"), f.get("kg_6d"), f.get("kg_14d")

    # R1 - weight change over 7 days
    change_pct = None
    if kg_now is not None and kg_6d:
        change_pct = round(100 * (kg_now - kg_6d) / kg_6d, 1)
        if change_pct <= DROP_SEGERA_PCT:
            reasons.append("bobot turun lebih dari 5% dalam 7 hari")
            level = "segera"
        elif change_pct <= DROP_WASPADA_PCT:
            reasons.append("bobot turun 3-5% dalam 7 hari")
            level = max_level(level, "waspada")

    # R2 - slow growth over 14 days
    adg_14d = None
    if kg_now is not None and kg_14d is not None:
        adg_14d = round((kg_now - kg_14d) / 14, 3)
        if adg_14d < SLOW_GROWTH_FRACTION * TARGET_ADG and change_pct is not None \
                and change_pct > DROP_WASPADA_PCT:
            # only report slow growth when R1 has not already fired on a drop
            reasons.append("pertumbuhan 14 hari di bawah 60% target")
            level = max_level(level, "waspada")

    # R3 - no gate passes for GATE_DAYS calendar days
    passes_recent = int(f.get("passes_recent") or 0)
    if passes_recent == 0:
        reasons.append(f"tidak lewat gerbang {GATE_DAYS * 24} jam")
        level = "segera"

    today = f["today"]
    age_months = int((today - f["dob"]).days / 30.4) if f.get("dob") else None

    return {
        "ear_tag": f["ear_tag"],
        "jenis_kelamin": f.get("sex"),
        "umur_bulan": age_months,
        "ras": f.get("breed"),
        "farm_id": f.get("farm_id"),
        "tanggal_data": today.isoformat() if isinstance(today, date) else str(today),
        "bobot_6_hari_lalu_kg": kg_6d,
        "bobot_hari_ini_kg": kg_now,
        "perubahan_persen": change_pct,
        "pertumbuhan_14_hari_kg_per_hari": adg_14d,
        "target_pertumbuhan_per_hari_kg": TARGET_ADG,
        "lewat_gerbang_48_jam_terakhir": passes_recent,
        "rata_rata_lewat_gerbang_per_hari": round(float(f.get("avg_passes_per_day") or 0), 1),
        "alasan_alert": reasons,
        "tingkat_sistem": level,
    }


LEVEL_ORDER = {"normal": 0, "waspada": 1, "segera": 2}


def max_level(a: str, b: str) -> str:
    return a if LEVEL_ORDER[a] >= LEVEL_ORDER[b] else b


GEMINI_FIELDS = [
    "ear_tag", "jenis_kelamin", "umur_bulan",
    "bobot_6_hari_lalu_kg", "bobot_hari_ini_kg", "perubahan_persen",
    "target_pertumbuhan_per_hari_kg",
    "lewat_gerbang_48_jam_terakhir", "rata_rata_lewat_gerbang_per_hari",
    "alasan_alert",
]


def to_gemini_payload(record: dict) -> dict:
    """Exactly the fields the validated prompt was tested with, nothing else."""
    return {k: record[k] for k in GEMINI_FIELDS if record.get(k) is not None}


def run(client: bigquery.Client | None = None) -> list[dict]:
    records = [evaluate(f) for f in fetch_features(client)]
    # flagged animals first, worst first
    records.sort(key=lambda r: (-LEVEL_ORDER[r["tingkat_sistem"]], r["ear_tag"]))
    return records


if __name__ == "__main__":
    out = run()
    if len(sys.argv) > 1:
        out = [r for r in out if r["ear_tag"] == sys.argv[1]]
    print(json.dumps(out, ensure_ascii=False, indent=2, default=str))