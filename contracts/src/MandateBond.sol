// SPDX-License-Identifier: Apache-2.0
pragma solidity ^0.8.24;

/// @title MandateBond
/// @notice Custody for the bond a strategist posts to take followers, plus the
///         challenge game that decides when it is slashed.
///
/// What this contract does and does not claim:
///
/// It does NOT verify fills onchain. Checking whether a strategist honoured a
/// slippage ceiling would mean putting every fill and every venue quote on
/// chain, which is neither cheap nor honest to promise. Anyone claiming
/// otherwise has not costed it.
///
/// What it does instead is make the two things that must be tamper proof
/// tamper proof, then settle disputes with money rather than with a committee:
///
///   1. The rules hash is stored here at publication. What was promised is
///      immutable and public, so it cannot be rewritten after the fact.
///   2. The bond is held here, not by the strategist and not by us. It cannot
///      be withdrawn while followers are exposed.
///   3. A slash is opened by a challenger who stakes their own money on the
///      claim. The strategist may dispute within a window. Unanswered claims
///      execute; answered ones go to the arbiter named at construction.
///
/// The challenger's stake is what stops griefing: a false claim loses it to the
/// strategist. That is the same shape as any optimistic system, chosen because
/// the alternative is either trusting one party or pretending to an onchain
/// verification nobody can afford.
///
/// Off chain, `core/bond.py` computes exactly what a challenger should submit,
/// from the rules hashed here and from public fills. The arithmetic is open, so
/// both sides of a dispute can run it.
contract MandateBond {
    // --- types --------------------------------------------------------------

    struct Bond {
        address strategist;
        uint256 posted;          // held here, in the bond token's units
        uint256 committed;       // follower money this mandate directs
        uint256 slashed;
        uint64 lockedUntil;      // no withdrawal before this
        bool exists;
    }

    struct Claim {
        address challenger;
        uint256 stake;
        uint256 slashBps;        // what core/bond.py computed
        bytes32 evidence;        // hash of the fills and the audit output
        uint64 opensAt;
        uint64 closesAt;
        bool disputed;
        bool resolved;
    }

    // --- configuration ------------------------------------------------------

    /// Minimum bond, plus the share of committed follower money required above
    /// it. Mirrors core/bond.py: floor 250, 2% of committed.
    uint256 public immutable bondFloor;
    uint16 public constant BOND_BPS_OF_COMMITTED = 200;

    /// A challenger must stake this share of the slash they are claiming.
    uint16 public constant CHALLENGE_STAKE_BPS = 1_000; // 10%

    /// How long a strategist has to dispute before a claim executes.
    /// Timing uses block.timestamp deliberately. The lint warning about
    /// validator manipulation is real and irrelevant here: the window is
    /// measured in days. A validator moving it by seconds changes nothing
    /// a challenger or a strategist could exploit.
    uint64 public immutable challengeWindow;

    /// Only used when a claim is disputed. Named at construction and public, so
    /// a follower knows before subscribing who breaks a tie.
    address public immutable arbiter;

    /// The bond asset. $MANDATE, launched through Bankr on Robinhood Chain.
    address public immutable bondToken;

    // --- state --------------------------------------------------------------

    mapping(bytes32 => Bond) public bonds;                  // mandateId -> bond
    mapping(bytes32 => bytes32) public rulesHash;           // mandateId -> rules
    mapping(bytes32 => Claim[]) public claims;

    // --- events -------------------------------------------------------------

    event Published(bytes32 indexed mandateId, address indexed strategist, bytes32 rules);
    event Posted(bytes32 indexed mandateId, uint256 amount, uint256 total);
    event Committed(bytes32 indexed mandateId, uint256 committed, uint256 required);
    event ClaimOpened(bytes32 indexed mandateId, uint256 indexed index,
                      address indexed challenger, uint256 slashBps, bytes32 evidence);
    event ClaimDisputed(bytes32 indexed mandateId, uint256 indexed index);
    event ClaimResolved(bytes32 indexed mandateId, uint256 indexed index,
                        bool upheld, uint256 slashed);
    event Paid(bytes32 indexed mandateId, address indexed to, uint256 amount);
    event Withdrawn(bytes32 indexed mandateId, uint256 amount);

    // --- errors -------------------------------------------------------------

    error NotStrategist();
    error NotArbiter();
    error UnknownMandate();
    error AlreadyPublished();
    error BondLocked();
    error BondTooSmall(uint256 required, uint256 posted);
    error NothingToSlash();
    error WindowOpen();
    error WindowClosed();
    error AlreadyResolved();
    error NotDisputed();
    error BadSlash();
    error TransferFailed();

    constructor(address _bondToken, address _arbiter, uint256 _bondFloor,
                uint64 _challengeWindow) {
        bondToken = _bondToken;
        arbiter = _arbiter;
        bondFloor = _bondFloor;
        challengeWindow = _challengeWindow;
    }

    // --- publication --------------------------------------------------------

    /// @notice Record a mandate's rules hash and open its bond.
    /// @param mandateId sha256 of the canonical rules, the same id core/spec.py
    ///        derives. It is supplied rather than computed because the rules
    ///        are JSON. Hashing JSON onchain would be a way to burn gas.
    /// @param rules the same value. Stored separately so a later change to how
    ///        ids are formed cannot silently re-point an existing mandate.
    function publish(bytes32 mandateId, bytes32 rules) external {
        if (bonds[mandateId].exists) revert AlreadyPublished();
        bonds[mandateId] = Bond({
            strategist: msg.sender,
            posted: 0,
            committed: 0,
            slashed: 0,
            lockedUntil: 0,
            exists: true
        });
        rulesHash[mandateId] = rules;
        emit Published(mandateId, msg.sender, rules);
    }

    /// @notice Add to the bond. Anyone may top up a strategist's bond.
    function post(bytes32 mandateId, uint256 amount) external {
        Bond storage b = bonds[mandateId];
        if (!b.exists) revert UnknownMandate();
        _pull(msg.sender, amount);
        b.posted += amount;
        emit Posted(mandateId, amount, b.posted);
    }

    /// @notice Record how much follower money this mandate now directs, then
    ///         lock the bond while that exposure exists.
    /// @dev Called by the book as followers subscribe. It raises the required
    ///      bond, so a strategist cannot take on exposure their bond does not
    ///      cover.
    function setCommitted(bytes32 mandateId, uint256 committed, uint64 lockFor) external {
        Bond storage b = bonds[mandateId];
        if (!b.exists) revert UnknownMandate();
        if (msg.sender != b.strategist && msg.sender != arbiter) revert NotStrategist();
        uint256 required = requiredBond(committed);
        if (available(mandateId) < required) {
            revert BondTooSmall(required, available(mandateId));
        }
        b.committed = committed;
        uint64 until = uint64(block.timestamp) + lockFor;
        if (until > b.lockedUntil) b.lockedUntil = until;
        emit Committed(mandateId, committed, required);
    }

    function requiredBond(uint256 committed) public view returns (uint256) {
        uint256 pct = (committed * BOND_BPS_OF_COMMITTED) / 10_000;
        return pct > bondFloor ? pct : bondFloor;
    }

    function available(bytes32 mandateId) public view returns (uint256) {
        Bond storage b = bonds[mandateId];
        return b.posted > b.slashed ? b.posted - b.slashed : 0;
    }

    // --- the challenge game -------------------------------------------------

    /// @notice Claim that a strategist broke their own published rules.
    /// @param slashBps what core/bond.py computed from the rules and the fills.
    /// @param evidence hash of the fills and the audit output, published off
    ///        chain so both sides can recompute it.
    function openClaim(bytes32 mandateId, uint256 slashBps, bytes32 evidence)
        external
        returns (uint256 index)
    {
        Bond storage b = bonds[mandateId];
        if (!b.exists) revert UnknownMandate();
        if (slashBps == 0 || slashBps > 10_000) revert BadSlash();
        if (available(mandateId) == 0) revert NothingToSlash();

        uint256 atRisk = (available(mandateId) * slashBps) / 10_000;
        uint256 stake = (atRisk * CHALLENGE_STAKE_BPS) / 10_000;
        _pull(msg.sender, stake);

        index = claims[mandateId].length;
        claims[mandateId].push(Claim({
            challenger: msg.sender,
            stake: stake,
            slashBps: slashBps,
            evidence: evidence,
            opensAt: uint64(block.timestamp),
            closesAt: uint64(block.timestamp) + challengeWindow,
            disputed: false,
            resolved: false
        }));
        emit ClaimOpened(mandateId, index, msg.sender, slashBps, evidence);
    }

    /// @notice The strategist rejects a claim, sending it to the arbiter.
    function dispute(bytes32 mandateId, uint256 index) external {
        Bond storage b = bonds[mandateId];
        if (!b.exists) revert UnknownMandate();
        if (msg.sender != b.strategist) revert NotStrategist();
        Claim storage c = claims[mandateId][index];
        if (c.resolved) revert AlreadyResolved();
        if (block.timestamp >= c.closesAt) revert WindowClosed();
        c.disputed = true;
        emit ClaimDisputed(mandateId, index);
    }

    /// @notice Execute an unanswered claim once its window has passed.
    /// @dev Permissionless on purpose. A slash that only the operator can
    ///      trigger is a slash the operator can decline to trigger.
    function execute(bytes32 mandateId, uint256 index, address[] calldata harmed,
                     uint256[] calldata weights) external {
        Claim storage c = claims[mandateId][index];
        if (c.resolved) revert AlreadyResolved();
        if (c.disputed) revert NotDisputed();
        if (block.timestamp < c.closesAt) revert WindowOpen();
        _settle(mandateId, index, true, harmed, weights);
    }

    /// @notice Resolve a disputed claim. Only the arbiter named at construction.
    function resolve(bytes32 mandateId, uint256 index, bool upheld,
                     address[] calldata harmed, uint256[] calldata weights) external {
        if (msg.sender != arbiter) revert NotArbiter();
        Claim storage c = claims[mandateId][index];
        if (c.resolved) revert AlreadyResolved();
        if (!c.disputed) revert NotDisputed();
        _settle(mandateId, index, upheld, harmed, weights);
    }

    function _settle(bytes32 mandateId, uint256 index, bool upheld,
                     address[] calldata harmed, uint256[] calldata weights) internal {
        Bond storage b = bonds[mandateId];
        Claim storage c = claims[mandateId][index];
        c.resolved = true;

        if (!upheld) {
            // A false claim pays its stake to the strategist. This is the only
            // thing standing between an honest strategist and a griefer.
            _push(b.strategist, c.stake);
            emit ClaimResolved(mandateId, index, false, 0);
            return;
        }

        uint256 slashed = (available(mandateId) * c.slashBps) / 10_000;
        b.slashed += slashed;

        // The challenger gets their stake back for doing the work.
        _push(c.challenger, c.stake);

        // The slash goes to the followers who were harmed, in proportion to the
        // exposure they had. Not to a treasury: the harmed party is the follower.
        uint256 total;
        for (uint256 i; i < weights.length; ++i) total += weights[i];
        if (total == 0 || harmed.length != weights.length) {
            // Nobody named, so it stays in the contract rather than being
            // silently redirected somewhere it was never owed.
            emit ClaimResolved(mandateId, index, true, slashed);
            return;
        }
        uint256 paid;
        for (uint256 i; i < harmed.length; ++i) {
            uint256 cut = i == harmed.length - 1
                ? slashed - paid                       // last takes the dust
                : (slashed * weights[i]) / total;
            paid += cut;
            _push(harmed[i], cut);
            emit Paid(mandateId, harmed[i], cut);
        }
        emit ClaimResolved(mandateId, index, true, slashed);
    }

    // --- withdrawal ---------------------------------------------------------

    /// @notice Take back what is left, once no follower is exposed.
    function withdraw(bytes32 mandateId, uint256 amount) external {
        Bond storage b = bonds[mandateId];
        if (!b.exists) revert UnknownMandate();
        if (msg.sender != b.strategist) revert NotStrategist();
        if (block.timestamp < b.lockedUntil) revert BondLocked();
        if (_openClaims(mandateId)) revert WindowOpen();
        if (amount > available(mandateId)) revert BondTooSmall(amount, available(mandateId));
        b.posted -= amount;
        _push(msg.sender, amount);
        emit Withdrawn(mandateId, amount);
    }

    function _openClaims(bytes32 mandateId) internal view returns (bool) {
        Claim[] storage cs = claims[mandateId];
        for (uint256 i; i < cs.length; ++i) {
            if (!cs[i].resolved) return true;
        }
        return false;
    }

    function claimCount(bytes32 mandateId) external view returns (uint256) {
        return claims[mandateId].length;
    }

    // --- token plumbing -----------------------------------------------------

    function _pull(address from, uint256 amount) internal {
        (bool ok, bytes memory data) = bondToken.call(
            abi.encodeWithSelector(0x23b872dd, from, address(this), amount)); // transferFrom
        if (!ok || (data.length != 0 && !abi.decode(data, (bool)))) revert TransferFailed();
    }

    function _push(address to, uint256 amount) internal {
        if (amount == 0) return;
        (bool ok, bytes memory data) = bondToken.call(
            abi.encodeWithSelector(0xa9059cbb, to, amount)); // transfer
        if (!ok || (data.length != 0 && !abi.decode(data, (bool)))) revert TransferFailed();
    }
}
