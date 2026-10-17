import { ethers, network } from "hardhat";

/**
 * Deploys SunLedgerREC and (optionally) grants OPERATOR / ORACLE roles.
 *
 * Env:
 *   DEPLOYER_PRIVATE_KEY   - deployer key (never commit; see .env.example)
 *   ADMIN_ADDRESS          - admin (defaults to deployer)
 *   OPERATOR_ADDRESS       - mini-grid operator wallet (optional)
 *   ORACLE_ADDRESS         - oracle wallet that runs ai/oracle.py (optional)
 *   METADATA_URI           - ERC-1155 metadata URI template
 *   ANOMALY_THRESHOLD      - 0..100 (default 30)
 */
async function main() {
  const [deployer] = await ethers.getSigners();
  if (!deployer) {
    throw new Error("No deployer account. Set DEPLOYER_PRIVATE_KEY in .env");
  }
  const admin = process.env.ADMIN_ADDRESS || deployer.address;
  const uri = process.env.METADATA_URI || "https://sunledger.example/api/rec/{id}.json";
  const threshold = Number(process.env.ANOMALY_THRESHOLD ?? 30);

  console.log(`Network:  ${network.name} (chainId ${network.config.chainId ?? "local"})`);
  console.log(`Deployer: ${deployer.address}`);

  const factory = await ethers.getContractFactory("SunLedgerREC");
  const rec = await factory.deploy(admin, uri, threshold);
  await rec.waitForDeployment();
  const address = await rec.getAddress();
  console.log(`SunLedgerREC deployed at ${address}`);

  if (admin.toLowerCase() === deployer.address.toLowerCase()) {
    if (process.env.OPERATOR_ADDRESS) {
      await (await rec.grantRole(await rec.OPERATOR_ROLE(), process.env.OPERATOR_ADDRESS)).wait();
      console.log(`Granted OPERATOR_ROLE to ${process.env.OPERATOR_ADDRESS}`);
    }
    if (process.env.ORACLE_ADDRESS) {
      await (await rec.grantRole(await rec.ORACLE_ROLE(), process.env.ORACLE_ADDRESS)).wait();
      console.log(`Granted ORACLE_ROLE to ${process.env.ORACLE_ADDRESS}`);
    }
  }

  console.log("\nAdd to .env for the oracle:");
  console.log(`SUNLEDGER_CONTRACT=${address}`);
}

main().catch((err) => {
  console.error(err);
  process.exitCode = 1;
});
