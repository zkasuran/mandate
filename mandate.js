// SPDX-License-Identifier: Apache-2.0
// Mandate, running entirely in the browser.
//
// Both chains and the Flash API serve access-control-allow-origin: *, so the
// whole product runs client side with no backend. Nothing can go down during
// judging except the static host. The Python package in this repo is the
// reference implementation and the CLI. This file is the same logic, so the
// mandate id it produces is byte-identical: both hash the same canonical JSON.

export const CHAINS = {
  robinhood: {
    key: 'robinhood', name: 'Robinhood Chain', chainId: 4663,
    // Browser-reachable endpoints only. core/chains.py carries a longer list
    // because Python is not subject to CORS. rpc.arrowrpc.com answers fine from
    // a server and sends no access-control header, so putting it in this
    // rotation emptied the ghost scan from a page while passing in node.
    // Checked with an OPTIONS preflight, not assumed.
    rpc: ['https://robinhood-rpc.publicnode.com',
          'https://rpc.mainnet.chain.robinhood.com'],
    explorer: 'https://robinscan.io',
    quoteSymbol: 'USDG',
    quote: '0x5fc5360D0400a0Fd4f2af552ADD042D716F1d168',
    quoteDecimals: 6, flash: 'robinhood',
  },
  base: {
    key: 'base', name: 'Base', chainId: 8453,
    // Browser-reachable and stable under load. 1rpc.io/base answers a single
    // request fine but drops its CORS header on the error responses it returns
    // once a scan is in flight, so it is server-side only. A preflight check
    // passed it; the real traffic is what found it.
    rpc: ['https://mainnet.base.org',
          'https://base-rpc.publicnode.com',
          'https://base.drpc.org'],
    explorer: 'https://basescan.org',
    quoteSymbol: 'USDC',
    quote: '0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913',
    quoteDecimals: 6, flash: 'base',
    v3Factory: '0x33128a8fC17869897dcE68Ed026d694621f6FDfD',
  },
};

export const FLASH_URL = 'https://flash.definitive.fi/v1';
// Published by Definitive in their own docs for trade-only use.
export const FLASH_KEY = 'dpka_513a2bd7_57a2_46d2_927b_2a3857fe271b';

export const TICKERS = ['NVDA', 'AAPL', 'TSLA', 'SPY', 'GOOGL', 'META', 'MSFT', 'QQQ'];

// Selectors. Verified against the chain by scripts/verify_addresses.py.
const SEL = {
  name: '0x06fdde03', symbol: '0x95d89b41', decimals: '0x313ce567',
  totalSupply: '0x18160ddd', getPool: '0x1698ee82', slot0: '0x3850c7bd',
  liquidity: '0x1a686502', token0: '0x0dfe1681', token1: '0xd21220a7',
  balanceOf: '0x70a08231',
};

// --- rpc --------------------------------------------------------------------
// A public endpoint rate limits long before a scan finishes. The first version
// of this client had no throttle and swallowed every failure into `null`, which
// downstream read as "this pool does not exist": a scan that found one pool
// where there were four, then looked like a clean result. So transport failure
// and genuine absence are now different things. Only absence returns null.

let rpcId = 0;
let lastCall = 0;
let nextEndpoint = 0;
let inFlight = 0;

// Two separate problems, fixed separately.
//
// Rate limiting. At 60ms a full ghost scan drew 46 rate-limit responses.
// Slowing to 130ms drew 51, because every call started on the same endpoint and
// only moved after being refused. One host absorbed the whole scan while three
// sat idle. Calls now start on a rotating endpoint, which took that to zero.
//
// Latency. A strict minimum gap also serialised every call, so a scan paid the
// full network round trip about seventy times over and took 110 seconds. The
// limit that matters is requests per second per host, not one request at a
// time, so a few are allowed in flight together.
const MIN_GAP_MS = 45;
const MAX_IN_FLIGHT = 5;

const sleep = ms => new Promise(r => setTimeout(r, ms));

async function throttle() {
  while (inFlight >= MAX_IN_FLIGHT) await sleep(15);
  const wait = MIN_GAP_MS - (Date.now() - lastCall);
  if (wait > 0) await sleep(wait);
  lastCall = Date.now();
}

export class RpcError extends Error {}

export async function rpc(chainKey, method, params, { retries = 4 } = {}) {
  const eps = CHAINS[chainKey].rpc;
  const from = nextEndpoint++;
  let last;
  for (let i = 0; i < retries; i++) {
    const url = eps[(from + i) % eps.length];
    await throttle();
    inFlight++;
    try {
      const r = await fetch(url, {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ jsonrpc: '2.0', id: ++rpcId, method, params }),
      });
      // Any non-2xx is the endpoint declining to answer, not the chain
      // answering. Rotating only on 429 and 5xx let a 410 from one host poison
      // a read while three healthy hosts sat idle, which moved a ghost-pool
      // count without reporting a single failure.
      if (!r.ok) {
        last = new RpcError(`${url} returned ${r.status}`);
      } else {
        const out = await r.json();
        // A revert is a real answer from the chain, not a transport problem.
        if (out.error) return { reverted: true, error: out.error };
        return { result: out.result };
      }
    } catch (e) {
      last = e;
    } finally {
      // Released on every path. An increment without a matching decrement
      // would let the window fill and never drain, which is a deadlock rather
      // than a slow page.
      inFlight--;
    }
    await sleep(200 * 2 ** i);
  }
  throw new RpcError(`${method} failed after ${retries} attempts: ${last?.message || last}`);
}

export async function rpcValue(chainKey, method, params) {
  const out = await rpc(chainKey, method, params);
  if (out.reverted) throw new RpcError(`${method}: ${out.error.message || out.error}`);
  return out.result;
}

const callCache = new Map();

/** Returns null only when the call genuinely returned nothing. Transport
 *  failures throw, so a caller can never read an outage as an empty market. */
export async function ethCall(chainKey, to, data, { cache = true } = {}) {
  const key = `${chainKey}:${to}:${data}`;
  if (cache && callCache.has(key)) return callCache.get(key);
  const out = await rpc(chainKey, 'eth_call', [{ to, data }, 'latest']);
  let value;
  if (out.reverted) value = null;
  else value = (out.result === '0x' || out.result === '') ? null : out.result;
  if (cache) callCache.set(key, value);
  return value;
}

const encAddr = a => a.toLowerCase().replace('0x', '').padStart(64, '0');
const encUint = n => BigInt(n).toString(16).padStart(64, '0');
const decUint = (h, w = 0) => BigInt('0x' + (h.slice(2).substr(w * 64, 64) || '0'));

// Decoded as UTF-8, not byte by byte. These names carry a U+2022 bullet
// ("NVIDIA \u2022 Robinhood Token"). String.fromCharCode per byte renders that
// as mojibake. Python got it right, the browser did not, then it took opening
// the page to see it.
const UTF8 = new TextDecoder('utf-8');

function hexToBytes(hex) {
  const out = new Uint8Array(hex.length / 2);
  for (let i = 0; i < out.length; i++) out[i] = parseInt(hex.substr(i * 2, 2), 16);
  return out;
}

export function decodeAbiString(h) {
  if (!h) return null;
  const b = h.slice(2);
  // Short returns are the old bytes32 style: right-padded with zeros.
  if (b.length < 128) {
    return UTF8.decode(hexToBytes(b)).replace(/\u0000+$/, '');
  }
  const len = Number(BigInt('0x' + b.substr(64, 64)));
  if (!len || 128 + len * 2 > b.length) {
    return UTF8.decode(hexToBytes(b.substr(0, 64))).replace(/\u0000+$/, '');
  }
  return UTF8.decode(hexToBytes(b.substr(128, len * 2)));
}

export async function erc20(chainKey, addr) {
  const [sym, nm, dec, sup] = await Promise.all([
    ethCall(chainKey, addr, SEL.symbol),
    ethCall(chainKey, addr, SEL.name),
    ethCall(chainKey, addr, SEL.decimals),
    ethCall(chainKey, addr, SEL.totalSupply),
  ]);
  const code = await rpcValue(chainKey, 'eth_getCode', [addr, 'latest']);
  return {
    address: addr, hasCode: code && code.length > 2,
    symbol: decodeAbiString(sym), name: decodeAbiString(nm),
    decimals: dec ? Number(decUint(dec)) : null,
    totalSupply: sup ? decUint(sup) : null,
  };
}

// --- flash ------------------------------------------------------------------

async function flashFetch(path, body) {
  const r = await fetch(FLASH_URL + path, {
    method: body ? 'POST' : 'GET',
    headers: { 'x-definitive-api-key': FLASH_KEY, 'content-type': 'application/json' },
    body: body ? JSON.stringify(body) : undefined,
  });
  const out = await r.json();
  if (!r.ok) {
    const e = out.error || {};
    const err = new Error(e.message || `HTTP ${r.status}`);
    err.code = e.code; err.details = e.details; throw err;
  }
  return out;
}

export const flashSearch = (q, chain, limit = 25) =>
  flashFetch(`/search?query=${encodeURIComponent(q)}&chain=${chain}&limit=${limit}`)
    .then(d => d.assets || []);

export const flashQuote = body => flashFetch('/quote', body);

// --- ticker resolution ------------------------------------------------------
// The venue's index returns squatters next to the real asset. NVDA returns
// NVDAx3L, a 3x leveraged token holding $407,605. AAPL returns AAPLCAT. One
// token's symbol is a comma-joined list of thousands of tickers so it matches
// every search. Resolving by "first result" hands over the wrong contract.

export const MIN_LIQUIDITY_USD = 50_000;
export const MIN_HOLDERS = 1_000;

export function resolveFrom(ticker, rows) {
  const want = ticker.trim().toUpperCase();
  const impostors = [], exact = [];
  for (const a of rows) {
    if ((a.symbol || '').toUpperCase() === want) exact.push(a);
    else impostors.push({ a, why: 'symbol does not match the requested ticker' });
  }
  const viable = [];
  for (const a of exact) {
    const liq = Number(a.liquidity || 0), hold = Number(a.holders || 0);
    if (a.riskFlagged) impostors.push({ a, why: 'exact symbol but flagged risky by the venue' });
    else if (liq < MIN_LIQUIDITY_USD) impostors.push({ a, why: `exact symbol but only $${liq.toLocaleString()} of liquidity` });
    else if (hold < MIN_HOLDERS) impostors.push({ a, why: `exact symbol but only ${hold.toLocaleString()} holders` });
    else viable.push(a);
  }
  viable.sort((x, y) => Number(y.liquidity || 0) - Number(x.liquidity || 0));
  viable.slice(1).forEach(a => impostors.push({ a, why: 'second contract with the same symbol' }));
  if (!viable.length) {
    return {
      ok: false, query: want, chosen: null, impostors,
      reason: `no contract matches ${want} with at least $${MIN_LIQUIDITY_USD.toLocaleString()} liquidity and ${MIN_HOLDERS.toLocaleString()} holders`,
    };
  }
  const best = viable[0];
  return {
    ok: true, query: want, chosen: best, impostors,
    reason: `exact symbol match with $${Number(best.liquidity).toLocaleString(undefined, { maximumFractionDigits: 0 })} liquidity and ${Number(best.holders).toLocaleString()} holders`,
  };
}

export async function resolveInstrument(ticker, chainKey = 'robinhood') {
  const rows = await flashSearch(ticker, chainKey);
  const res = resolveFrom(ticker, rows);
  const impostors = res.impostors.map(({ a, why }) => ({
    symbol: (a.symbol || '').slice(0, 60), address: a.address,
    liquidity: Number(a.liquidity || 0), holders: Number(a.holders || 0), why,
  }));
  if (!res.ok) {
    return { ticker: ticker.toUpperCase(), chainKey, verified: false,
             notes: [res.reason], impostors };
  }
  const a = res.chosen;
  const token = await erc20(chainKey, a.address);
  const notes = [];
  let verified = true;
  if (!token.hasCode) { verified = false; notes.push('no contract code at the address the venue returned'); }
  else if ((token.symbol || '').toUpperCase() !== ticker.toUpperCase()) {
    verified = false;
    notes.push(`chain reports symbol "${token.symbol}", venue indexed it as "${a.symbol}"`);
  }
  return {
    ticker: ticker.toUpperCase(), chainKey, address: a.address,
    symbolOnchain: token.symbol, name: token.name || a.name,
    decimals: token.decimals ?? a.decimals,
    price: Number(a.price), liquidity: Number(a.liquidity),
    volume24h: Number(a.volume24h || 0), holders: Number(a.holders || 0),
    verified, notes, impostors, reason: res.reason,
  };
}

export async function buildUniverse(tickers = TICKERS, chainKey = 'robinhood', onEach) {
  const out = {};
  for (const t of tickers) {
    try { out[t] = await resolveInstrument(t, chainKey); }
    catch (e) { out[t] = { ticker: t, chainKey, verified: false, notes: [String(e.message || e)], impostors: [] }; }
    if (onEach) onEach(t, out[t]);
  }
  return out;
}

// --- mandate identity -------------------------------------------------------
// Must agree byte for byte with core/spec.py: JSON, sorted keys, no spaces.

export function canonical(value) {
  if (value === null || typeof value !== 'object') return JSON.stringify(value);
  if (Array.isArray(value)) return '[' + value.map(canonical).join(',') + ']';
  return '{' + Object.keys(value).sort()
    .map(k => JSON.stringify(k) + ':' + canonical(value[k])).join(',') + '}';
}

export async function mandateId(rules) {
  const bytes = new TextEncoder().encode(canonical(rules));
  const digest = await crypto.subtle.digest('SHA-256', bytes);
  return 'mnd_' + [...new Uint8Array(digest)]
    .map(b => b.toString(16).padStart(2, '0')).join('');
}

export function buildRules({ legs, execution, risk, feeBps, chainId = 4663,
                             cadenceSeconds = 86400, trigger = 'schedule' }) {
  return {
    schema: 'mandate/v1', version: 1, chain_id: chainId, trigger,
    cadence_seconds: cadenceSeconds, fee_bps: feeBps,
    legs: [...legs].sort((a, b) => a.token.toLowerCase() < b.token.toLowerCase() ? -1 : 1)
      .map(l => ({ symbol: l.symbol, token: l.token.toLowerCase(), weight_bps: l.weight_bps })),
    execution: {
      kind: execution.kind, slices: execution.slices,
      interval_seconds: execution.interval_seconds,
      max_slippage_bps: execution.max_slippage_bps,
      min_depth_multiple: execution.min_depth_multiple,
      require_market_hours: execution.require_market_hours,
    },
    risk: {
      stop_loss_bps: risk.stop_loss_bps, take_profit_bps: risk.take_profit_bps,
      max_position_bps: risk.max_position_bps, max_daily_orders: risk.max_daily_orders,
    },
  };
}

export function signingMessage(id, name, strategist, chainId, feeBps) {
  return `Mandate publication\nid: ${id}\nname: ${name}\n`
    + `strategist: ${strategist.toLowerCase()}\nchain: ${chainId}\n`
    + `fee_bps: ${feeBps}\nI am publishing this strategy. The rules above are `
    + `fixed and cannot be changed without producing a new id.`;
}

// --- market session ---------------------------------------------------------

const HOLIDAYS_2026 = new Set([
  '2026-01-01', '2026-01-19', '2026-02-16', '2026-04-03', '2026-05-25',
  '2026-06-19', '2026-07-03', '2026-09-07', '2026-11-26', '2026-12-25']);
const HALF_DAYS_2026 = new Set(['2026-11-27', '2026-12-24']);

export function sessionState(now = new Date()) {
  const fmt = new Intl.DateTimeFormat('en-CA', {
    timeZone: 'America/New_York', year: 'numeric', month: '2-digit',
    day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false,
    weekday: 'short',
  });
  const parts = Object.fromEntries(fmt.formatToParts(now).map(p => [p.type, p.value]));
  const date = `${parts.year}-${parts.month}-${parts.day}`;
  const mins = Number(parts.hour) * 60 + Number(parts.minute);
  const weekend = parts.weekday === 'Sat' || parts.weekday === 'Sun';
  const close = HALF_DAYS_2026.has(date) ? 13 * 60 : 16 * 60;

  if (HOLIDAYS_2026.has(date)) return { open: false, reason: 'exchange holiday', date };
  if (weekend) return { open: false, reason: 'weekend', date };
  if (mins < 9 * 60 + 30) return { open: false, reason: 'before the opening bell', date };
  if (mins >= close) return { open: false, reason: 'after the closing bell', date };
  return { open: true, reason: 'regular session', date };
}

// --- planning ---------------------------------------------------------------

export function splitNotional(total, slices) {
  const base = Math.floor(total / slices), rem = total % slices;
  return Array.from({ length: slices }, (_, i) => base + (i < rem ? 1 : 0));
}

export function depthAwareSlices(sleeveUsd, depthUsd, requested, cover) {
  if (!depthUsd) return { slices: requested, note: 'venue depth is zero' };
  const maxClip = depthUsd / Math.max(cover, 1);
  const needed = Math.ceil(sleeveUsd / maxClip);
  if (needed > requested) {
    return { slices: needed, note: `widened from ${requested} to ${needed} clips: `
      + `venue depth $${depthUsd.toLocaleString(undefined,{maximumFractionDigits:0})} `
      + `only covers $${maxClip.toLocaleString(undefined,{maximumFractionDigits:0})} per clip at ${cover}x` };
  }
  return { slices: requested, note: null };
}

export function buildPlan(rules, universe, sleeveUsd) {
  const ex = rules.execution, risk = rules.risk;
  const clips = [], protections = [], warnings = [];
  const capped = sleeveUsd * risk.max_position_bps / 10_000;
  if (capped < sleeveUsd) {
    warnings.push(`position capped at ${risk.max_position_bps}bps of the sleeve: `
      + `$${capped.toFixed(2)} of $${sleeveUsd.toFixed(2)}`);
  }
  for (const leg of rules.legs) {
    const inst = universe[leg.symbol.toUpperCase()];
    const legNotional = capped * leg.weight_bps / 10_000;
    if (legNotional <= 0) { warnings.push(`${leg.symbol}: weight rounds to nothing`); continue; }
    let slices = (ex.kind === 'market' || ex.kind === 'limit') ? 1 : ex.slices;
    if (ex.kind === 'twap' && inst && inst.liquidity) {
      const r = depthAwareSlices(legNotional, inst.liquidity, slices, ex.min_depth_multiple);
      slices = r.slices; if (r.note) warnings.push(`${leg.symbol}: ${r.note}`);
    }
    splitNotional(Math.round(legNotional * 1e6), slices).forEach((cents, i) => {
      clips.push({ index: i + 1, total: slices, symbol: leg.symbol.toUpperCase(),
                   spendUsd: cents / 1e6, kind: ex.kind });
    });
    if (inst && inst.price) {
      if (risk.stop_loss_bps) protections.push({ kind: 'stop', symbol: leg.symbol,
        trigger: inst.price * (1 - risk.stop_loss_bps / 10_000), reference: inst.price });
      if (risk.take_profit_bps) protections.push({ kind: 'take_profit', symbol: leg.symbol,
        trigger: inst.price * (1 + risk.take_profit_bps / 10_000), reference: inst.price });
    }
  }
  if (clips.length > risk.max_daily_orders) {
    warnings.push(`plan has ${clips.length} clips against a ${risk.max_daily_orders} daily cap, `
      + 'so it will run across more than one day');
  }
  return { clips, protections, warnings };
}

// --- bond -------------------------------------------------------------------

export const BOND_FLOOR_USD = 250;
export const BOND_BPS_OF_COMMITTED = 200;
export const PENALTY_BPS = {
  slippage_exceeded: 1500, traded_out_of_hours: 2500,
  unverified_instrument: 5000, depth_cover_ignored: 1000, wrong_instrument: 10000,
};

export const requiredBond = committed =>
  Math.max(BOND_FLOOR_USD, committed * BOND_BPS_OF_COMMITTED / 10_000);

export function audit(rules, fills, universe) {
  const allowed = new Set(rules.legs.map(l => l.symbol.toUpperCase()));
  const maxSlip = rules.execution.max_slippage_bps;
  const hoursOnly = rules.execution.require_market_hours;
  const cover = rules.execution.min_depth_multiple;
  const violations = [];
  for (const f of fills) {
    const t = (f.ticker || '').toUpperCase();
    if (!allowed.has(t)) {
      violations.push({ kind: 'wrong_instrument', fill: f.fillId, follower: f.follower,
        detail: `filled ${t}, which is not a leg of this mandate (${[...allowed].sort().join(', ')})`,
        penaltyBps: PENALTY_BPS.wrong_instrument, harmedUsd: f.spendUsd });
      continue;
    }
    const inst = universe[t];
    if (!inst || !inst.verified) {
      violations.push({ kind: 'unverified_instrument', fill: f.fillId, follower: f.follower,
        detail: `filled ${t} at ${f.address}, which did not pass ticker verification`,
        penaltyBps: PENALTY_BPS.unverified_instrument, harmedUsd: f.spendUsd });
      continue;
    }
    if (f.slippageBps != null && f.slippageBps > maxSlip) {
      violations.push({ kind: 'slippage_exceeded', fill: f.fillId, follower: f.follower,
        detail: `filled at ${f.slippageBps}bps against a published ceiling of ${maxSlip}bps`,
        penaltyBps: PENALTY_BPS.slippage_exceeded,
        harmedUsd: f.spendUsd * (f.slippageBps - maxSlip) / 10_000 });
    }
    if (hoursOnly && f.markCertified === false) {
      violations.push({ kind: 'traded_out_of_hours', fill: f.fillId, follower: f.follower,
        detail: 'mandate requires market hours, this fill used an uncertified mark',
        penaltyBps: PENALTY_BPS.traded_out_of_hours, harmedUsd: f.spendUsd });
    }
    if (f.depthCover != null && f.depthCover < cover) {
      violations.push({ kind: 'depth_cover_ignored', fill: f.fillId, follower: f.follower,
        detail: `clip took ${f.depthCover.toFixed(2)}x depth cover against a published ${cover}x`,
        penaltyBps: PENALTY_BPS.depth_cover_ignored, harmedUsd: f.spendUsd });
    }
  }
  const slashBps = Math.min(violations.reduce((a, v) => a + v.penaltyBps, 0), 10_000);
  return { fillsChecked: fills.length, clean: !violations.length, violations, slashBps };
}

export function settle(postedUsd, auditResult) {
  const slashed = postedUsd * auditResult.slashBps / 10_000;
  if (slashed <= 0) return { slashedUsd: 0, payouts: [] };
  const harmed = {};
  for (const v of auditResult.violations) {
    harmed[v.follower] = (harmed[v.follower] || 0) + v.harmedUsd;
  }
  const total = Object.values(harmed).reduce((a, b) => a + b, 0);
  const payouts = Object.entries(harmed)
    .sort((a, b) => b[1] - a[1])
    .map(([follower, harm]) => ({ follower, harmUsd: harm,
      share: harm / total, payoutUsd: slashed * (harm / total) }));
  return { slashedUsd: slashed, payouts };
}

// --- portfolio --------------------------------------------------------------

export function portfolio(fills, marks = {}) {
  const holdings = {};
  for (const f of [...fills].sort((a, b) => (a.at || 0) - (b.at || 0))) {
    const h = holdings[f.ticker] ||= { ticker: f.ticker, address: f.address,
      quantity: 0, costBasis: 0, spentUsd: 0, receivedUsd: 0, realisedPnl: 0, fills: 0 };
    if (f.side === 'buy') {
      const total = h.costBasis * h.quantity + f.spendUsd;
      h.quantity += f.quantity;
      h.costBasis = h.quantity ? total / h.quantity : 0;
      h.spentUsd += f.spendUsd;
    } else {
      const sold = Math.min(f.quantity, h.quantity);
      h.realisedPnl += (f.price - h.costBasis) * sold;
      h.quantity -= sold; h.receivedUsd += f.spendUsd;
      if (h.quantity <= 1e-12) { h.quantity = 0; h.costBasis = 0; }
    }
    h.fills++;
  }
  const rows = Object.values(holdings).map(h => {
    const mark = marks[h.ticker] ?? null;
    const invested = h.costBasis * h.quantity;
    const unreal = mark ? (mark - h.costBasis) * h.quantity : 0;
    return { ...h, mark, invested, marketValue: mark ? mark * h.quantity : null,
             unrealisedPnl: unreal, totalPnl: h.realisedPnl + unreal,
             returnPct: invested ? unreal / invested * 100 : null };
  });
  return {
    holdings: rows,
    marketValue: rows.reduce((a, h) => a + (h.marketValue || 0), 0),
    realisedPnl: rows.reduce((a, h) => a + h.realisedPnl, 0),
    unrealisedPnl: rows.reduce((a, h) => a + h.unrealisedPnl, 0),
    totalPnl: rows.reduce((a, h) => a + h.totalPnl, 0),
    fillCount: fills.length,
  };
}

// --- uniswap v3 ghost pools (Base) ------------------------------------------

export const FEE_TIERS = [100, 500, 3000, 10000];
const ZERO = '0x0000000000000000000000000000000000000000';

export async function getPool(tokenA, tokenB, fee) {
  const data = SEL.getPool + encAddr(tokenA) + encAddr(tokenB) + encUint(fee);
  const raw = await ethCall('base', CHAINS.base.v3Factory, data);
  if (!raw) return null;
  const addr = '0x' + raw.slice(2).substr(0, 64).slice(-40);
  return addr === ZERO ? null : addr;
}

export async function poolReport(pool, base, baseDec, quoteDec = 6) {
  const [slot0, liq, t0, t1] = await Promise.all([
    ethCall('base', pool, SEL.slot0), ethCall('base', pool, SEL.liquidity),
    ethCall('base', pool, SEL.token0), ethCall('base', pool, SEL.token1),
  ]);
  if (!slot0) return null;
  const sqrtP = decUint(slot0, 0);
  let tick = decUint(slot0, 1);
  if (tick >= (1n << 255n)) tick -= (1n << 256n);
  const token0 = '0x' + t0.slice(2).substr(0, 64).slice(-40);
  const token1 = '0x' + t1.slice(2).substr(0, 64).slice(-40);
  const baseIs0 = token0.toLowerCase() === base.toLowerCase();
  const quote = baseIs0 ? token1 : token0;
  const qb = await ethCall('base', quote, SEL.balanceOf + encAddr(pool), { cache: false });
  const quoteHeld = qb ? Number(decUint(qb)) / 1e6 : 0;
  // The decimal adjustment follows token0/token1 ordering, not base/quote.
  // Applying the base's decimals regardless priced AAPLc at $0.03.
  const [dec0, dec1] = baseIs0 ? [baseDec, quoteDec] : [quoteDec, baseDec];
  const ratio = Number(sqrtP) / 2 ** 96;
  const p01 = ratio * ratio * 10 ** dec0 / 10 ** dec1;
  const price = baseIs0 ? p01 : (p01 ? 1 / p01 : 0);
  const liquidity = liq ? decUint(liq) : 0n;
  const reasons = [];
  let status = 'live';
  if (tick <= -800000n || tick >= 800000n) {
    status = 'trapped';
    reasons.push(`tick ${tick} sits at the extreme of the tick range, so the quoted price is not a market`);
  }
  if (liquidity === 0n) { reasons.push('in-range liquidity is zero'); if (status !== 'trapped') status = 'dead'; }
  if (quoteHeld < 1) { reasons.push(`pool holds $${quoteHeld.toFixed(2)} of the quote token`); if (status === 'live') status = 'dead'; }
  else if (quoteHeld < 5000) { reasons.push(`only $${quoteHeld.toLocaleString(undefined,{maximumFractionDigits:2})} of quote token held`); if (status === 'live') status = 'thin'; }
  return { pool, price, tick: Number(tick), liquidity: liquidity.toString(),
           quoteHeld, status, reasons };
}


// --- protective exits -------------------------------------------------------
// Mirrors core/exits.py. A mandate that plans a stop and never places one is a
// mandate with no stop, so the browser places them too rather than showing a
// shorter version of the same run.

export const TRIGGER_DIRECTION = { stop_loss: 'lower', take_profit: 'upper' };
export const EXIT_ORDER_TYPE = { stop_loss: 'stop-loss', take_profit: 'take-profit' };

/** Exits owed on the positions actually held, triggered off the position's own
 *  basis rather than the live mark. Re-deriving from the current price quietly
 *  moves the risk the follower agreed to. */
export function deriveExits(rules, holdings, universe) {
  const risk = rules.risk;
  const exits = [], warnings = [];
  if (!risk.stop_loss_bps && !risk.take_profit_bps) {
    warnings.push('this mandate publishes no stop and no take-profit, so nothing '
      + 'protects the position once it is open');
    return { exits, warnings };
  }
  for (const leg of rules.legs) {
    const t = leg.symbol.toUpperCase();
    const h = holdings[t];
    if (!h || h.quantity <= 0) continue;
    const inst = universe[t];
    if (!inst || !inst.verified) { warnings.push(`${t}: unverified, no exit placed`); continue; }
    if (!(h.costBasis > 0)) { warnings.push(`${t}: no cost basis, cannot derive a trigger`); continue; }
    const add = (kind, bps, sign) => exits.push({
      kind, ticker: t, address: inst.address, quantity: h.quantity,
      triggerPrice: h.costBasis * (1 + sign * bps / 10_000),
      referencePrice: h.costBasis, bpsFromReference: sign * bps, placed: false,
    });
    if (risk.stop_loss_bps) add('stop_loss', risk.stop_loss_bps, -1);
    if (risk.take_profit_bps) add('take_profit', risk.take_profit_bps, 1);
  }
  return { exits, warnings };
}

export async function placeExits(exits, chainKey, funder, maxSlippageBps = 100) {
  for (const e of exits) {
    try {
      const q = await flashQuote({
        targetChain: CHAINS[chainKey].flash, contraChain: CHAINS[chainKey].flash,
        targetAsset: e.address, contraAsset: CHAINS[chainKey].quote,
        side: 'sell', qty: e.quantity.toFixed(8),
        orderType: EXIT_ORDER_TYPE[e.kind], funderAddress: funder,
        maxSlippage: (maxSlippageBps / 10_000).toFixed(6),
        triggers: [{ notionalPrice: e.triggerPrice.toFixed(6),
                     triggerType: TRIGGER_DIRECTION[e.kind] }],
      });
      e.placed = true; e.quoteId = q.quoteId; e.orderType = q.orderType;
      e.hasTypedData = !!q.evm?.orderTypedData;
    } catch (err) { e.error = err.message; }
  }
  return exits;
}

/** How much of the position carries a live stop. A stop on two of three legs
 *  must not read as a protected position. */
export function exitCoverage(exits, holdings) {
  const withStop = [], without = [];
  for (const [t, h] of Object.entries(holdings)) {
    if (h.quantity <= 0) continue;
    (exits.some(e => e.ticker === t && e.kind === 'stop_loss' && e.placed)
      ? withStop : without).push(t);
  }
  const total = withStop.length + without.length;
  return {
    legsWithALiveStop: withStop.sort(), legsWithNoStop: without.sort(),
    fullyProtected: without.length === 0,
    coveragePct: total ? 100 * withStop.length / total : 100,
  };
}
