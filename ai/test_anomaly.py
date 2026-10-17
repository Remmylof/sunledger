"""Tests for the SunLedger detector + oracle.  Run:  python -m unittest ai/test_anomaly.py -v"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

import pandas as pd

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from anomaly import data_hash, detect, load_batch  # noqa: E402
from oracle import build_payload  # noqa: E402

CSV = HERE / "sample_data.csv"
LABELS = json.loads((HERE / "injected_labels.json").read_text())


def to_utc(ts: str) -> str:
    return pd.Timestamp(ts).tz_convert("UTC").strftime("%Y-%m-%dT%H:%M:%SZ")


def clean_batch() -> pd.DataFrame:
    """Sample data with the injected fakes reverted to the original genuine readings."""
    df = load_batch(CSV)
    for lab in LABELS:
        mask = df["ts"] == pd.Timestamp(lab["timestamp"]).tz_convert("UTC")
        df.loc[mask, "energy_kwh"] = lab["original_kwh"]
    return df


class DetectorTests(unittest.TestCase):
    def test_flags_every_injected_fake(self):
        res = detect(load_batch(CSV), 50)
        flagged = {f.timestamp for f in res.flags}
        injected = {to_utc(l["timestamp"]) for l in LABELS}
        self.assertTrue(injected.issubset(flagged), f"missed: {injected - flagged}")

    def test_no_false_positives_on_sample(self):
        res = detect(load_batch(CSV), 50)
        flagged = {f.timestamp for f in res.flags}
        injected = {to_utc(l["timestamp"]) for l in LABELS}
        self.assertEqual(flagged - injected, set())

    def test_tampered_batch_scores_above_threshold(self):
        self.assertGreater(detect(load_batch(CSV), 50).anomaly_score, 30)

    def test_clean_batch_scores_low(self):
        res = detect(clean_batch(), 50)
        self.assertLessEqual(res.anomaly_score, 10)
        self.assertEqual(res.flags, [])

    def test_hash_is_deterministic_and_tamper_evident(self):
        df = load_batch(CSV)
        self.assertEqual(data_hash(df), data_hash(load_batch(CSV)))
        self.assertTrue(data_hash(df).startswith("0x") and len(data_hash(df)) == 66)
        df2 = df.copy()
        df2.loc[100, "energy_kwh"] += 0.001
        self.assertNotEqual(data_hash(df), data_hash(df2))


class OracleTests(unittest.TestCase):
    def test_strict_payload_is_rejected_by_preflight(self):
        p = build_payload(str(CSV), 1, 50, "strict", 30)
        self.assertFalse(p["preflight"]["willBeAccepted"])

    def test_quarantine_certifies_only_clean_energy(self):
        p = build_payload(str(CSV), 1, 50, "quarantine", 30)
        a = p["contractCall"]["args"]
        self.assertTrue(p["preflight"]["willBeAccepted"])
        self.assertLessEqual(a["anomalyScore"], 30)
        self.assertEqual(len(p["verification"]["excludedHours"]), len(LABELS))
        self.assertLess(a["kWh"], p["verification"]["rawTotalKWh"])
        self.assertEqual(a["periodEnd"] - a["periodStart"], 168 * 3600)
        # stays within nameplate * hours, which the contract also enforces
        self.assertLessEqual(a["kWh"], 50 * 168)


if __name__ == "__main__":
    unittest.main()
