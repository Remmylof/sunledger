"""
SunLedger oracle: verifies a meter batch with ai/anomaly.py and produces the
SunLedgerREC.submitBatch(...) payload.

Modes
  strict      (default) submit the batch exactly as metered. If it is tampered, its anomalyScore
              is above the on-chain threshold and the contract will reject it.
  quarantine  drop every flagged hour, re-verify the remaining hours, and certify only the clean
              energy. The dataHash still commits to the full raw batch, so the evidence (including
              the rejected hours) is auditable and that batch can never be minted twice.

Output is plain JSON (no ethers / web3 needed). If web3.py is installed and these env vars are set,
`--send` broadcasts the transaction:
  SUNLEDGER_RPC_URL, SUNLEDGER_CONTRACT, ORACLE_PRIVATE_KEY   (never commit these)

Usage
  python ai/oracle.py ai/sample_data.csv --site-id 1 --capacity-kw 50
  python ai/oracle.py ai/sample_data.csv --mode quarantine --out ai/out/payload.json
  python ai/oracle.py ai/sample_data.csv --mode quarantine --send
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from anomaly import detect, load_batch  # noqa: E402

DEFAULT_THRESHOLD = 30

SUBMIT_BATCH_ABI = [{
    "type": "function",
    "name": "submitBatch",
    "stateMutability": "nonpayable",
    "inputs": [
        {"name": "siteId", "type": "uint256"},
        {"name": "periodStart", "type": "uint64"},
        {"name": "periodEnd", "type": "uint64"},
        {"name": "kWh", "type": "uint256"},
        {"name": "anomalyScore", "type": "uint8"},
        {"name": "dataHash", "type": "bytes32"},
    ],
    "outputs": [{"name": "tokenId", "type": "uint256"}],
}]


def build_payload(csv_path: str, site_id: int, capacity_kw: float, mode: str, threshold: int) -> dict:
    df = load_batch(csv_path)
    raw = detect(df, capacity_kw)

    certified = raw
    excluded: list[str] = []
    if mode == "quarantine":
        current = df
        for _ in range(3):  # re-verify until no new flags appear
            res = detect(current, capacity_kw)
            if not res.flags:
                break
            bad = {f.timestamp for f in res.flags}
            excluded.extend(sorted(bad))
            current = current[~current["ts"].dt.strftime("%Y-%m-%dT%H:%M:%SZ").isin(bad)]
        certified = detect(current, capacity_kw)

    kwh = int(math.floor(certified.total_kwh))
    args = {
        "siteId": site_id,
        "periodStart": raw.period_start,
        "periodEnd": raw.period_end,
        "kWh": kwh,
        "anomalyScore": certified.anomaly_score,
        "dataHash": raw.data_hash,  # always commits to the full raw batch
    }
    return {
        "mode": mode,
        "contractCall": {
            "function": "submitBatch(uint256,uint64,uint64,uint256,uint8,bytes32)",
            "args": args,
            "argsOrdered": [args[k] for k in ["siteId", "periodStart", "periodEnd", "kWh", "anomalyScore", "dataHash"]],
        },
        "preflight": {
            "threshold": threshold,
            "willBeAccepted": certified.anomaly_score <= threshold and kwh > 0,
            "certificatesIfNoCarry": kwh // 1000,
            "remainderKWh": kwh % 1000,
        },
        "verification": {
            "rawAnomalyScore": raw.anomaly_score,
            "rawTotalKWh": raw.total_kwh,
            "physicsExpectedKWh": raw.expected_kwh,
            "criticalHours": raw.critical_hours,
            "suspiciousHours": raw.suspicious_hours,
            "excludedHours": excluded,
            "flags": [f.__dict__ for f in raw.flags],
        },
        "abi": SUBMIT_BATCH_ABI,
    }


def send(payload: dict) -> str:
    try:
        from web3 import Web3  # type: ignore
    except ImportError as exc:
        raise SystemExit("web3.py not installed: pip install web3") from exc

    rpc = os.environ.get("SUNLEDGER_RPC_URL")
    contract_addr = os.environ.get("SUNLEDGER_CONTRACT")
    key = os.environ.get("ORACLE_PRIVATE_KEY")
    if not (rpc and contract_addr and key):
        raise SystemExit("set SUNLEDGER_RPC_URL, SUNLEDGER_CONTRACT and ORACLE_PRIVATE_KEY to --send")

    w3 = Web3(Web3.HTTPProvider(rpc))
    acct = w3.eth.account.from_key(key)
    contract = w3.eth.contract(address=Web3.to_checksum_address(contract_addr), abi=payload["abi"])
    a = payload["contractCall"]["args"]
    fn = contract.functions.submitBatch(
        a["siteId"], a["periodStart"], a["periodEnd"], a["kWh"], a["anomalyScore"], bytes.fromhex(a["dataHash"][2:])
    )
    tx = fn.build_transaction({
        "from": acct.address,
        "nonce": w3.eth.get_transaction_count(acct.address),
        "chainId": w3.eth.chain_id,
    })
    signed = acct.sign_transaction(tx)
    raw_tx = getattr(signed, "raw_transaction", None) or getattr(signed, "rawTransaction")
    tx_hash = w3.eth.send_raw_transaction(raw_tx)
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash)
    if receipt.status != 1:
        raise SystemExit(f"transaction reverted: {tx_hash.hex()}")
    return tx_hash.hex()


def main() -> None:
    ap = argparse.ArgumentParser(description="SunLedger oracle")
    ap.add_argument("csv", nargs="?", default=str(Path(__file__).with_name("sample_data.csv")))
    ap.add_argument("--site-id", type=int, default=1)
    ap.add_argument("--capacity-kw", type=float, default=50.0)
    ap.add_argument("--mode", choices=["strict", "quarantine"], default="strict")
    ap.add_argument("--threshold", type=int, default=int(os.environ.get("ANOMALY_THRESHOLD", DEFAULT_THRESHOLD)))
    ap.add_argument("--out", help="also write the payload JSON to this path")
    ap.add_argument("--send", action="store_true", help="broadcast with web3.py (requires env vars)")
    ap.add_argument("--force", action="store_true", help="send even if preflight predicts a revert")
    args = ap.parse_args()

    payload = build_payload(args.csv, args.site_id, args.capacity_kw, args.mode, args.threshold)
    text = json.dumps(payload, indent=2)
    print(text)
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(text)

    if args.send:
        if not payload["preflight"]["willBeAccepted"] and not args.force:
            raise SystemExit("preflight: contract would reject this batch (anomalyScore above threshold); not sending")
        print(f"sent: {send(payload)}", file=sys.stderr)


if __name__ == "__main__":
    main()
