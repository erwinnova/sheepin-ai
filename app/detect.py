"""
Sheepin AI - rule-based detection.

Reads per-animal features from BigQuery, applies three deterministic rules and
returns one record per animal. Gemini never decides whether an animal has a
problem; it only explains the alert this module produces.

Rules (all relative to the latest weighing date in the dataset, "today"):
  R1  weight change over 7 days   <= -5%  -> alert (segera)
                                   -3..-5% -> alert (waspada)
  R2  14-day growth slope (least-squares over all daily readings)
                                   < 60% of TARGET_ADG -> alert (waspada)
  R3  no gate passes today and yesterday (2 calendar days) -> alert (segera)
  R4  14-day feed conversion ratio (kg dry matter per kg gain) > 10 -> alert (waspada)
      Feed comes from the farmer's own logs: per animal where recorded, otherwise
      allocated from the pen's log by body-weight share and marked as an estimate.

Run locally (after `gcloud auth application-default login`):
  python -m app.detect            # all animals as JSON
  python -m app.detect 0931       # one animal
"""

import json
import os
import sys
from datetime import date, timedelta

from google.cloud import bigquery

PROJECT = os.environ.get("GCP_PROJECT", "sheepin-ai")
DATASET = os.environ.get("BQ_DATASET", "sheepin")

TARGET_ADG = 0.12            # kg/day growth target used by R2
DROP_SEGERA_PCT = -5.0       # R1 threshold for "segera"
DROP_WASPADA_PCT = -3.0      # R1 threshold for "waspada"
SLOW_GROWTH_FRACTION = 0.6   # R2: ADG below this fraction of target
GATE_DAYS = 2                # R3: calendar days with zero passes

FCR_WASPADA = 10.0           # R4 threshold (dry-matter basis); local sheep run 6-10
FCR_WINDOW_DAYS = 14
MIN_GAIN_FOR_FCR_KG = 0.3    # below this, a ratio would be meaningless

# Dry-matter fraction of each feed type as the farmer records it (fresh kg).
FEED_DM = {"hijauan": 0.25, "konsentrat": 0.88, "silase": 0.35, "ampas": 0.20}
FEED_DM_DEFAULT = 0.30

# Ideal daily dry-matter intake for a growing sheep, as a fraction of body weight
# (rule of thumb 2.5-3%; 2.5% here), and the ration used to express it as fresh feed.
IDEAL_DM_PCT_BW = 0.025
RATION_DM_SHARE = {"hijauan": 0.65, "konsentrat": 0.35}
FEED_PRICE_FRESH = {"hijauan": 1_500, "konsentrat": 5_000}   # IDR per fresh kg, Oct 2026
DAYS_PER_MONTH = 30

# Live-weight market prices (IDR per kg), Jakarta area, October 2026.
PRICE_PER_KG = {"lokal": 90_000, "impor": 110_000}
IMPORTED_BREEDS = {"dorper", "merino", "suffolk", "texel"}  # none in the demo data

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
  a.ear_tag, a.sex, a.dob, a.breed, a.farm_id, a.kandang, a.target_kg, t.d AS today,
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


HISTORY_DAYS = 30

HISTORY_SQL = """
WITH today AS (
  SELECT MAX(DATE(ts)) AS d FROM `{p}.{d}.weights`
)
SELECT w.ear_tag, DATE(w.ts) AS day, w.kg
FROM `{p}.{d}.weights` w, today t
WHERE DATE(w.ts) > DATE_SUB(t.d, INTERVAL {days} DAY)
ORDER BY w.ear_tag, day
"""


FEED_SQL = """
WITH today AS (
  SELECT MAX(DATE(ts)) AS d FROM `{p}.{d}.weights`
)
SELECT f.farm_id, f.kandang, f.ear_tag, f.jenis_pakan,
       SUM(f.kg_segar) AS kg_segar,
       SUM(f.kg_segar * f.harga_per_kg_rp) AS biaya_rp
FROM `{p}.{d}.feed_logs` f, today t
WHERE f.tanggal > DATE_SUB(t.d, INTERVAL {days} DAY)
GROUP BY f.farm_id, f.kandang, f.ear_tag, f.jenis_pakan
"""


def fetch_features(client: bigquery.Client | None = None) -> list[dict]:
    """One feature row per animal, with weight history and feed intake attached."""
    client = client or bigquery.Client(project=PROJECT)
    sql = FEATURES_SQL.format(p=PROJECT, d=DATASET, gate_days_minus_1=GATE_DAYS - 1)
    rows = [dict(row) for row in client.query(sql).result()]

    feed_sql = FEED_SQL.format(p=PROJECT, d=DATASET, days=FCR_WINDOW_DAYS)
    try:
        feed_rows = [dict(r) for r in client.query(feed_sql).result()]
    except Exception:          # feed_logs table absent: feed features stay empty
        feed_rows = []
    attach_feed(rows, feed_rows)

    hist_sql = HISTORY_SQL.format(p=PROJECT, d=DATASET, days=HISTORY_DAYS)
    history: dict[str, list] = {}
    for h in client.query(hist_sql).result():
        history.setdefault(h["ear_tag"], []).append(
            {"tanggal": h["day"].isoformat(), "kg": h["kg"]})
    for r in rows:
        r["history"] = history.get(r["ear_tag"], [])
    return rows


def attach_feed(rows: list[dict], feed_rows: list[dict]) -> None:
    """Give every animal row a `feed` dict: its own logged intake, or its share
    of the pen's logged intake, over the FCR window. Dry matter from FEED_DM."""
    own: dict[str, dict] = {}
    pen: dict[tuple, dict] = {}
    for r in feed_rows:
        dm = float(r["kg_segar"] or 0) * FEED_DM.get((r["jenis_pakan"] or "").lower(), FEED_DM_DEFAULT)
        cost = float(r["biaya_rp"] or 0)
        bucket = own.setdefault(r["ear_tag"], {"dm_kg": 0.0, "biaya_rp": 0.0}) if r.get("ear_tag") \
            else pen.setdefault((r["farm_id"], r["kandang"]), {"dm_kg": 0.0, "biaya_rp": 0.0})
        bucket["dm_kg"] += dm
        bucket["biaya_rp"] += cost

    pen_weight: dict[tuple, float] = {}
    for r in rows:
        if r.get("kg_now"):
            k = (r.get("farm_id"), r.get("kandang"))
            pen_weight[k] = pen_weight.get(k, 0.0) + r["kg_now"]

    for r in rows:
        k = (r.get("farm_id"), r.get("kandang"))
        if r["ear_tag"] in own:
            r["feed"] = {"mode": "ternak", **own[r["ear_tag"]]}
        elif k in pen and r.get("kg_now") and pen_weight.get(k):
            share = r["kg_now"] / pen_weight[k]
            r["feed"] = {"mode": "kandang", "dm_kg": pen[k]["dm_kg"] * share,
                         "biaya_rp": pen[k]["biaya_rp"] * share}
        else:
            r["feed"] = None


def slope_per_day(points: list[dict]) -> float | None:
    """Least-squares growth rate (kg/day) over daily readings; None if too few."""
    if len(points) < 5:
        return None
    xs = list(range(len(points)))
    ys = [p["kg"] for p in points]
    mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
    sxx = sum((x - mx) ** 2 for x in xs)
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    return round(sxy / sxx, 3) if sxx else None


def evaluate(f: dict) -> dict:
    """Apply the three rules to one feature row and build the animal record."""
    reasons: list[str] = []
    codes: list[str] = []
    level = "normal"

    kg_now, kg_6d, kg_14d = f.get("kg_now"), f.get("kg_6d"), f.get("kg_14d")

    # R1 - weight change over 7 days
    change_pct = None
    if kg_now is not None and kg_6d:
        change_pct = round(100 * (kg_now - kg_6d) / kg_6d, 1)
        if change_pct <= DROP_SEGERA_PCT:
            reasons.append("bobot turun lebih dari 5% dalam 7 hari")
            codes.append("drop_5")
            level = "segera"
        elif change_pct <= DROP_WASPADA_PCT:
            reasons.append("bobot turun 3-5% dalam 7 hari")
            codes.append("drop_3")
            level = max_level(level, "waspada")

    # R2 - slow growth over 14 days (regression slope over the last 15 readings,
    # which is far less sensitive to a single noisy scale reading than two points)
    history = f.get("history") or []
    adg_14d = slope_per_day(history[-15:])
    if adg_14d is not None:
        if adg_14d < SLOW_GROWTH_FRACTION * TARGET_ADG and change_pct is not None \
                and change_pct > DROP_WASPADA_PCT:
            # only report slow growth when R1 has not already fired on a drop
            reasons.append("pertumbuhan 14 hari di bawah 60% target")
            codes.append("slow_growth")
            level = max_level(level, "waspada")

    # R3 - no gate passes for GATE_DAYS calendar days
    passes_recent = int(f.get("passes_recent") or 0)
    if passes_recent == 0:
        reasons.append(f"tidak lewat gerbang {GATE_DAYS * 24} jam")
        codes.append("no_gate")
        level = "segera"

    # R4 - feed conversion over the window, from the farmer's feed logs
    feed = feed_efficiency(f.get("feed"), adg_14d, kg_now)
    if feed and feed["fcr"] is not None and feed["fcr"] > FCR_WASPADA \
            and "slow_growth" not in codes:
        reasons.append(f"konversi pakan 14 hari di atas {FCR_WASPADA:g}")
        codes.append("fcr_high")
        level = max_level(level, "waspada")

    today = f["today"]
    age_months = int((today - f["dob"]).days / 30.4) if f.get("dob") else None
    growth = project_growth(f, kg_now, kg_6d, adg_14d, today)
    savings = potential_savings(feed, feed_requirement(kg_now), adg_14d, level, growth)

    return {
        "ear_tag": f["ear_tag"],
        "jenis_kelamin": f.get("sex"),
        "umur_bulan": age_months,
        "ras": f.get("breed"),
        "farm_id": f.get("farm_id"),
        "kandang": f.get("kandang"),
        "tanggal_data": today.isoformat() if isinstance(today, date) else str(today),
        "bobot_6_hari_lalu_kg": kg_6d,
        "bobot_hari_ini_kg": kg_now,
        "perubahan_persen": change_pct,
        "pertumbuhan_14_hari_kg_per_hari": adg_14d,
        "target_pertumbuhan_per_hari_kg": TARGET_ADG,
        "lewat_gerbang_48_jam_terakhir": passes_recent,
        "rata_rata_lewat_gerbang_per_hari": round(float(f.get("avg_passes_per_day") or 0), 1),
        "alasan_alert": reasons,
        "alasan_kode": codes,
        "tingkat_sistem": level,
        "pertumbuhan": growth,
        "kebutuhan_pakan": feed_requirement(kg_now),
        "pakan": feed,
        "potensi_hemat": savings,
        "riwayat_bobot": history,
    }


def feed_requirement(kg_now: float | None) -> dict | None:
    """Ideal daily intake for this animal today, from the rule of thumb above,
    expressed both as dry matter and as fresh feed under the standard ration."""
    if not kg_now:
        return None
    dm = IDEAL_DM_PCT_BW * kg_now
    return {
        "persen_bobot_badan": IDEAL_DM_PCT_BW * 100,
        "bk_per_hari_kg": round(dm, 2),
        "segar_per_hari_kg": {name: round(dm * share / FEED_DM[name], 2)
                              for name, share in RATION_DM_SHARE.items()},
    }


def feed_efficiency(feed: dict | None, adg: float | None, kg_now: float | None = None) -> dict | None:
    """FCR and feed cost per kg of gain over the window. None values mean
    'cannot be computed', never a guess."""
    if not feed:
        return None
    gain = round(adg * FCR_WINDOW_DAYS, 2) if adg is not None else None
    ok = gain is not None and gain >= MIN_GAIN_FOR_FCR_KG
    avg_dm = feed["dm_kg"] / FCR_WINDOW_DAYS
    need = IDEAL_DM_PCT_BW * kg_now if kg_now else None
    return {
        "rata_rata_bk_per_hari_kg": round(avg_dm, 2),
        "persen_dari_kebutuhan": round(100 * avg_dm / need) if need else None,
        "mode": feed["mode"],                       # "ternak" (own log) or "kandang" (pen estimate)
        "kandang": None,                            # filled by pen_summary
        "pakan_bk_14_hari_kg": round(feed["dm_kg"], 1),
        "biaya_pakan_14_hari_rp": round(feed["biaya_rp"]),
        "kenaikan_14_hari_kg": gain,
        "fcr": round(feed["dm_kg"] / gain, 1) if ok else None,
        "biaya_per_kg_kenaikan_rp": round(feed["biaya_rp"] / gain) if ok else None,
    }


def default_price_per_kg_dm() -> float:
    """Cost of one kg of dry matter under the standard ration and list prices."""
    return sum(share / FEED_DM[name] * FEED_PRICE_FRESH[name]
               for name, share in RATION_DM_SHARE.items())


def potential_savings(feed: dict | None, need: dict | None, adg: float | None,
                      level: str, growth: dict) -> dict:
    """Monthly rupiah value of feeding this animal correctly, in two parts:
    feed given above requirement (waste) and growth below target (lost value,
    net of the extra feed it would take). 'Potential' only: computed from the
    requirement rule and current prices, not yet verified by the scale."""
    out = {
        "status": "potensi",
        "hemat_pakan_rp_per_bulan": None,
        "nilai_pertumbuhan_rp_per_bulan": None,
        "tambahan_pakan_rp_per_bulan": None,
        "manfaat_bersih_rp_per_bulan": None,
        "butuh_jatah_pakan": feed is None,
        "alasan_tidak_dihitung": None,
    }
    if level == "segera" or adg is None or adg < 0:
        out["alasan_tidak_dihitung"] = "sakit_atau_bobot_turun"
        return out
    if not need:
        out["alasan_tidak_dihitung"] = "tanpa_bobot"
        return out

    need_dm = need["bk_per_hari_kg"]
    price_dm = (feed["biaya_pakan_14_hari_rp"] / feed["pakan_bk_14_hari_kg"]
                if feed and feed["pakan_bk_14_hari_kg"] else default_price_per_kg_dm())
    sale_price = growth["harga_per_kg_rp"]

    # 1. feed above requirement -> waste that can be saved
    if feed:
        excess = feed["rata_rata_bk_per_hari_kg"] - need_dm
        out["hemat_pakan_rp_per_bulan"] = round(max(excess, 0) * price_dm * DAYS_PER_MONTH)

    # 2. growth below target -> live weight not gained, minus the feed it would cost
    gap = TARGET_ADG - adg
    if gap > 0.005:
        out["nilai_pertumbuhan_rp_per_bulan"] = round(gap * DAYS_PER_MONTH * sale_price)
        shortfall = (need_dm - feed["rata_rata_bk_per_hari_kg"]) if feed else 0.0
        out["tambahan_pakan_rp_per_bulan"] = round(max(shortfall, 0) * price_dm * DAYS_PER_MONTH)
    else:
        out["nilai_pertumbuhan_rp_per_bulan"] = 0
        out["tambahan_pakan_rp_per_bulan"] = 0

    out["manfaat_bersih_rp_per_bulan"] = (
        (out["hemat_pakan_rp_per_bulan"] or 0)
        + (out["nilai_pertumbuhan_rp_per_bulan"] or 0)
        - (out["tambahan_pakan_rp_per_bulan"] or 0))
    return out


def pen_summary(records: list[dict]) -> dict:
    """Pen-level FCR and cost per kg gain, from the same records; attached to
    each animal so the farmer can compare the animal with its pen."""
    pens: dict[tuple, dict] = {}
    for r in records:
        p = r.get("pakan")
        if not p:
            continue
        k = (r["farm_id"], r.get("kandang"))
        b = pens.setdefault(k, {"farm_id": r["farm_id"], "kandang": r.get("kandang"),
                                "ekor": 0, "pakan_bk_14_hari_kg": 0.0,
                                "biaya_pakan_14_hari_rp": 0.0, "kenaikan_14_hari_kg": 0.0})
        b["ekor"] += 1
        b["pakan_bk_14_hari_kg"] += p["pakan_bk_14_hari_kg"]
        b["biaya_pakan_14_hari_rp"] += p["biaya_pakan_14_hari_rp"]
        b["kenaikan_14_hari_kg"] += p["kenaikan_14_hari_kg"] or 0
    for b in pens.values():
        g = b["kenaikan_14_hari_kg"]
        ok = g >= MIN_GAIN_FOR_FCR_KG
        b["fcr"] = round(b["pakan_bk_14_hari_kg"] / g, 1) if ok else None
        b["biaya_per_kg_kenaikan_rp"] = round(b["biaya_pakan_14_hari_rp"] / g) if ok else None
        for key in ("pakan_bk_14_hari_kg", "kenaikan_14_hari_kg"):
            b[key] = round(b[key], 1)
        b["biaya_pakan_14_hari_rp"] = round(b["biaya_pakan_14_hari_rp"])
    for r in records:
        if r.get("pakan"):
            r["pakan"]["kandang"] = pens.get((r["farm_id"], r.get("kandang")))
    return pens


def price_per_kg(breed: str | None) -> int:
    kind = "impor" if (breed or "").lower().split()[0:1] and \
        (breed or "").lower().split()[0] in IMPORTED_BREEDS else "lokal"
    return PRICE_PER_KG[kind]


def project_growth(f: dict, kg_now, kg_6d, adg, today: date) -> dict:
    """Value of the animal today and the road to the farmer's own target weight.

    Everything here is arithmetic on recorded weights and the farmer's target;
    nothing is estimated by the model. days_to_target is None when the animal
    is not gaining, which the UI shows as 'cannot be estimated'.
    """
    price = price_per_kg(f.get("breed"))
    target = f.get("target_kg")
    out = {
        "harga_per_kg_rp": price,
        "nilai_hari_ini_rp": round(kg_now * price) if kg_now is not None else None,
        "selisih_nilai_7_hari_rp": round((kg_now - kg_6d) * price)
        if kg_now is not None and kg_6d is not None else None,
        "bobot_target_kg": target,
        "sisa_ke_target_kg": round(target - kg_now, 1)
        if target is not None and kg_now is not None else None,
        "nilai_di_target_rp": round(target * price) if target is not None else None,
        "perkiraan_hari_ke_target": None,
        "perkiraan_tanggal_target": None,
        "target_tercapai": bool(target is not None and kg_now is not None and kg_now >= target),
    }
    if out["sisa_ke_target_kg"] is not None and out["sisa_ke_target_kg"] > 0 \
            and adg is not None and adg > 0.01:
        days = int(round(out["sisa_ke_target_kg"] / adg))
        out["perkiraan_hari_ke_target"] = days
        out["perkiraan_tanggal_target"] = (today + timedelta(days=days)).isoformat()
    return out


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
    pen_summary(records)
    # flagged animals first, worst first
    records.sort(key=lambda r: (-LEVEL_ORDER[r["tingkat_sistem"]], r["ear_tag"]))
    return records


if __name__ == "__main__":
    out = run()
    if len(sys.argv) > 1:
        out = [r for r in out if r["ear_tag"] == sys.argv[1]]
    print(json.dumps(out, ensure_ascii=False, indent=2, default=str))