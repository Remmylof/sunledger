"""
SunLedger anomaly detector for hourly solar mini-grid meter data.

Two layers:
  1. Physics: expected_kwh = capacity_kw * irradiance/1000 * performance_ratio * temp_derate
     plus hard rules that no honest meter can break (generation at night, more energy in one hour
     than the nameplate allows, negative readings).
  2. Machine learning: an IsolationForest (scikit-learn) trained on the residuals between measured
     and expected output, catching readings that are physically possible but statistically
     inconsistent with the irradiance sensor (e.g. an inflated reading on a cloudy hour).

Outputs, per batch:
  - anomalyScore 0..100 (0 = clean, 100 = certainly tampered); the contract rejects > threshold
  - dataHash: SHA-256 of the canonical batch (bytes32-compatible, 0x-prefixed)
  - per-hour flags with reasons, so operators can see exactly which readings were rejected

Usage:
  python ai/anomaly.py ai/sample_data.csv --capacity-kw 50
  python ai/anomaly.py ai/sample_data.csv --json
"""
from __future__ import annotations

import argparse
import hashlib
import json
from dataclasses import dataclass, field, asdict
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.ensemble import IsolationForest

REQUIRED_COLUMNS = ["timestamp", "site_id", "irradiance_wm2", "ambient_temp_c", "energy_kwh"]

# --- model parameters (documented in README) --------------------------------------------------
PERFORMANCE_RATIO = 0.78     # typical for small PV + inverter + dust in the Sahel
TEMP_COEFF = -0.004          # per degC above 25 degC cell temperature (crystalline Si)
NOCT_RISE = 25.0             # cell temperature rise at 800 W/m2
NIGHT_IRRADIANCE = 10.0      # W/m2 below which no meaningful generation is possible
NIGHT_TOLERANCE_KWH = 0.5    # allow tiny inverter self-consumption artefacts
OVER_CAPACITY_MARGIN = 1.05  # 5% headroom for meter error
REL_TOLERANCE = 0.25         # +/-25% residual tolerated before an ML outlier is flagged
ABS_TOLERANCE_KWH = 3.0      # ...and at least 3 kWh absolute


@dataclass
class HourFlag:
    timestamp: str
    energy_kwh: float
    expected_kwh: float
    residual_kwh: float
    severity: str  # "critical" (physically impossible) | "suspicious" (statistical outlier)
    reasons: list[str] = field(default_factory=list)


@dataclass
class DetectionResult:
    site_id: int
    capacity_kw: float
    hours: int
    period_start: int  # unix seconds (inclusive)
    period_end: int    # unix seconds (exclusive)
    total_kwh: float
    expected_kwh: float
    verified_kwh: float          # energy from hours that passed all checks
    flagged_excess_kwh: float    # energy claimed above expectation in flagged hours
    anomaly_score: int
    data_hash: str
    critical_hours: int
    suspicious_hours: int
    flags: list[HourFlag]

    def to_dict(self) -> dict:
        d = asdict(self)
        d["flags"] = [asdict(f) for f in self.flags]
        return d


# --------------------------------------------------------------------------- helpers

def load_batch(path: str | Path) -> pd.DataFrame:
    df = pd.read_csv(path)
    missing = [c for c in REQUIRED_COLUMNS if c not in df.columns]
    if missing:
        raise ValueError(f"missing columns: {missing}")
    df["ts"] = pd.to_datetime(df["timestamp"], utc=True)
    df = df.sort_values("ts").reset_index(drop=True)
    if df["ts"].duplicated().any():
        raise ValueError("duplicate timestamps in batch")
    if df["site_id"].nunique() != 1:
        raise ValueError("a batch must contain exactly one site")
    return df


def canonical_bytes(df: pd.DataFrame) -> bytes:
    """Deterministic serialisation so every honest party computes the same hash."""
    lines = ["timestamp,site_id,irradiance_wm2,ambient_temp_c,energy_kwh"]
    for r in df.itertuples(index=False):
        ts = r.ts.strftime("%Y-%m-%dT%H:%M:%SZ")
        lines.append(f"{ts},{int(r.site_id)},{r.irradiance_wm2:.1f},{r.ambient_temp_c:.1f},{r.energy_kwh:.3f}")
    return ("\n".join(lines) + "\n").encode("utf-8")


def data_hash(df: pd.DataFrame) -> str:
    return "0x" + hashlib.sha256(canonical_bytes(df)).hexdigest()


def expected_output(df: pd.DataFrame, capacity_kw: float, pr: float = PERFORMANCE_RATIO) -> np.ndarray:
    """Physical model: capacity * irradiance/1000 * PR, derated for cell temperature."""
    irr = df["irradiance_wm2"].clip(lower=0).to_numpy(dtype=float)
    cell_temp = df["ambient_temp_c"].to_numpy(dtype=float) + irr / 800.0 * NOCT_RISE
    derate = 1 + TEMP_COEFF * np.clip(cell_temp - 25.0, 0, None)
    return capacity_kw * irr / 1000.0 * pr * derate


# --------------------------------------------------------------------------- detector

def detect(df: pd.DataFrame, capacity_kw: float, random_state: int = 7) -> DetectionResult:
    actual = df["energy_kwh"].to_numpy(dtype=float)
    irr = df["irradiance_wm2"].to_numpy(dtype=float)
    expected = expected_output(df, capacity_kw)
    residual = actual - expected
    rel_residual = residual / (expected + 1.0)
    is_day = irr >= NIGHT_IRRADIANCE

    reasons: list[list[str]] = [[] for _ in range(len(df))]
    severity = np.array([""] * len(df), dtype=object)

    # ---- layer 1: physics hard rules ------------------------------------------------------
    for i in range(len(df)):
        if actual[i] < 0:
            reasons[i].append("negative reading")
        if not is_day[i] and actual[i] > NIGHT_TOLERANCE_KWH:
            reasons[i].append(f"generation at night (irradiance {irr[i]:.0f} W/m2)")
        if actual[i] > capacity_kw * OVER_CAPACITY_MARGIN:
            reasons[i].append(f"exceeds nameplate ({actual[i]:.1f} kWh in 1h from {capacity_kw:.0f} kW)")
        if reasons[i]:
            severity[i] = "critical"

    # ---- layer 2: IsolationForest on residuals (daylight hours) ---------------------------
    day_idx = np.where(is_day)[0]
    ml_scores = np.zeros(len(df))
    if len(day_idx) >= 20:
        features = np.column_stack([
            residual[day_idx],
            rel_residual[day_idx],
            actual[day_idx] / capacity_kw,
            irr[day_idx] / 1000.0,
        ])
        forest = IsolationForest(n_estimators=300, contamination="auto", random_state=random_state)
        forest.fit(features)
        decision = forest.decision_function(features)  # < 0 => outlier
        ml_scores[day_idx] = -decision
        outlier = forest.predict(features) == -1
        # robust z-score of residuals (median / MAD) as a second statistical opinion
        med = np.median(residual[day_idx])
        mad = np.median(np.abs(residual[day_idx] - med)) * 1.4826 + 1e-9
        robust_z = (residual[day_idx] - med) / mad
        for k, i in enumerate(day_idx):
            big_residual = abs(residual[i]) > max(ABS_TOLERANCE_KWH, REL_TOLERANCE * expected[i])
            if outlier[k] and big_residual and abs(robust_z[k]) > 3.5:
                direction = "above" if residual[i] > 0 else "below"
                reasons[i].append(
                    f"inconsistent with irradiance: {actual[i]:.1f} kWh vs {expected[i]:.1f} expected "
                    f"({direction}, robust z={robust_z[k]:.1f})"
                )
                if not severity[i]:
                    severity[i] = "suspicious"

    flagged = np.array([bool(r) for r in reasons])
    critical = severity == "critical"
    suspicious = severity == "suspicious"

    # energy claimed above what the sun could explain, in flagged hours
    excess = float(np.clip(residual[flagged], 0, None).sum())
    total = float(actual.sum())
    excess_share = excess / total if total > 0 else 1.0
    verified = float(actual[~flagged].sum())

    # ---- batch anomaly score 0..100 --------------------------------------------------------
    # Physically impossible hours weigh most; statistical outliers less; plus share of energy
    # that is unexplained by irradiance. Clean weeks score in single digits.
    score = 400.0 * excess_share + 20.0 * critical.sum() + 8.0 * suspicious.sum()
    anomaly_score = int(min(100, round(score)))

    flags = [
        HourFlag(
            timestamp=df["ts"].iloc[i].strftime("%Y-%m-%dT%H:%M:%SZ"),
            energy_kwh=round(float(actual[i]), 3),
            expected_kwh=round(float(expected[i]), 3),
            residual_kwh=round(float(residual[i]), 3),
            severity=str(severity[i]),
            reasons=reasons[i],
        )
        for i in np.where(flagged)[0]
    ]

    start = int(df["ts"].iloc[0].timestamp())
    end = int(df["ts"].iloc[-1].timestamp()) + 3600
    return DetectionResult(
        site_id=int(df["site_id"].iloc[0]),
        capacity_kw=capacity_kw,
        hours=len(df),
        period_start=start,
        period_end=end,
        total_kwh=round(total, 3),
        expected_kwh=round(float(expected.sum()), 3),
        verified_kwh=round(verified, 3),
        flagged_excess_kwh=round(excess, 3),
        anomaly_score=anomaly_score,
        data_hash=data_hash(df),
        critical_hours=int(critical.sum()),
        suspicious_hours=int(suspicious.sum()),
        flags=flags,
    )


def print_report(res: DetectionResult) -> None:
    print("SunLedger anomaly report")
    print("=" * 72)
    print(f"site {res.site_id} | {res.capacity_kw:.0f} kW | {res.hours} hours")
    print(f"measured {res.total_kwh:,.1f} kWh | physics-expected {res.expected_kwh:,.1f} kWh")
    print(f"verified (unflagged) {res.verified_kwh:,.1f} kWh | unexplained excess {res.flagged_excess_kwh:,.1f} kWh")
    print(f"critical hours {res.critical_hours} | suspicious hours {res.suspicious_hours}")
    print(f"anomalyScore {res.anomaly_score}/100")
    print(f"dataHash {res.data_hash}")
    if res.flags:
        print("-" * 72)
        for f in res.flags:
            print(f"[{f.severity:<10}] {f.timestamp}  {f.energy_kwh:7.2f} kWh (exp {f.expected_kwh:6.2f})")
            for r in f.reasons:
                print(f"              - {r}")


def main() -> None:
    ap = argparse.ArgumentParser(description="SunLedger solar meter anomaly detector")
    ap.add_argument("csv", nargs="?", default=str(Path(__file__).with_name("sample_data.csv")))
    ap.add_argument("--capacity-kw", type=float, default=50.0)
    ap.add_argument("--json", action="store_true", help="print machine-readable JSON")
    args = ap.parse_args()

    res = detect(load_batch(args.csv), args.capacity_kw)
    if args.json:
        print(json.dumps(res.to_dict(), indent=2))
    else:
        print_report(res)


if __name__ == "__main__":
    main()
