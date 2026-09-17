// SPDX-License-Identifier: Apache-2.0
// The browser and the Python package must agree on a mandate id. Otherwise the
// same strategy has two identities and the whole "rules are the id" claim is void.
//
// It also checks ERC20 string decoding, because the two implementations once
// disagreed there: these token names carry a U+2022 bullet. Decoding byte
// by byte rendered it as mojibake in the browser while Python was fine. That
// stayed invisible until the page was opened, so it is gated here now.
//
// Run with: node scripts/cross_check_id.mjs   (compared against Python by
// scripts/cross_check_id.sh)
import { buildRules, canonical, mandateId, decodeAbiString } from '../web/mandate.js';

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

// An abi-encoded "NVIDIA \u2022 Robinhood Token", as the chain returns it.
const NAME_BLOB =
  '0x0000000000000000000000000000000000000000000000000000000000000020'
  + '000000000000000000000000000000000000000000000000000000000000001a'
  + '4e564944494120e280a220526f62696e686f6f6420546f6b656e00000000000000';
console.log(decodeAbiString(NAME_BLOB));
