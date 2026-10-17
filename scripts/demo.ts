/**
 * End-to-end local demo:  AI oracle payload -> on-chain verification -> sale -> retirement.
 *
 *   python ai/oracle.py --out ai/out/payload_strict.json
 *   python ai/oracle.py --mode quarantine --out ai/out/payload.json
 *   npx hardhat run scripts/demo.ts
 */
import { ethers } from "hardhat";
import { readFileSync, existsSync } from "fs";
import path from "path";

function loadPayload(file: string) {
  const p = path.join(__dirname, "..", "ai", "out", file);
  if (!existsSync(p)) throw new Error(`missing ${p} - run ai/oracle.py first (see header)`);
  return JSON.parse(readFileSync(p, "utf8")).contractCall.args;
}

async function main() {
  const [admin, operator, oracle, buyer, payout] = await ethers.getSigners();
  const rec = await (await ethers.getContractFactory("SunLedgerREC")).deploy(
    admin.address,
    "https://sunledger.example/api/rec/{id}.json",
    30
  );
  await rec.waitForDeployment();
  await rec.grantRole(await rec.OPERATOR_ROLE(), operator.address);
  await rec.grantRole(await rec.ORACLE_ROLE(), oracle.address);
  await rec.connect(operator).registerSite("Kano Community Mini-Grid", "Kano, NG", 50, payout.address);
  console.log(`SunLedgerREC at ${await rec.getAddress()} | site #1 registered (50 kW, Kano)`);

  // 1. Raw (tampered) batch -> rejected on-chain
  const strict = loadPayload("payload_strict.json");
  try {
    await rec
      .connect(oracle)
      .submitBatch(strict.siteId, strict.periodStart, strict.periodEnd, strict.kWh, strict.anomalyScore, strict.dataHash);
    console.log("UNEXPECTED: tampered batch accepted");
    process.exitCode = 1;
  } catch (e: any) {
    const reason = rec.interface.parseError(e.data ?? e.error?.data ?? "0x")?.name ?? e.shortMessage ?? e.message;
    console.log(`Tampered batch (score ${strict.anomalyScore}, ${strict.kWh} kWh) -> REJECTED: ${reason}`);
  }

  // 2. Quarantined batch (fake hours excluded) -> minted
  const q = loadPayload("payload.json");
  await (
    await rec.connect(oracle).submitBatch(q.siteId, q.periodStart, q.periodEnd, q.kWh, q.anomalyScore, q.dataHash)
  ).wait();
  const id = await rec.certificateId(q.siteId, q.periodStart, q.periodEnd);
  const site = await rec.getSite(1);
  console.log(
    `Verified batch (score ${q.anomalyScore}, ${q.kWh} kWh) -> minted ${await rec.balanceOf(payout.address, id)} REC, carry ${site.carryKWh} kWh`
  );

  // 3. Replay of the same batch -> rejected
  try {
    await rec.connect(oracle).submitBatch(q.siteId, q.periodStart, q.periodEnd, q.kWh, q.anomalyScore, q.dataHash);
  } catch (e: any) {
    console.log(`Replay of same meter batch -> REJECTED: ${rec.interface.parseError(e.data)?.name ?? e.message}`);
  }

  // 4. Sell to a corporate buyer and retire
  await rec.connect(payout).safeTransferFrom(payout.address, buyer.address, id, 1, "0x");
  await (await rec.connect(buyer).retire(id, 1, "Example Manufacturing Ltd (Kano)", "Q4 2026 Scope 2 claim")).wait();
  const r = await rec.getRetirement(0);
  console.log(`Retired ${r.amount} REC for "${r.beneficiary}" (${r.reason}); buyer balance now ${await rec.balanceOf(buyer.address, id)}`);
}

main().catch((e) => {
  console.error(e);
  process.exitCode = 1;
});
