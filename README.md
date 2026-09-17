# Mandate

**Copy trading that can't lie.**

A strategist publishes a signed mandate. Followers subscribe with their own wallet. An agent
executes it with depth-proven routing and real order types. Every fill lands onchain, so the
track record is computed rather than claimed.

Built for Runtime Agent Week, on Base, over Uniswap v3, on B20 tokenized equities.

---

## The finding this is built on

Tokenized equities are live on Base right now. Apple, Alphabet, Meta and NVIDIA all trade as
ordinary ERC-20s with real Uniswap v3 pools. So we scanned every pool for every one of them
against live chain state.

| | |
|---|---|
| Tokenized equities verified onchain | 4 |
| Uniswap v3 pools found | 13 |
| Live | 4 |
| Thin (under $5,000 of quote depth) | 3 |
| Dead (zero liquidity, zero balance) | 5 |
| Trapped (quotes a price, cannot fill) | 1 |
| **Unusable** | **6 of 13, 46%** |

The clearest one: AAPLc's fee-10000 pool sits at tick **-887272**, the bottom of the tick
range, quoting roughly **$3.4e40 per Apple share**. It holds $1,019 of real USDC that will
never trade.

Nearly half the venues on this market quote a price and cannot fill anything. A router that
picks by fee tier or by "a pool exists" walks straight into them. That is the risk sitting
under every tokenized-equity strategy today. It is why this product refuses rather than
fills badly.

Reproduce it yourself:

```bash
python3 scripts/scan_market.py
```

## Run it

No API key, no dependencies, Python 3.11 or newer.

```bash
git clone <this repo> && cd mandate
python3 scripts/verify_addresses.py     # every address read off Base, live
python3 -m unittest discover -s tests   # 34 tests, offline
python3 api/server.py                   # http://127.0.0.1:8402
```

The web app has four surfaces: **Market truth** (every pool classified), **Mandates**
(publish a strategy, get its id), **Simulate** (run the agent and read every decision it
made, including the refusals) plus **Tape** (real fills replayed into a track record).

## How it works

A **mandate** is a strategy as an immutable ruleset: legs and weights, execution style,
slippage ceiling, depth cover, stops, take-profits, fee. Its id is the SHA-256 of the
canonical rules, so changing what it does changes its id. The strategist signs a message that
contains that id, which means the signature commits to the rules and not to a name that could
later be re-pointed somewhere else.

A follower subscribes with a size. The agent then runs six steps. Any of them can refuse:

1. **Market hours.** The underlying equity trades about six and a half hours a weekday. The
   token trades all the time. Outside the session nothing arbitrages the token back to its
   underlying, so a mark taken then is uncertified and a strict mandate refuses to act on it.
2. **Marks.** Price each leg off the deepest live pool.
3. **Plan.** Turn the mandate plus the sleeve into clips. A TWAP widens itself when the venue
   is too shallow for the requested clip size.
4. **Route.** For every clip, classify the pools, drop the dead and trapped ones, simulate the
   fill with exact Uniswap v3 in-range maths, require depth cover, enforce the slippage
   ceiling, then pick the best executable price left.
5. **Build.** Real `exactInputSingle` calldata against SwapRouter02.
6. **Broadcast**, else refuse and say why.

A refusal is a result, not a failure. An agent that always finds a reason to trade is not
managing anyone's money, it is generating fees.

## Uniswap integration

Reviewers: these are the exact lines that implement it.

| What | File and line |
|---|---|
| v3 factory address, on Base | [`core/uniswap.py:14`](core/uniswap.py#L14) |
| `getPool(address,address,uint24)` lookup across all four fee tiers | [`core/uniswap.py:33`](core/uniswap.py#L33) |
| `slot0()`, `liquidity()`, `token0()`, `token1()` reads | [`core/uniswap.py:44`](core/uniswap.py#L44) |
| sqrtPriceX96 to a decimal-adjusted price | [`core/uniswap.py:65`](core/uniswap.py#L65) |
| Pool discovery per token against USDC and WETH | [`core/uniswap.py:73`](core/uniswap.py#L73) |
| Real withdrawable balances, not just in-range `liquidity()` | [`core/uniswap.py:91`](core/uniswap.py#L91) |
| ETH priced off the deepest WETH/USDC pool, never hardcoded | [`core/uniswap.py:103`](core/uniswap.py#L103) |
| Pool classification: live, thin, dead, trapped | [`core/depth.py:75`](core/depth.py#L75) |
| Cohort price-outlier detection | [`core/depth.py:128`](core/depth.py#L128) |
| Exact v3 in-range swap maths | [`core/depth.py:153`](core/depth.py#L153) |
| Fill simulation that refuses past in-range capacity | [`core/depth.py:172`](core/depth.py#L172) |
| Depth-aware route selection | [`core/router.py:60`](core/router.py#L60) |
| SwapRouter02 address | [`core/wallet.py:36`](core/wallet.py#L36) |
| `exactInputSingle` calldata construction | [`core/wallet.py:67`](core/wallet.py#L67) |
| v3 `Swap` event topic, derived not copied | [`core/ledger.py:30`](core/ledger.py#L30) |
| Swap log decoding into fills | [`core/ledger.py:143`](core/ledger.py#L143) |

The integration talks to the AMM directly with `eth_call` and `eth_getLogs`, so it needs no
API key and anyone can re-run it against the same blocks. Every selector is derived by our own
keccak implementation and checked against the published values in
`scripts/verify_addresses.py`: `exactInputSingle` resolves to `0x04e45aaf` and `approve` to
`0x095ea7b3`.

Feedback for the Uniswap team is in [FEEDBACK.md](FEEDBACK.md).

## Dynamic integration

The product is non-custodial by construction, which is exactly Dynamic's delegated-access
pattern: the follower keeps their own embedded wallet and grants this server scoped, revocable
signing rights. Revoking in Dynamic ends the agent's authority immediately, without us being
involved.

[`core/wallet.py`](core/wallet.py) carries three adapters behind one interface: `DryRunWallet`
(default, builds calldata and refuses to broadcast), `DynamicServerWallet` (an MPC server
wallet the agent owns) and `DynamicDelegatedWallet` (a follower's own wallet under
delegation). They call the SDK as its Python docs specify: `DynamicEvmWalletClient(env_id,
rpc_urls=...)`, `authenticate_api_token`, `create_wallet_account`, `send_transaction`, plus
`decrypt_delegated_webhook_data` and `create_delegated_evm_client` on the delegated path.

**Honest status:** the Dynamic adapters are wired but not exercised. No Dynamic environment id
or API token was available while building, so `available()` reports False and every call
refuses instead of pretending. Running them needs `DYNAMIC_ENV_ID` plus `DYNAMIC_API_TOKEN`, nothing else.

## Advanced order types

Thin equity pools make execution style load-bearing, so mandates execute through real order
types rather than market swaps: `market`, `dca`, `twap`, `limit`, `stop`, `take_profit` and
`bracket`, in [`core/orders.py`](core/orders.py). Stops and take-profits attach to the
position as protective exits at plan time. A TWAP that meets a venue too shallow for its
requested clip widens itself and records why.

## What is verified, what is not

Verified live, re-checkable by anyone:

- Every address and selector, off Base, by `scripts/verify_addresses.py`
- The pool classification and the 46% figure, by `scripts/scan_market.py`
- Route selection and refusal at $500, $5,000 and $50,000 clip sizes
- `exactInputSingle` calldata, 228 bytes, correct selector
- Track records rebuilt from real `Swap` logs (570 fills over 20,000 blocks on the AAPLc pool)
- 34 offline tests

Not verified, stated rather than implied:

- **No transaction has been broadcast.** The default wallet is dry-run by design and no funds
  were moved. The calldata is built and checked, not sent.
- **The Dynamic adapters have never run**, for want of credentials. See above.
- **Fill simulation is in-range only.** Crossing a tick boundary needs the initialized-tick
  bitmap, which is not fetched per quote. A size beyond in-range capacity is reported as
  `exceeds_in_range` rather than extrapolated.
- **Ledger coverage is partial.** Fills are read from the pools in the registry. A trade
  routed through an aggregator that settles elsewhere is not counted. Every record says so.
- **The registry is four tokens.** More B20 equities exist. These four are the ones whose
  addresses were confirmed to carry matching contracts on Base.
- **Bankr's own tokenized-stock trading requires location verification** and is unavailable in
  the US and UK. This project reads Base directly and does not route through that path.

## Known gaps

Things a reviewer would find, listed here first:

- No onchain settlement contract for strategist fees yet. Fees are computed and owed, not escrowed.
- Delegation lifecycle (webhook receipt, credential storage, revocation) is designed and typed
  but has no persistence layer.
- Mandates are stored in a JSON file rather than a database, with no authentication on publish.
- Corporate actions: B20 tracks total return through an onchain multiplier. The ledger does not
  yet read that multiplier, so a track record spanning a split would be wrong. It is the next
  correctness item, not a stylistic one.
- Only the buy side is routed. Sells reuse the same depth logic but are not wired end to end.

## Licence

Apache-2.0. See [LICENSE](LICENSE) and [NOTICE](NOTICE).

## AI disclosure

AI assistance (Claude, Anthropic) was used to build this. The design, the research and the
verification decisions are the author's. Every onchain number in this README was produced by
running the code in this repo against Base mainnet. Every one of them is reproducible with
the commands above.
