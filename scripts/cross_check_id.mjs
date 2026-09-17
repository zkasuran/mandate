// SPDX-License-Identifier: Apache-2.0
// The browser and the Python package must agree on a mandate id, or the same
// strategy has two identities and the whole "rules are the id" claim is void.
// Run with: node scripts/cross_check_id.mjs   (compared against Python by
// scripts/cross_check_id.sh)
import { buildRules, canonical, mandateId } from '../web/mandate.js';

const rules = buildRules({
  legs: [
    { symbol: 'NVDA', token: '0xd0601CE157Db5bdC3162BbaC2a2C8aF5320D9EEC', weight_bps: 4000 },
    { symbol: 'META', token: '0xAAAA000000000000000000000000000000000001', weight_bps: 3000 },
    { symbol: 'SPY', token: '0xBBBB000000000000000000000000000000000002', weight_bps: 3000 }],
  execution: { kind: 'twap', slices: 4, interval_seconds: 900, max_slippage_bps: 50,
               min_depth_multiple: 3, require_market_hours: false },
  risk: { stop_loss_bps: 800, take_profit_bps: 1500, max_position_bps: 10000,
          max_daily_orders: 12 },
  feeBps: 100, chainId: 4663,
});
console.log(canonical(rules));
console.log(await mandateId(rules));
