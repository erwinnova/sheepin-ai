"""
Sheepin AI - synthetic demo data generator.

Produces three CSV files in ./data that match the BigQuery schema:
  animals.csv      ear_tag, sex, dob, breed, farm_id
  weights.csv      ear_tag, ts, kg
  gate_events.csv  ear_tag, ts, direction

40 animals, 60 days of daily weighing (Smart Scale) and gate passes (auto gate).
Four animals carry deliberate patterns that mirror the Gemini test cases:
  0931  moderate drop  (-6.5% in 6 days) + no gate passes for 48 h   -> "segera"
  0266  severe drop    (-12%  in 6 days) + no gate passes for 48 h   -> "segera"
  0582  mild drop      (-3.2% in 6 days), gate activity normal        -> "waspada"
  0744  slow growth    (~35% of target ADG for the last 14 days)      -> "waspada"
  0417  healthy reference animal, above target                        -> "normal"

Usage:
  python scripts/generate_demo_data.py            # ends today
  python scripts/generate_demo_data.py 2026-10-18 # ends on a chosen date
"""

import csv
import os
import random
import sys
from datetime import date, datetime, timedelta

SEED = 42
N_ANIMALS = 40
N_DAYS = 60
OUT_DIR = "data"

TARGET_ADG = 0.12          # kg/day, the growth target used by the detection rules
GATE_PASSES_PER_DAY = 4    # average passes through the auto gate per animal

BREEDS = ["Garut", "Ekor Tipis", "Ekor Gemuk", "Garut", "Ekor Tipis"]
FARMS = ["F001", "F001", "F001", "F002"]

# ear_tag -> (sex, age_months, start_weight_kg, pattern)
SPECIAL = {
    "0931": ("betina", 14, 32.0, "drop_moderate_no_gate"),
    "0266": ("betina", 11, 31.0, "drop_severe_no_gate"),
    "0582": ("betina", 18, 30.5, "drop_mild"),
    "0744": ("jantan", 10, 29.0, "slow_growth"),
    "0417": ("jantan", 9, 29.5, "healthy_above_target"),
}


def main() -> None:
    random.seed(SEED)
    end_day = date.fromisoformat(sys.argv[1]) if len(sys.argv) > 1 else date.today()
    start_day = end_day - timedelta(days=N_DAYS - 1)
    os.makedirs(OUT_DIR, exist_ok=True)

    animals = build_animals(end_day)
    weights, gate_events = [], []
    for a in animals:
        w, g = simulate_animal(a, start_day)
        weights.extend(w)
        gate_events.extend(g)

    write_csv("animals.csv", ["ear_tag", "sex", "dob", "breed", "farm_id"],
              [[a["ear_tag"], a["sex"], a["dob"], a["breed"], a["farm_id"]] for a in animals])
    write_csv("weights.csv", ["ear_tag", "ts", "kg"], weights)
    write_csv("gate_events.csv", ["ear_tag", "ts", "direction"], gate_events)

    print(f"Period: {start_day} -> {end_day} ({N_DAYS} days)")
    print(f"animals.csv      {len(animals):>6} rows")
    print(f"weights.csv      {len(weights):>6} rows")
    print(f"gate_events.csv  {len(gate_events):>6} rows")
    print("Patterned animals: " + ", ".join(f"{t} ({p[3]})" for t, p in SPECIAL.items()))


def build_animals(end_day: date) -> list[dict]:
    animals = []
    used = set(SPECIAL)
    for tag, (sex, age_months, start_kg, pattern) in SPECIAL.items():
        animals.append(make_animal(tag, sex, age_months, start_kg, pattern, end_day))
    while len(animals) < N_ANIMALS:
        tag = f"{random.randint(1, 999):04d}"
        if tag in used:
            continue
        used.add(tag)
        sex = random.choice(["jantan", "betina"])
        age_months = random.randint(6, 24)
        start_kg = round(random.uniform(22.0, 34.0) + (age_months - 6) * 0.3, 1)
        animals.append(make_animal(tag, sex, age_months, start_kg, "healthy", end_day))
    animals.sort(key=lambda a: a["ear_tag"])
    return animals


def make_animal(tag, sex, age_months, start_kg, pattern, end_day) -> dict:
    dob = end_day - timedelta(days=int(age_months * 30.4))
    return {
        "ear_tag": tag,
        "sex": sex,
        "dob": dob.isoformat(),
        "breed": random.choice(BREEDS),
        "farm_id": random.choice(FARMS),
        "start_kg": start_kg,
        "pattern": pattern,
    }


def simulate_animal(a: dict, start_day: date):
    """Daily weight and gate passes for one animal over N_DAYS."""
    pattern = a["pattern"]
    # Each animal has its own normal ADG around the target.
    base_adg = random.uniform(0.10, 0.15)
    if pattern == "healthy_above_target":
        base_adg = 0.14

    weights, gates = [], []
    kg = a["start_kg"]

    for d in range(N_DAYS):
        day = start_day + timedelta(days=d)
        days_left = N_DAYS - 1 - d          # 0 on the final day
        adg = base_adg
        gate_rate = GATE_PASSES_PER_DAY

        # --- deliberate patterns, applied only near the end of the window ---
        if pattern == "drop_moderate_no_gate" and days_left <= 5:
            adg = -0.42                      # ~-2.5 kg over the last 6 days
            if days_left <= 1:
                gate_rate = 0                # no passes in the last 48 h
        elif pattern == "drop_severe_no_gate" and days_left <= 5:
            adg = -0.78                      # ~-4.7 kg over the last 6 days
            if days_left <= 1:
                gate_rate = 0
        elif pattern == "drop_mild" and days_left <= 5:
            adg = -0.20                      # ~-1.2 kg over the last 6 days
        elif pattern == "slow_growth" and days_left <= 13:
            adg = 0.04                       # a third of the target for 14 days

        if d > 0:
            kg = kg + adg + random.gauss(0, 0.12)
        kg = max(kg, 15.0)

        # Smart Scale reading, once a day in the morning.
        ts = datetime.combine(day, datetime.min.time()) + timedelta(
            hours=6, minutes=random.randint(0, 90))
        weights.append([a["ear_tag"], ts.strftime("%Y-%m-%d %H:%M:%S"), round(kg, 1)])

        # Auto gate passes, spread over the day, alternating in/out.
        n_pass = poisson(gate_rate)
        if n_pass % 2 == 1:
            n_pass += 1                      # every "in" has an "out"
        times = sorted(random.uniform(5.0, 18.5) for _ in range(n_pass))
        for i, h in enumerate(times):
            gts = datetime.combine(day, datetime.min.time()) + timedelta(hours=h)
            gates.append([a["ear_tag"], gts.strftime("%Y-%m-%d %H:%M:%S"),
                          "in" if i % 2 == 0 else "out"])

    return weights, gates


def poisson(lam: float) -> int:
    """Knuth's Poisson sampler, enough for small lambda."""
    if lam <= 0:
        return 0
    import math
    L, k, p = math.exp(-lam), 0, 1.0
    while True:
        k += 1
        p *= random.random()
        if p <= L:
            return k - 1


def write_csv(name: str, header: list[str], rows: list[list]) -> None:
    path = os.path.join(OUT_DIR, name)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows)


if __name__ == "__main__":
    main()