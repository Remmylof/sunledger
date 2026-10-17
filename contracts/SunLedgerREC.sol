// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {ERC1155} from "@openzeppelin/contracts/token/ERC1155/ERC1155.sol";
import {AccessControl} from "@openzeppelin/contracts/access/AccessControl.sol";

/**
 * @title SunLedgerREC
 * @author Lawal Ridwan
 * @notice Verifiable Renewable Energy Certificates (RECs) for Nigerian solar mini-grids.
 *
 *  - OPERATOR_ROLE registers mini-grid sites (capacity, location, payout wallet).
 *  - ORACLE_ROLE submits AI-verified hourly meter batches.
 *  - Every 1 MWh (1,000 kWh) of verified generation = 1 certificate token.
 *    Sub-MWh remainders are carried forward per site, so no energy is lost or double-counted.
 *  - Token id = keccak256(siteId, periodStart, periodEnd) -> one certificate class per site+period.
 *  - Every meter batch hash can be minted exactly once; periods of a site can never overlap.
 *  - Batches whose anomaly score is above the threshold are rejected on-chain.
 *  - retire() burns certificates and writes a permanent Retirement record so a claimed REC
 *    can never be resold.
 */
contract SunLedgerREC is ERC1155, AccessControl {
    bytes32 public constant OPERATOR_ROLE = keccak256("OPERATOR_ROLE");
    bytes32 public constant ORACLE_ROLE = keccak256("ORACLE_ROLE");

    uint256 public constant KWH_PER_CERTIFICATE = 1_000; // 1 MWh
    uint8 public constant MAX_SCORE = 100;

    struct Site {
        string name;
        string location; // e.g. "Kano, NG"
        uint32 capacityKw;
        address payout; // receives minted certificates
        address operator; // who registered the site
        bool active;
        uint64 lastPeriodEnd; // prevents overlapping periods
        uint256 carryKWh; // sub-MWh remainder carried to next batch
        uint256 totalKWh; // lifetime verified generation
    }

    struct Certificate {
        uint256 siteId;
        uint64 periodStart;
        uint64 periodEnd;
        uint256 kWh; // verified kWh reported for this period
        uint8 anomalyScore; // 0 (clean) .. 100 (certainly tampered)
        bytes32 dataHash; // SHA-256 of the canonical meter batch
        uint256 minted; // certificates minted for this id
        uint64 mintedAt;
    }

    struct Retirement {
        uint256 tokenId;
        uint256 amount;
        address retiree;
        string beneficiary;
        string reason;
        uint64 timestamp;
    }

    uint8 public anomalyThreshold;
    uint256 public siteCount;

    mapping(uint256 => Site) private _sites;
    mapping(uint256 => Certificate) private _certificates;
    mapping(bytes32 => bool) public batchHashUsed;
    mapping(uint256 => uint256) public totalRetired; // tokenId => retired amount
    Retirement[] private _retirements;
    uint256[] private _tokenIds;

    event SiteRegistered(uint256 indexed siteId, string name, string location, uint32 capacityKw, address payout, address indexed operator);
    event SiteStatusChanged(uint256 indexed siteId, bool active);
    event BatchVerified(
        uint256 indexed tokenId,
        uint256 indexed siteId,
        uint64 periodStart,
        uint64 periodEnd,
        uint256 kWh,
        uint8 anomalyScore,
        bytes32 indexed dataHash,
        uint256 certificatesMinted,
        uint256 carryKWh
    );
    event Retired(
        uint256 indexed retirementId,
        uint256 indexed tokenId,
        address indexed retiree,
        uint256 amount,
        string beneficiary,
        string reason
    );
    event AnomalyThresholdChanged(uint8 oldThreshold, uint8 newThreshold);

    error UnknownSite(uint256 siteId);
    error SiteInactive(uint256 siteId);
    error NotSiteOperator(uint256 siteId, address caller);
    error InvalidPeriod();
    error PeriodOverlap(uint64 periodStart, uint64 lastPeriodEnd);
    error DuplicateBatch(bytes32 dataHash);
    error AnomalyTooHigh(uint8 score, uint8 threshold);
    error InvalidScore(uint8 score);
    error ExceedsCapacity(uint256 kWh, uint256 maxKWh);
    error ZeroValue();
    error ZeroAddress();
    error EmptyBeneficiary();

    constructor(address admin, string memory uri_, uint8 threshold_) ERC1155(uri_) {
        if (admin == address(0)) revert ZeroAddress();
        if (threshold_ > MAX_SCORE) revert InvalidScore(threshold_);
        _grantRole(DEFAULT_ADMIN_ROLE, admin);
        anomalyThreshold = threshold_;
    }

    // ------------------------------------------------------------------ sites

    function registerSite(
        string calldata name,
        string calldata location,
        uint32 capacityKw,
        address payout
    ) external onlyRole(OPERATOR_ROLE) returns (uint256 siteId) {
        if (capacityKw == 0) revert ZeroValue();
        if (payout == address(0)) revert ZeroAddress();
        siteId = ++siteCount;
        Site storage s = _sites[siteId];
        s.name = name;
        s.location = location;
        s.capacityKw = capacityKw;
        s.payout = payout;
        s.operator = msg.sender;
        s.active = true;
        emit SiteRegistered(siteId, name, location, capacityKw, payout, msg.sender);
    }

    function setSiteActive(uint256 siteId, bool active) external {
        Site storage s = _requireSite(siteId);
        if (msg.sender != s.operator && !hasRole(DEFAULT_ADMIN_ROLE, msg.sender)) {
            revert NotSiteOperator(siteId, msg.sender);
        }
        s.active = active;
        emit SiteStatusChanged(siteId, active);
    }

    // --------------------------------------------------------------- minting

    /// @notice Deterministic certificate id for a site + period.
    function certificateId(uint256 siteId, uint64 periodStart, uint64 periodEnd) public pure returns (uint256) {
        return uint256(keccak256(abi.encode(siteId, periodStart, periodEnd)));
    }

    /**
     * @notice Submit an AI-verified meter batch. Mints floor((kWh + carry) / 1000) certificates
     *         to the site's payout wallet.
     */
    function submitBatch(
        uint256 siteId,
        uint64 periodStart,
        uint64 periodEnd,
        uint256 kWh,
        uint8 anomalyScore,
        bytes32 dataHash
    ) external onlyRole(ORACLE_ROLE) returns (uint256 tokenId) {
        Site storage s = _requireSite(siteId);
        if (!s.active) revert SiteInactive(siteId);
        if (batchHashUsed[dataHash]) revert DuplicateBatch(dataHash);
        if (periodEnd <= periodStart || periodEnd > block.timestamp) revert InvalidPeriod();
        if (periodStart < s.lastPeriodEnd) revert PeriodOverlap(periodStart, s.lastPeriodEnd);
        if (kWh == 0 || dataHash == bytes32(0)) revert ZeroValue();
        if (anomalyScore > MAX_SCORE) revert InvalidScore(anomalyScore);
        if (anomalyScore > anomalyThreshold) revert AnomalyTooHigh(anomalyScore, anomalyThreshold);

        // Physical plausibility: a site can never produce more than nameplate * hours.
        uint256 maxKWh = (uint256(s.capacityKw) * (periodEnd - periodStart)) / 3600;
        if (kWh > maxKWh) revert ExceedsCapacity(kWh, maxKWh);

        batchHashUsed[dataHash] = true;
        tokenId = certificateId(siteId, periodStart, periodEnd);

        uint256 pool = kWh + s.carryKWh;
        uint256 amount = pool / KWH_PER_CERTIFICATE;
        s.carryKWh = pool % KWH_PER_CERTIFICATE;
        s.lastPeriodEnd = periodEnd;
        s.totalKWh += kWh;

        _certificates[tokenId] = Certificate({
            siteId: siteId,
            periodStart: periodStart,
            periodEnd: periodEnd,
            kWh: kWh,
            anomalyScore: anomalyScore,
            dataHash: dataHash,
            minted: amount,
            mintedAt: uint64(block.timestamp)
        });
        _tokenIds.push(tokenId);

        if (amount > 0) {
            _mint(s.payout, tokenId, amount, "");
        }
        emit BatchVerified(tokenId, siteId, periodStart, periodEnd, kWh, anomalyScore, dataHash, amount, s.carryKWh);
    }

    // ------------------------------------------------------------ retirement

    /**
     * @notice Permanently retire (burn) certificates on behalf of a beneficiary.
     *         The Retirement record is stored forever and the tokens cannot be resold.
     */
    function retire(
        uint256 tokenId,
        uint256 amount,
        string calldata beneficiary,
        string calldata reason
    ) external returns (uint256 retirementId) {
        if (amount == 0) revert ZeroValue();
        if (bytes(beneficiary).length == 0) revert EmptyBeneficiary();
        _burn(msg.sender, tokenId, amount); // reverts on insufficient balance
        totalRetired[tokenId] += amount;
        retirementId = _retirements.length;
        _retirements.push(
            Retirement({
                tokenId: tokenId,
                amount: amount,
                retiree: msg.sender,
                beneficiary: beneficiary,
                reason: reason,
                timestamp: uint64(block.timestamp)
            })
        );
        emit Retired(retirementId, tokenId, msg.sender, amount, beneficiary, reason);
    }

    // ----------------------------------------------------------------- admin

    function setAnomalyThreshold(uint8 newThreshold) external onlyRole(DEFAULT_ADMIN_ROLE) {
        if (newThreshold > MAX_SCORE) revert InvalidScore(newThreshold);
        emit AnomalyThresholdChanged(anomalyThreshold, newThreshold);
        anomalyThreshold = newThreshold;
    }

    function setURI(string calldata newUri) external onlyRole(DEFAULT_ADMIN_ROLE) {
        _setURI(newUri);
    }

    // ----------------------------------------------------------------- views

    function getSite(uint256 siteId) external view returns (Site memory) {
        return _requireSiteView(siteId);
    }

    function getCertificate(uint256 tokenId) external view returns (Certificate memory) {
        return _certificates[tokenId];
    }

    function getRetirement(uint256 retirementId) external view returns (Retirement memory) {
        return _retirements[retirementId];
    }

    function retirementCount() external view returns (uint256) {
        return _retirements.length;
    }

    function certificateCount() external view returns (uint256) {
        return _tokenIds.length;
    }

    function tokenIdAt(uint256 index) external view returns (uint256) {
        return _tokenIds[index];
    }

    function supportsInterface(bytes4 interfaceId) public view override(ERC1155, AccessControl) returns (bool) {
        return super.supportsInterface(interfaceId);
    }

    // -------------------------------------------------------------- internal

    function _requireSite(uint256 siteId) private view returns (Site storage s) {
        s = _sites[siteId];
        if (s.capacityKw == 0) revert UnknownSite(siteId);
    }

    function _requireSiteView(uint256 siteId) private view returns (Site memory) {
        Site memory s = _sites[siteId];
        if (s.capacityKw == 0) revert UnknownSite(siteId);
        return s;
    }
}
