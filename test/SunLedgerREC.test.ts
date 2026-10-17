import { expect } from "chai";
import { ethers } from "hardhat";
import { loadFixture, time } from "@nomicfoundation/hardhat-toolbox/network-helpers";

const HOUR = 3600;
const WEEK = 7 * 24 * HOUR;
const THRESHOLD = 30;

function batchHash(label: string): string {
  return ethers.sha256(ethers.toUtf8Bytes(label));
}

describe("SunLedgerREC", function () {
  async function deployFixture() {
    const [admin, operator, oracle, buyer, outsider, payout] = await ethers.getSigners();
    const Rec = await ethers.getContractFactory("SunLedgerREC");
    const rec = await Rec.deploy(admin.address, "https://sunledger.example/api/rec/{id}.json", THRESHOLD);
    await rec.waitForDeployment();

    await rec.grantRole(await rec.OPERATOR_ROLE(), operator.address);
    await rec.grantRole(await rec.ORACLE_ROLE(), oracle.address);

    // 50 kW mini-grid in Kano
    await rec.connect(operator).registerSite("Kano Community Mini-Grid", "Kano, NG", 50, payout.address);
    const siteId = 1n;

    // Move chain time forward so a 7-day period in the past is valid.
    const now = await time.latest();
    const periodStart = BigInt(now);
    await time.increase(WEEK + HOUR);
    const periodEnd = periodStart + BigInt(WEEK);

    return { rec, admin, operator, oracle, buyer, outsider, payout, siteId, periodStart, periodEnd };
  }

  async function mintedFixture() {
    const f = await deployFixture();
    // 2,350 kWh -> 2 certificates, 350 kWh carried forward
    await f.rec.connect(f.oracle).submitBatch(f.siteId, f.periodStart, f.periodEnd, 2350, 12, batchHash("week-1"));
    const tokenId = await f.rec.certificateId(f.siteId, f.periodStart, f.periodEnd);
    return { ...f, tokenId };
  }

  describe("deployment & roles", function () {
    it("sets admin, threshold and supports ERC-1155 + AccessControl interfaces", async function () {
      const { rec, admin } = await loadFixture(deployFixture);
      expect(await rec.hasRole(await rec.DEFAULT_ADMIN_ROLE(), admin.address)).to.equal(true);
      expect(await rec.anomalyThreshold()).to.equal(THRESHOLD);
      expect(await rec.supportsInterface("0xd9b67a26")).to.equal(true); // IERC1155
      expect(await rec.supportsInterface("0x7965db0b")).to.equal(true); // IAccessControl
    });

    it("only OPERATOR_ROLE can register sites", async function () {
      const { rec, outsider } = await loadFixture(deployFixture);
      await expect(rec.connect(outsider).registerSite("Fake", "Lagos, NG", 10, outsider.address))
        .to.be.revertedWithCustomError(rec, "AccessControlUnauthorizedAccount")
        .withArgs(outsider.address, await rec.OPERATOR_ROLE());
    });

    it("only ORACLE_ROLE can submit batches (operators cannot self-certify)", async function () {
      const { rec, operator, siteId, periodStart, periodEnd } = await loadFixture(deployFixture);
      await expect(rec.connect(operator).submitBatch(siteId, periodStart, periodEnd, 1000, 5, batchHash("x")))
        .to.be.revertedWithCustomError(rec, "AccessControlUnauthorizedAccount")
        .withArgs(operator.address, await rec.ORACLE_ROLE());
    });

    it("only admin can change the anomaly threshold, and it is capped at 100", async function () {
      const { rec, admin, outsider } = await loadFixture(deployFixture);
      await expect(rec.connect(outsider).setAnomalyThreshold(50)).to.be.revertedWithCustomError(
        rec,
        "AccessControlUnauthorizedAccount"
      );
      await expect(rec.connect(admin).setAnomalyThreshold(101)).to.be.revertedWithCustomError(rec, "InvalidScore");
      await expect(rec.connect(admin).setAnomalyThreshold(40))
        .to.emit(rec, "AnomalyThresholdChanged")
        .withArgs(THRESHOLD, 40);
    });

    it("admin can revoke the oracle", async function () {
      const { rec, admin, oracle, siteId, periodStart, periodEnd } = await loadFixture(deployFixture);
      await rec.connect(admin).revokeRole(await rec.ORACLE_ROLE(), oracle.address);
      await expect(
        rec.connect(oracle).submitBatch(siteId, periodStart, periodEnd, 1000, 5, batchHash("x"))
      ).to.be.revertedWithCustomError(rec, "AccessControlUnauthorizedAccount");
    });
  });

  describe("site registry", function () {
    it("stores site metadata", async function () {
      const { rec, operator, payout, siteId } = await loadFixture(deployFixture);
      const site = await rec.getSite(siteId);
      expect(site.name).to.equal("Kano Community Mini-Grid");
      expect(site.location).to.equal("Kano, NG");
      expect(site.capacityKw).to.equal(50);
      expect(site.payout).to.equal(payout.address);
      expect(site.operator).to.equal(operator.address);
      expect(site.active).to.equal(true);
    });

    it("rejects batches for unknown or deactivated sites", async function () {
      const { rec, operator, oracle, siteId, periodStart, periodEnd } = await loadFixture(deployFixture);
      await expect(rec.connect(oracle).submitBatch(99, periodStart, periodEnd, 1000, 5, batchHash("x")))
        .to.be.revertedWithCustomError(rec, "UnknownSite")
        .withArgs(99);
      await rec.connect(operator).setSiteActive(siteId, false);
      await expect(
        rec.connect(oracle).submitBatch(siteId, periodStart, periodEnd, 1000, 5, batchHash("x"))
      ).to.be.revertedWithCustomError(rec, "SiteInactive");
    });

    it("only the site operator or admin can toggle a site", async function () {
      const { rec, outsider, siteId } = await loadFixture(deployFixture);
      await expect(rec.connect(outsider).setSiteActive(siteId, false)).to.be.revertedWithCustomError(
        rec,
        "NotSiteOperator"
      );
    });
  });

  describe("minting verified batches", function () {
    it("mints 1 token per verified MWh with full on-chain metadata", async function () {
      const { rec, oracle, payout, siteId, periodStart, periodEnd } = await loadFixture(deployFixture);
      const hash = batchHash("week-1");
      const tokenId = await rec.certificateId(siteId, periodStart, periodEnd);

      await expect(rec.connect(oracle).submitBatch(siteId, periodStart, periodEnd, 2350, 12, hash))
        .to.emit(rec, "BatchVerified")
        .withArgs(tokenId, siteId, periodStart, periodEnd, 2350, 12, hash, 2, 350)
        .and.to.emit(rec, "TransferSingle");

      expect(await rec.balanceOf(payout.address, tokenId)).to.equal(2);
      const cert = await rec.getCertificate(tokenId);
      expect(cert.siteId).to.equal(siteId);
      expect(cert.periodStart).to.equal(periodStart);
      expect(cert.periodEnd).to.equal(periodEnd);
      expect(cert.kWh).to.equal(2350);
      expect(cert.anomalyScore).to.equal(12);
      expect(cert.dataHash).to.equal(hash);
      expect(cert.minted).to.equal(2);
      expect(await rec.batchHashUsed(hash)).to.equal(true);
      expect(await rec.certificateCount()).to.equal(1);
      expect(await rec.tokenIdAt(0)).to.equal(tokenId);
    });

    it("carries sub-MWh remainders into the next period", async function () {
      const { rec, oracle, payout, siteId, periodEnd } = await loadFixture(mintedFixture);
      // second week: 700 kWh + 350 carried = 1,050 -> 1 certificate, 50 carried
      const start2 = periodEnd;
      const end2 = start2 + BigInt(WEEK);
      await time.increase(WEEK);
      await rec.connect(oracle).submitBatch(siteId, start2, end2, 700, 8, batchHash("week-2"));
      const id2 = await rec.certificateId(siteId, start2, end2);
      expect(await rec.balanceOf(payout.address, id2)).to.equal(1);
      const site = await rec.getSite(siteId);
      expect(site.carryKWh).to.equal(50);
      expect(site.totalKWh).to.equal(3050);
    });

    it("rejects a meter batch hash that was already minted (no double counting)", async function () {
      const { rec, oracle, siteId, periodEnd } = await loadFixture(mintedFixture);
      const start2 = periodEnd;
      const end2 = start2 + BigInt(WEEK);
      await time.increase(WEEK);
      const reused = batchHash("week-1");
      await expect(rec.connect(oracle).submitBatch(siteId, start2, end2, 1500, 5, reused))
        .to.be.revertedWithCustomError(rec, "DuplicateBatch")
        .withArgs(reused);
    });

    it("rejects overlapping periods for the same site even with a new hash", async function () {
      const { rec, oracle, siteId, periodStart, periodEnd } = await loadFixture(mintedFixture);
      await expect(
        rec.connect(oracle).submitBatch(siteId, periodStart, periodEnd, 1500, 5, batchHash("week-1-replayed"))
      ).to.be.revertedWithCustomError(rec, "PeriodOverlap");
    });

    it("rejects batches with anomaly score above the threshold", async function () {
      const { rec, oracle, siteId, periodStart, periodEnd } = await loadFixture(deployFixture);
      await expect(rec.connect(oracle).submitBatch(siteId, periodStart, periodEnd, 2000, 31, batchHash("bad")))
        .to.be.revertedWithCustomError(rec, "AnomalyTooHigh")
        .withArgs(31, THRESHOLD);
      // hash is NOT burned by a rejected batch, so a corrected batch can still be submitted
      expect(await rec.batchHashUsed(batchHash("bad"))).to.equal(false);
      // score exactly at threshold is accepted
      await expect(rec.connect(oracle).submitBatch(siteId, periodStart, periodEnd, 2000, 30, batchHash("ok"))).to
        .not.be.reverted;
    });

    it("rejects invalid anomaly scores, zero energy and empty hashes", async function () {
      const { rec, admin, oracle, siteId, periodStart, periodEnd } = await loadFixture(deployFixture);
      await rec.connect(admin).setAnomalyThreshold(100);
      await expect(
        rec.connect(oracle).submitBatch(siteId, periodStart, periodEnd, 1000, 101, batchHash("x"))
      ).to.be.revertedWithCustomError(rec, "InvalidScore");
      await expect(
        rec.connect(oracle).submitBatch(siteId, periodStart, periodEnd, 0, 1, batchHash("x"))
      ).to.be.revertedWithCustomError(rec, "ZeroValue");
      await expect(
        rec.connect(oracle).submitBatch(siteId, periodStart, periodEnd, 1000, 1, ethers.ZeroHash)
      ).to.be.revertedWithCustomError(rec, "ZeroValue");
    });

    it("rejects physically impossible generation (> nameplate x hours)", async function () {
      const { rec, oracle, siteId, periodStart, periodEnd } = await loadFixture(deployFixture);
      // 50 kW * 168 h = 8,400 kWh max
      await expect(rec.connect(oracle).submitBatch(siteId, periodStart, periodEnd, 8401, 1, batchHash("x")))
        .to.be.revertedWithCustomError(rec, "ExceedsCapacity")
        .withArgs(8401, 8400);
    });

    it("rejects future or inverted periods", async function () {
      const { rec, oracle, siteId, periodStart, periodEnd } = await loadFixture(deployFixture);
      await expect(
        rec.connect(oracle).submitBatch(siteId, periodEnd, periodStart, 1000, 1, batchHash("x"))
      ).to.be.revertedWithCustomError(rec, "InvalidPeriod");
      const future = BigInt(await time.latest()) + 10_000n;
      await expect(
        rec.connect(oracle).submitBatch(siteId, periodStart, future, 1000, 1, batchHash("x"))
      ).to.be.revertedWithCustomError(rec, "InvalidPeriod");
    });
  });

  describe("transfer", function () {
    it("lets the payout wallet sell certificates to a buyer", async function () {
      const { rec, payout, buyer, tokenId } = await loadFixture(mintedFixture);
      await expect(rec.connect(payout).safeTransferFrom(payout.address, buyer.address, tokenId, 1, "0x"))
        .to.emit(rec, "TransferSingle")
        .withArgs(payout.address, payout.address, buyer.address, tokenId, 1);
      expect(await rec.balanceOf(buyer.address, tokenId)).to.equal(1);
      expect(await rec.balanceOf(payout.address, tokenId)).to.equal(1);
    });

    it("blocks transfers by non-owners without approval", async function () {
      const { rec, payout, buyer, outsider, tokenId } = await loadFixture(mintedFixture);
      await expect(
        rec.connect(outsider).safeTransferFrom(payout.address, buyer.address, tokenId, 1, "0x")
      ).to.be.revertedWithCustomError(rec, "ERC1155MissingApprovalForAll");
    });
  });

  describe("retirement", function () {
    it("burns certificates and stores a permanent Retirement record", async function () {
      const { rec, payout, buyer, tokenId } = await loadFixture(mintedFixture);
      await rec.connect(payout).safeTransferFrom(payout.address, buyer.address, tokenId, 2, "0x");

      await expect(rec.connect(buyer).retire(tokenId, 1, "Dangote Cement plc", "2026 Scope 2 claim"))
        .to.emit(rec, "Retired")
        .withArgs(0, tokenId, buyer.address, 1, "Dangote Cement plc", "2026 Scope 2 claim");

      expect(await rec.balanceOf(buyer.address, tokenId)).to.equal(1);
      expect(await rec.totalRetired(tokenId)).to.equal(1);
      expect(await rec.retirementCount()).to.equal(1);
      const r = await rec.getRetirement(0);
      expect(r.tokenId).to.equal(tokenId);
      expect(r.amount).to.equal(1);
      expect(r.retiree).to.equal(buyer.address);
      expect(r.beneficiary).to.equal("Dangote Cement plc");
      expect(r.reason).to.equal("2026 Scope 2 claim");
      expect(r.timestamp).to.be.greaterThan(0);
    });

    it("retired certificates cannot be transferred or retired again", async function () {
      const { rec, payout, buyer, tokenId } = await loadFixture(mintedFixture);
      await rec.connect(payout).retire(tokenId, 2, "Acme Ltd", "claim");
      expect(await rec.balanceOf(payout.address, tokenId)).to.equal(0);
      await expect(
        rec.connect(payout).safeTransferFrom(payout.address, buyer.address, tokenId, 1, "0x")
      ).to.be.revertedWithCustomError(rec, "ERC1155InsufficientBalance");
      await expect(rec.connect(payout).retire(tokenId, 1, "Acme Ltd", "again")).to.be.revertedWithCustomError(
        rec,
        "ERC1155InsufficientBalance"
      );
    });

    it("rejects zero-amount retirements, empty beneficiary, and retiring tokens you don't hold", async function () {
      const { rec, payout, outsider, tokenId } = await loadFixture(mintedFixture);
      await expect(rec.connect(payout).retire(tokenId, 0, "Acme", "r")).to.be.revertedWithCustomError(
        rec,
        "ZeroValue"
      );
      await expect(rec.connect(payout).retire(tokenId, 1, "", "r")).to.be.revertedWithCustomError(
        rec,
        "EmptyBeneficiary"
      );
      await expect(rec.connect(outsider).retire(tokenId, 1, "Acme", "r")).to.be.revertedWithCustomError(
        rec,
        "ERC1155InsufficientBalance"
      );
    });
  });
});
