// SPDX-License-Identifier: Apache-2.0
pragma solidity ^0.8.24;

import {Test} from "forge-std/Test.sol";
import {MandateBond} from "../src/MandateBond.sol";

/// Minimal ERC20 for the bond asset. Enough surface for the contract under
/// test, no more, so a failure here is a failure in MandateBond.
contract MockToken {
    mapping(address => uint256) public balanceOf;
    mapping(address => mapping(address => uint256)) public allowance;

    function mint(address to, uint256 amount) external {
        balanceOf[to] += amount;
    }

    function approve(address spender, uint256 amount) external returns (bool) {
        allowance[msg.sender][spender] = amount;
        return true;
    }

    function transfer(address to, uint256 amount) external returns (bool) {
        require(balanceOf[msg.sender] >= amount, "balance");
        balanceOf[msg.sender] -= amount;
        balanceOf[to] += amount;
        return true;
    }

    function transferFrom(address from, address to, uint256 amount) external returns (bool) {
        require(balanceOf[from] >= amount, "balance");
        require(allowance[from][msg.sender] >= amount, "allowance");
        allowance[from][msg.sender] -= amount;
        balanceOf[from] -= amount;
        balanceOf[to] += amount;
        return true;
    }
}

contract MandateBondTest is Test {
    MandateBond bond;
    MockToken token;

    address strategist = address(0xA1);
    address challenger = address(0xB2);
    address arbiter = address(0xC3);
    address followerOne = address(0xD4);
    address followerTwo = address(0xD5);

    bytes32 constant MANDATE = keccak256("mnd_test");
    bytes32 constant RULES = keccak256("rules");
    bytes32 constant EVIDENCE = keccak256("fills+audit");

    uint256 constant FLOOR = 250e18;
    uint64 constant WINDOW = 2 days;

    function setUp() public {
        token = new MockToken();
        bond = new MandateBond(address(token), arbiter, FLOOR, WINDOW);
        for (uint160 i = 0xA1; i <= 0xD5; ++i) {
            token.mint(address(i), 1_000_000e18);
            vm.prank(address(i));
            token.approve(address(bond), type(uint256).max);
        }
    }

    function _publishAndPost(uint256 amount) internal {
        vm.startPrank(strategist);
        bond.publish(MANDATE, RULES);
        bond.post(MANDATE, amount);
        vm.stopPrank();
    }

    // --- publication --------------------------------------------------------

    function test_publish_stores_the_rules_hash() public {
        vm.prank(strategist);
        bond.publish(MANDATE, RULES);
        assertEq(bond.rulesHash(MANDATE), RULES);
    }

    function test_a_mandate_cannot_be_republished() public {
        vm.startPrank(strategist);
        bond.publish(MANDATE, RULES);
        vm.expectRevert(MandateBond.AlreadyPublished.selector);
        bond.publish(MANDATE, keccak256("different rules"));
        vm.stopPrank();
    }

    function test_rules_cannot_be_repointed_by_anyone_else() public {
        vm.prank(strategist);
        bond.publish(MANDATE, RULES);
        vm.prank(challenger);
        vm.expectRevert(MandateBond.AlreadyPublished.selector);
        bond.publish(MANDATE, keccak256("attacker rules"));
        assertEq(bond.rulesHash(MANDATE), RULES);
    }

    // --- bond sizing --------------------------------------------------------

    function test_floor_applies_below_the_percentage() public view {
        assertEq(bond.requiredBond(1_000e18), FLOOR);
    }

    function test_percentage_applies_above_the_floor() public view {
        assertEq(bond.requiredBond(100_000e18), 2_000e18);
    }

    function test_committing_more_than_the_bond_covers_is_refused() public {
        _publishAndPost(300e18);
        vm.prank(strategist);
        vm.expectRevert(
            abi.encodeWithSelector(MandateBond.BondTooSmall.selector, 2_000e18, 300e18));
        bond.setCommitted(MANDATE, 100_000e18, 30 days);
    }

    function test_committing_within_cover_locks_the_bond() public {
        _publishAndPost(300e18);
        vm.prank(strategist);
        bond.setCommitted(MANDATE, 5_000e18, 30 days);
        vm.prank(strategist);
        vm.expectRevert(MandateBond.BondLocked.selector);
        bond.withdraw(MANDATE, 1e18);
    }

    // --- the challenge game -------------------------------------------------

    function test_a_claim_costs_the_challenger_a_stake() public {
        _publishAndPost(1_000e18);
        uint256 before = token.balanceOf(challenger);
        vm.prank(challenger);
        bond.openClaim(MANDATE, 5_000, EVIDENCE); // 50% of 1000 = 500, stake 10% = 50
        assertEq(before - token.balanceOf(challenger), 50e18);
    }

    function test_an_unanswered_claim_executes_after_the_window() public {
        _publishAndPost(1_000e18);
        vm.prank(challenger);
        uint256 i = bond.openClaim(MANDATE, 5_000, EVIDENCE);

        address[] memory harmed = new address[](1);
        uint256[] memory weights = new uint256[](1);
        harmed[0] = followerOne;
        weights[0] = 1;

        vm.warp(block.timestamp + WINDOW + 1);
        bond.execute(MANDATE, i, harmed, weights);

        assertEq(token.balanceOf(followerOne), 1_000_000e18 + 500e18);
        assertEq(bond.available(MANDATE), 500e18);
    }

    function test_a_claim_cannot_execute_before_its_window_closes() public {
        _publishAndPost(1_000e18);
        vm.prank(challenger);
        uint256 i = bond.openClaim(MANDATE, 5_000, EVIDENCE);
        address[] memory harmed = new address[](0);
        uint256[] memory weights = new uint256[](0);
        vm.expectRevert(MandateBond.WindowOpen.selector);
        bond.execute(MANDATE, i, harmed, weights);
    }

    function test_the_slash_splits_by_exposure() public {
        _publishAndPost(1_000e18);
        vm.prank(challenger);
        uint256 i = bond.openClaim(MANDATE, 10_000, EVIDENCE); // the whole bond

        address[] memory harmed = new address[](2);
        uint256[] memory weights = new uint256[](2);
        harmed[0] = followerOne; weights[0] = 3;
        harmed[1] = followerTwo; weights[1] = 1;

        vm.warp(block.timestamp + WINDOW + 1);
        bond.execute(MANDATE, i, harmed, weights);

        assertEq(token.balanceOf(followerOne), 1_000_000e18 + 750e18);
        assertEq(token.balanceOf(followerTwo), 1_000_000e18 + 250e18);
        assertEq(bond.available(MANDATE), 0);
    }

    function test_no_dust_is_lost_in_the_split() public {
        _publishAndPost(1_000e18 + 7); // deliberately not divisible
        vm.prank(challenger);
        uint256 i = bond.openClaim(MANDATE, 10_000, EVIDENCE);

        address[] memory harmed = new address[](3);
        uint256[] memory weights = new uint256[](3);
        harmed[0] = followerOne; weights[0] = 1;
        harmed[1] = followerTwo; weights[1] = 1;
        harmed[2] = address(0xD6); weights[2] = 1;

        uint256 slashable = bond.available(MANDATE);
        vm.warp(block.timestamp + WINDOW + 1);
        bond.execute(MANDATE, i, harmed, weights);

        uint256 paid = (token.balanceOf(followerOne) - 1_000_000e18)
            + (token.balanceOf(followerTwo) - 1_000_000e18)
            + token.balanceOf(address(0xD6));
        assertEq(paid, slashable);
    }

    function test_a_disputed_claim_cannot_be_executed_by_anyone() public {
        _publishAndPost(1_000e18);
        vm.prank(challenger);
        uint256 i = bond.openClaim(MANDATE, 5_000, EVIDENCE);
        vm.prank(strategist);
        bond.dispute(MANDATE, i);

        address[] memory harmed = new address[](0);
        uint256[] memory weights = new uint256[](0);
        vm.warp(block.timestamp + WINDOW + 1);
        vm.expectRevert(MandateBond.NotDisputed.selector);
        bond.execute(MANDATE, i, harmed, weights);
    }

    function test_a_false_claim_pays_its_stake_to_the_strategist() public {
        _publishAndPost(1_000e18);
        uint256 before = token.balanceOf(strategist);
        vm.prank(challenger);
        uint256 i = bond.openClaim(MANDATE, 5_000, EVIDENCE);
        vm.prank(strategist);
        bond.dispute(MANDATE, i);

        address[] memory harmed = new address[](0);
        uint256[] memory weights = new uint256[](0);
        vm.prank(arbiter);
        bond.resolve(MANDATE, i, false, harmed, weights);

        assertEq(token.balanceOf(strategist), before + 50e18);
        assertEq(bond.available(MANDATE), 1_000e18); // untouched
    }

    function test_only_the_arbiter_resolves_a_dispute() public {
        _publishAndPost(1_000e18);
        vm.prank(challenger);
        uint256 i = bond.openClaim(MANDATE, 5_000, EVIDENCE);
        vm.prank(strategist);
        bond.dispute(MANDATE, i);

        address[] memory harmed = new address[](0);
        uint256[] memory weights = new uint256[](0);
        vm.prank(challenger);
        vm.expectRevert(MandateBond.NotArbiter.selector);
        bond.resolve(MANDATE, i, true, harmed, weights);
    }

    function test_only_the_strategist_disputes() public {
        _publishAndPost(1_000e18);
        vm.prank(challenger);
        uint256 i = bond.openClaim(MANDATE, 5_000, EVIDENCE);
        vm.prank(challenger);
        vm.expectRevert(MandateBond.NotStrategist.selector);
        bond.dispute(MANDATE, i);
    }

    function test_a_claim_cannot_be_settled_twice() public {
        _publishAndPost(1_000e18);
        vm.prank(challenger);
        uint256 i = bond.openClaim(MANDATE, 5_000, EVIDENCE);
        address[] memory harmed = new address[](0);
        uint256[] memory weights = new uint256[](0);
        vm.warp(block.timestamp + WINDOW + 1);
        bond.execute(MANDATE, i, harmed, weights);
        vm.expectRevert(MandateBond.AlreadyResolved.selector);
        bond.execute(MANDATE, i, harmed, weights);
    }

    function test_a_slash_over_the_whole_bond_is_refused() public {
        _publishAndPost(1_000e18);
        vm.prank(challenger);
        vm.expectRevert(MandateBond.BadSlash.selector);
        bond.openClaim(MANDATE, 10_001, EVIDENCE);
    }

    // --- withdrawal ---------------------------------------------------------

    function test_withdrawal_is_blocked_while_a_claim_is_open() public {
        _publishAndPost(1_000e18);
        vm.prank(challenger);
        bond.openClaim(MANDATE, 5_000, EVIDENCE);
        vm.prank(strategist);
        vm.expectRevert(MandateBond.WindowOpen.selector);
        bond.withdraw(MANDATE, 1e18);
    }

    function test_withdrawal_after_the_lock_and_with_no_claims() public {
        _publishAndPost(1_000e18);
        vm.prank(strategist);
        bond.setCommitted(MANDATE, 5_000e18, 1 days);
        vm.warp(block.timestamp + 2 days);
        uint256 before = token.balanceOf(strategist);
        vm.prank(strategist);
        bond.withdraw(MANDATE, 400e18);
        assertEq(token.balanceOf(strategist), before + 400e18);
    }

    function test_only_the_strategist_withdraws() public {
        _publishAndPost(1_000e18);
        vm.prank(challenger);
        vm.expectRevert(MandateBond.NotStrategist.selector);
        bond.withdraw(MANDATE, 1e18);
    }

    function test_cannot_withdraw_more_than_remains_after_a_slash() public {
        _publishAndPost(1_000e18);
        vm.prank(challenger);
        uint256 i = bond.openClaim(MANDATE, 5_000, EVIDENCE);
        address[] memory harmed = new address[](1);
        uint256[] memory weights = new uint256[](1);
        harmed[0] = followerOne; weights[0] = 1;
        vm.warp(block.timestamp + WINDOW + 1);
        bond.execute(MANDATE, i, harmed, weights);

        vm.prank(strategist);
        vm.expectRevert(
            abi.encodeWithSelector(MandateBond.BondTooSmall.selector, 600e18, 500e18));
        bond.withdraw(MANDATE, 600e18);
    }

    // --- fuzz ---------------------------------------------------------------

    function testFuzz_a_slash_never_exceeds_the_bond(uint256 posted, uint16 slashBps) public {
        posted = bound(posted, 1e18, 500_000e18);
        slashBps = uint16(bound(slashBps, 1, 10_000));
        _publishAndPost(posted);

        vm.prank(challenger);
        uint256 i = bond.openClaim(MANDATE, slashBps, EVIDENCE);
        address[] memory harmed = new address[](1);
        uint256[] memory weights = new uint256[](1);
        harmed[0] = followerOne; weights[0] = 1;

        vm.warp(block.timestamp + WINDOW + 1);
        bond.execute(MANDATE, i, harmed, weights);
        assertLe(bond.available(MANDATE), posted);
    }

    function testFuzz_required_bond_never_drops_below_the_floor(uint256 committed) public view {
        committed = bound(committed, 0, 1e30);
        assertGe(bond.requiredBond(committed), FLOOR);
    }
}
