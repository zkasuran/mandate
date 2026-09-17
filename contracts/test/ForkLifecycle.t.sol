// SPDX-License-Identifier: Apache-2.0
pragma solidity ^0.8.24;

import {Test} from "forge-std/Test.sol";
import {MandateBond} from "../src/MandateBond.sol";

interface IERC20 {
    function symbol() external view returns (string memory);
    function decimals() external view returns (uint8);
    function balanceOf(address) external view returns (uint256);
    function approve(address, uint256) external returns (bool);
}

/// The whole bond lifecycle against real Robinhood Chain state, with the real
/// USDG contract as the bond asset.
///
/// The unit suite proves the logic with a mock token. That is not the same as
/// proving it works against a token somebody else wrote, on a chain somebody
/// else runs, where the ERC20 may not return a bool, may take a fee, may
/// revert in a way a mock never would. This runs the real thing.
///
/// Requires a local fork:
///     anvil --fork-url https://rpc.mainnet.chain.robinhood.com
///     forge test --match-path test/ForkLifecycle.t.sol --fork-url http://127.0.0.1:8545
///
/// It skips itself when no fork is reachable, so the default `forge test` stays
/// offline and a missing network is never mistaken for a failure.
contract ForkLifecycleTest is Test {
    // Read off chain 4663 by scripts/verify_addresses.py, never from a doc.
    address constant USDG = 0x5fc5360D0400a0Fd4f2af552ADD042D716F1d168;
    address constant NVDA = 0xd0601CE157Db5bdC3162BbaC2a2C8aF5320D9EEC;
    uint256 constant CHAIN_ID = 4663;

    MandateBond bond;
    IERC20 usdg;

    address strategist = address(0xA11CE);
    address challenger = address(0xB0B);
    address arbiter = address(0xC0FFEE);
    address followerOne = address(0xD00D1);
    address followerTwo = address(0xD00D2);

    bytes32 constant MANDATE =
        0x304fc030a6c2bbf42cd15d5ceadfc5f9ce618ade79d318ab719e23d8727d5b6b;
    bytes32 constant RULES = keccak256("canonical rules");

    uint256 constant FLOOR = 250e6;      // USDG is 6 decimals, checked below
    uint64 constant WINDOW = 2 days;

    /// Reports as SKIPPED offline rather than PASSED. A test that returns early
    /// and still says pass is a green tick for work that never ran, which is
    /// the reporting failure this project exists to argue against.
    modifier onFork() {
        vm.skip(block.chainid != CHAIN_ID);
        _;
    }

    function setUp() public {
        if (block.chainid != CHAIN_ID) return;
        usdg = IERC20(USDG);
        bond = new MandateBond(USDG, arbiter, FLOOR, WINDOW);
    }

    /// The bond asset is the token this chain actually settles in, at the
    /// decimals it actually uses. A hardcoded 18 here would misprice every
    /// bond by twelve orders of magnitude.
    function test_fork_the_bond_asset_is_the_real_usdg() public onFork {
        assertEq(usdg.symbol(), "USDG");
        assertEq(usdg.decimals(), 6);
        assertEq(bond.bondToken(), USDG);
    }

    function test_fork_the_equity_contract_is_live() public onFork {
        assertEq(IERC20(NVDA).symbol(), "NVDA");
        assertEq(IERC20(NVDA).decimals(), 18);
    }

    function _fund(address who, uint256 amount) internal {
        deal(USDG, who, amount);
        vm.prank(who);
        usdg.approve(address(bond), type(uint256).max);
    }

    /// Publish, bond, take followers, get caught, pay the harmed. End to end,
    /// through the real token's own transfer and transferFrom.
    function test_fork_full_lifecycle() public onFork {
        _fund(strategist, 5_000e6);
        _fund(challenger, 1_000e6);

        // 1. publish: the rules hash is now immutable onchain
        vm.startPrank(strategist);
        bond.publish(MANDATE, RULES);
        bond.post(MANDATE, 1_000e6);
        vm.stopPrank();
        assertEq(bond.rulesHash(MANDATE), RULES);
        assertEq(usdg.balanceOf(address(bond)), 1_000e6);

        // 2. followers arrive, so the bond locks and must cover them
        vm.prank(strategist);
        bond.setCommitted(MANDATE, 20_000e6, 30 days);  // needs 2% = 400
        vm.prank(strategist);
        vm.expectRevert(MandateBond.BondLocked.selector);
        bond.withdraw(MANDATE, 1e6);

        // 3. the strategist breaks their published rules. A challenger stakes
        //    their own money on the claim core/bond.py computed.
        uint256 challengerBefore = usdg.balanceOf(challenger);
        vm.prank(challenger);
        uint256 claimId = bond.openClaim(MANDATE, 5_000, keccak256("fills+audit"));
        assertEq(challengerBefore - usdg.balanceOf(challenger), 50e6); // 10% of 500

        // 4. the strategist does not dispute, so anyone may execute it
        address[] memory harmed = new address[](2);
        uint256[] memory weights = new uint256[](2);
        harmed[0] = followerOne; weights[0] = 3;
        harmed[1] = followerTwo; weights[1] = 1;

        vm.warp(block.timestamp + WINDOW + 1);
        vm.prank(address(0xBEEF));            // a stranger, on purpose
        bond.execute(MANDATE, claimId, harmed, weights);

        // 5. the harmed followers hold the slash, in proportion to exposure
        assertEq(usdg.balanceOf(followerOne), 375e6);
        assertEq(usdg.balanceOf(followerTwo), 125e6);
        assertEq(bond.available(MANDATE), 500e6);
        // the challenger is made whole for doing the work
        assertEq(usdg.balanceOf(challenger), challengerBefore);
    }

    function test_fork_a_false_claim_pays_the_strategist() public onFork {
        _fund(strategist, 2_000e6);
        _fund(challenger, 1_000e6);

        vm.startPrank(strategist);
        bond.publish(MANDATE, RULES);
        bond.post(MANDATE, 1_000e6);
        vm.stopPrank();

        uint256 strategistBefore = usdg.balanceOf(strategist);
        vm.prank(challenger);
        uint256 claimId = bond.openClaim(MANDATE, 5_000, keccak256("bad claim"));
        vm.prank(strategist);
        bond.dispute(MANDATE, claimId);

        address[] memory harmed = new address[](0);
        uint256[] memory weights = new uint256[](0);
        vm.prank(arbiter);
        bond.resolve(MANDATE, claimId, false, harmed, weights);

        assertEq(usdg.balanceOf(strategist), strategistBefore + 50e6);
        assertEq(bond.available(MANDATE), 1_000e6);   // bond untouched
    }

    function test_fork_bond_must_cover_the_followers_taken_on() public onFork {
        _fund(strategist, 5_000e6);
        vm.startPrank(strategist);
        bond.publish(MANDATE, RULES);
        bond.post(MANDATE, 300e6);
        vm.expectRevert(
            abi.encodeWithSelector(MandateBond.BondTooSmall.selector, 2_000e6, 300e6));
        bond.setCommitted(MANDATE, 100_000e6, 30 days);
        vm.stopPrank();
    }
}
