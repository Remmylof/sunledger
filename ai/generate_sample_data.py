"""
Generate ~7 days of realistic hourly meter data for a 50 kW solar mini-grid in Kano, Nigeria,
with a handful of injected fake readings (night-time generation, spikes, inflated hours).

Writes:
  ai/sample_data.csv        - the meter batch the oracle verifies
  ai/injected_labels.json   - ground truth of which hours were tampered (for evaluation only;
                              the detector never reads this file)

Usage:  python ai/generate_sample_data.py
"""
from __future__ import annotations

import json
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
WAT = timezone(timedelta(hours=1))  # West Africa Time (UTC+1)

SITE_ID = 1
CAPACITY_KW = 50.0
LAT, LON = 12.0022, 8.5920  # Kano
PERFORMANCE_RATIO = 0.78
START = datetime(2026, 10, 5, 0, 0, tzinfo=WAT)
DAYS = 7
SEED = 42


def solar_elevation_deg(ts: datetime) -> float:
    """Approximate solar elevation (NOAA-style simplified) for Kano."""
    utc = ts.astimezone(timezone.utc)
    doy = utc.timetuple().tm_yday
    hour = utc.hour + utc.minute / 60 + 0.5  # mid-hour
    gamma = 2 * math.pi / 365 * (doy - 1 + (hour - 12) / 24)
    decl = (
        0.006918 - 0.399912 * math.cos(gamma) + 0.070257 * math.sin(gamma)
        - 0.006758 * math.cos(2 * gamma) + 0.000907 * math.sin(2 * gamma)
    )
    eqtime = 229.18 * (
        0.000075 + 0.001868 * math.cos(gamma) - 0.032077 * math.sin(gamma)
        - 0.014615 * math.cos(2 * gamma) - 0.040849 * math.sin(2 * gamma)
    )
    true_solar_min = hour * 60 + eqtime + 4 * LON
    ha = math.radians(true_solar_min / 4 - 180)
    lat = math.radians(LAT)
    cos_zen = math.sin(lat) * math.sin(decl) + math.cos(lat) * math.cos(decl) * math.cos(ha)
    return math.degrees(math.asin(max(-1.0, min(1.0, cos_zen))))


def main() -> None:
    rng = np.random.default_rng(SEED)
    rows = []
    # daily sky condition: post-rainy-season October in Kano, mostly clear with some haze/cloud days
    day_clearness = [0.92, 0.88, 0.70, 0.90, 0.85, 0.62, 0.89]

    for h in range(DAYS * 24):
        ts = START + timedelta(hours=h)
        day = h // 24
        elev = solar_elevation_deg(ts)
        if elev <= 0:
            ghi = 0.0
        else:
            clear_sky = 1050 * math.sin(math.radians(elev)) ** 1.15
            cloud = day_clearness[day] * rng.uniform(0.85, 1.05)
            if rng.random() < 0.12:  # passing cumulus
                cloud *= rng.uniform(0.45, 0.8)
            ghi = max(0.0, clear_sky * min(cloud, 1.0))

        temp = 24 + 10 * max(0.0, math.sin(math.pi * (ts.hour - 7) / 13)) + rng.normal(0, 0.8)
        cell_temp = temp + ghi / 800 * 25
        temp_derate = 1 - 0.004 * max(0.0, cell_temp - 25)
        energy = CAPACITY_KW * ghi / 1000 * PERFORMANCE_RATIO * temp_derate
        energy *= rng.normal(1.0, 0.025)  # meter / inverter noise
        energy = max(0.0, energy)
        if ghi < 5:
            energy = 0.0
            ghi = 0.0
        rows.append({"ts": ts, "irr": ghi, "temp": temp, "kwh": energy})

    # --------------------------------------------------------------- inject fakes
    injected = []

    def inject(day: int, hour: int, new_kwh: float, kind: str) -> None:
        idx = day * 24 + hour
        injected.append({
            "timestamp": rows[idx]["ts"].isoformat(),
            "kind": kind,
            "original_kwh": round(rows[idx]["kwh"], 3),
            "fake_kwh": new_kwh,
        })
        rows[idx]["kwh"] = new_kwh

    inject(1, 2, 18.40, "night_generation")      # 02:00, sun is down
    inject(3, 23, 11.75, "night_generation")     # 23:00, sun is down
    inject(2, 12, 96.30, "spike_over_capacity")  # 96 kWh in 1h from a 50 kW site
    inject(4, 13, 71.20, "spike_over_capacity")
    inject(5, 11, rows[5 * 24 + 11]["kwh"] * 1.9 + 4, "inflated_cloudy_hour")
    inject(6, 15, rows[6 * 24 + 15]["kwh"] + 17.5, "inflated_reading")

    with open(HERE / "sample_data.csv", "w", encoding="utf-8") as f:
        f.write("timestamp,site_id,irradiance_wm2,ambient_temp_c,energy_kwh\n")
        for r in rows:
            f.write(f"{r['ts'].isoformat()},{SITE_ID},{r['irr']:.1f},{r['temp']:.1f},{r['kwh']:.3f}\n")

    for inj in injected:
        inj["fake_kwh"] = round(inj["fake_kwh"], 3)
    (HERE / "injected_labels.json").write_text(json.dumps(injected, indent=2))
    total = sum(r["kwh"] for r in rows)
    print(f"Wrote {len(rows)} hourly rows to ai/sample_data.csv (total {total:.1f} kWh)")
    print(f"Injected {len(injected)} fake readings -> ai/injected_labels.json")


if __name__ == "__main__":
    main()
