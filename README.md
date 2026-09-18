# Mandate

**Copy trading that can't lie.**

A strategist publishes a signed mandate. Followers subscribe with their own wallet. An agent
executes it through real order types, refuses when it should, then places the stops it
promised. Every fill is public, so the track record is **computed, not claimed**.

Built for Runtime Agent Week. Live on Robinhood Chain and Base.

---

## Why this exists

Copy trading runs on numbers you have to take somebody's word for. The platform holds your
money, the star trader reports their own returns, you find out afterwards.

Three things make that fixable now. All three are new:

1. Tokenized equities trade onchain, so **every fill is a public log**
2. A strategy can be a **hash**, so what was promised is not in dispute
3. An agent can hold **scoped, revocable authority** over a wallet it does not own

Put together: a strategist cannot inflate a return anyone can recompute, nor quietly
rewrite the thesis they attracted followers with.

## What the market actually looks like

Every number below came out of this repo against live chains. Re-run any of it.

**Robinhood Chain**, where the real depth is (`./mandate.py universe`):

| | NVDA | META | QQQ | SPY | GOOGL | TSLA | AAPL | MSFT |
|---|---|---|---|---|---|---|---|---|
| Liquidity | $3.18M | $2.77M | $1.45M | $895k | $725k | $672k | $336k | $294k |
| Holders | 166k | 40.0k | 27.3k | 67.9k | 66.1k | 66.4k | 79.2k | 58.7k |

**That table is a reading taken on 2026-09-17, not a constant.** NVDA moved from
$3.18M to $4.01M and META from $1.22M to $2.77M inside one working session. Run
the command for today's numbers. Anything here quoted to the dollar is quoted
because it was measured, not because it is stable.

Against the deepest B20 pool on Base at **$46k** and 8,866 holders. So the universe resolves
on Robinhood Chain. Base stays as the venue whose depth can be audited directly.

### What the issuer can still do to your position

Depth and ticker checks answer "can I trade this". Neither answers the question a follower
should ask before committing money for a month. So `./mandate.py custody` reads the control
surface off the chain:

| | |
|---|---|
| Instruments that are upgradeable proxies | **8 of 8** |
| Distinct beacons behind them | **1** |
| Distinct implementations | **1** |
| Powers in that implementation | `mint`, `burn`, `pause`, `unpause` |

Every tokenized equity in the universe is a 283-byte beacon proxy in front of 11,614 bytes
of shared logic. **All eight point at the same beacon**
(`0xe10b6f6b275de231345c20d14ab812db62151b00`). One upgrade there changes the behaviour of
every one of them at once.

None of that is an accusation. A regulated issuer needs exactly these powers. A
tokenized equity that could not be paused or reissued would be the surprising thing. What
is wrong is taking a follower's money for a month without telling them the powers exist.
So this is a disclosure and nothing blocks a trade on it. Refusing to trade a regulated
equity for being pausable would refuse all of them.

### The ticker problem

Resolving "NVDA" to a contract is the most dangerous step in the whole system. The venue's
own index returns squatters right next to the real asset:

| Asked for | Answered | Liquidity | What it is |
|---|---|---|---|
| NVDA | **NVDAx3L** | $407,605 | a 3x leveraged token |
| AAPL | **AAPLCAT** | $86,717 | unrelated, 1,127 holders |
| GOOGL | **TREX** | $63,062 | unrelated, 1,477 holders |
| AAPL | `AAAP,AACG,...,AAPL,AAPU,...` | $68 | one symbol, thousands of tickers |

That last one is a token whose symbol is a comma-joined list of thousands of tickers, built
to match every ticker search ever run against it. Ranking by liquidity picks the leveraged
token. Fuzzy matching picks the poisoner.

**61 lookalikes refused across 8 names, holding $681,663 between them.**

Resolution requires three things to agree: an exact symbol match, a liquidity and holder
floor, plus the contract's own `symbol()` read off the chain. Fail any one and the ticker is
refused rather than traded.

### Ghost pools

The same equities on Base, read straight off Uniswap v3 (`./mandate.py ghosts`):

| | count |
|---|---|
| Pools found | 13 |
| Live | 4 |
| Thin (under $5,000 depth) | 3 |
| Dead (zero liquidity, zero balance) | 5 |
| Trapped (quotes a price, cannot fill) | 1 |
| **Unusable** | **6 of 13, 46%** |

The clearest one: AAPLc's fee-10000 pool sits at tick **-887272**, the bottom of the tick
range, quoting about **$3.4e40 per Apple share**, holding $1,019 of real USDC that will
never trade. Nearly half the venues on this market quote a price and cannot fill anything.

## Run it

Python 3.11 or newer. No dependencies, no API key, no account.

```bash
./mandate.py verify          # every address and selector, read off chain
./mandate.py universe        # resolve tickers, list refused lookalikes
./mandate.py ghosts          # classify every Uniswap v3 pool on Base
./mandate.py publish --name "Compute & Index" --legs NVDA:40,META:30,SPY:30
./mandate.py subscribe --mandate <id> --sleeve 5000
./mandate.py run --mandate <id> --bond 300
./mandate.py portfolio
./mandate.py board
./mandate.py audit --mandate <id> --bond 300
./mandate.py tape --symbol AAPLc    # a track record rebuilt from chain logs
./mandate.py custody                # what the issuer can still do to a position
./mandate.py token

python3 -m unittest discover -s tests   # 86 offline tests
cd contracts && forge test              # 24 solidity tests, 2 fuzz suites
./scripts/cross_check_id.sh             # browser and python agree on an id
python3 api/server.py                   # agent-to-agent HTTP API
```

The web app in `web/` is the same product with no backend at all. Both chains and the venue
serve `access-control-allow-origin: *`, so it reads them straight from the browser. Nothing
can go down during judging except the static host.

## The nine gates

A run walks these in order. Any one can refuse, then returns that refusal with its reason,
because a follower is owed the reason their agent chose not to act in their name.

| | Gate | Refuses when |
|---|---|---|
| 1 | subscription | the follower is not actively subscribed |
| 2 | **bond** | the strategist has not posted enough to be slashable |
| 3 | market hours | the underlying equity market is shut and the mandate requires it open |
| 4 | **universe** | any leg fails double ticker verification |
| 5 | plan | the sleeve produces no executable clip |
| 6 | quote | the venue will not price the clip |
| 7 | gates | slippage exceeds the published ceiling, else depth cover is short |
| 8 | **protect** | a stop the mandate promised could not be placed |
| 9 | execute | always, without a signature from the follower's own wallet |

A real run, live, $5,000 sleeve across three legs: **12 entries quoted and gated** at 18 to
38bps against a 50bps ceiling, depth cover 2,316x to 8,420x, then **6 protective exits
resting at the venue**, 100% of legs carrying a live stop. Every one a signable EIP-712
payload.

An agent that always finds a reason to trade is not managing anyone's money, it is
generating fees.

## $MANDATE

Returns already cannot be faked. What a token adds is a cost for publishing disciplined
rules and then trading outside them.

Publishing a mandate that takes followers requires posting a bond: 2% of committed follower
sleeve, floor $250. The bond is slashable. Slashing is arithmetic rather than a
committee, because **the rules are a hash and the fills are public**, so whether a
strategist kept their own published rules is a decidable question.

| Violation | Penalty |
|---|---|
| Traded an instrument outside the mandate | 100% |
| Routed into an unverified contract | 50% |
| Traded out of hours on a market-hours mandate | 25% |
| Filled past the published slippage ceiling | 15% |
| Ignored the published depth cover | 10% |

Slashed bond goes **to the followers who were harmed**, in proportion to exposure, not to a
treasury. The harmed party is the follower.

### The bond is held by a contract

[`contracts/src/MandateBond.sol`](contracts/src/MandateBond.sol), **24 Solidity tests
including two fuzz suites**. What it claims is narrow on purpose:

It does **not** verify fills onchain. Checking a slippage ceiling onchain means putting every
fill and every venue quote there, which is not affordable, so promising it would be
dishonest. What it does is make the two things that must be tamper proof tamper proof, then
settle disputes with money rather than a committee:

1. The **rules hash is stored at publication**, so what was promised is immutable and cannot
   be re-pointed, by the strategist or by anyone else
2. The **bond is held by the contract**, not by the strategist and not by us. It cannot be
   withdrawn while followers are exposed
3. A slash is opened by a **challenger staking their own money** on a claim computed from the
   hashed rules and public fills. Unanswered claims execute permissionlessly once the window
   closes. Disputed ones go to an arbiter named at deployment and public before anyone
   subscribes. **A false claim pays its stake to the strategist**, which is what stops
   griefing.

```bash
cd contracts && forge test        # 24 passed, 5 skipped (the fork suite)
```

**It also runs against real chain state.** The unit suite uses a mock token,
which proves the logic but not that it survives a token somebody else wrote on a
chain somebody else runs. So the same lifecycle runs on a fork of Robinhood
Chain with **the real USDG contract** as the bond asset:

```bash
anvil --fork-url https://rpc.mainnet.chain.robinhood.com
cd contracts && forge test --match-path test/ForkLifecycle.t.sol \
    --fork-url http://127.0.0.1:8545     # 5 passed
```

Publish, bond 1,000 USDG, take on 20,000 of follower exposure, have a challenger
stake 50 USDG on a claim, let the window close, then a stranger executes it and
the harmed followers hold **375 and 125 USDG** split by exposure, with the
challenger made whole. Every transfer through USDG's own code.

Offline those five report as **skipped**, not passed. A test that returns early
and still says pass is a green tick for work that never ran.

Launched through Bankr on Robinhood Chain, where creator trading fees pay for the agent's
own compute. Deliberately not: governance voting over which mandates are allowed, staking
yield unbacked by fee revenue, any fee that scales with a self-reported return.

`./mandate.py token` prints the whole design. `core/bond.py` implements it.

## Integrations

**Uniswap v3 on Base**, read directly with `eth_call` and `eth_getLogs`, no SDK and no API
key, so anyone can re-run it against the same blocks.

| What | Where |
|---|---|
| v3 factory, `getPool` across four fee tiers | [`core/uniswap.py:33`](core/uniswap.py#L33) |
| `slot0`, `liquidity`, `token0`, `token1` | [`core/uniswap.py:44`](core/uniswap.py#L44) |
| sqrtPriceX96 to a decimal-adjusted price | [`core/uniswap.py:65`](core/uniswap.py#L65) |
| Real balances, not just in-range `liquidity()` | [`core/uniswap.py:91`](core/uniswap.py#L91) |
| ETH priced off the deepest WETH/USDC pool, never hardcoded | [`core/uniswap.py:103`](core/uniswap.py#L103) |
| Pool classification: live, thin, dead, trapped | [`core/depth.py:75`](core/depth.py#L75) |
| Cohort price-outlier detection | [`core/depth.py:128`](core/depth.py#L128) |
| Exact v3 in-range swap maths | [`core/depth.py:153`](core/depth.py#L153) |
| Fill simulation that refuses past in-range capacity | [`core/depth.py:172`](core/depth.py#L172) |
| Depth-aware route selection | [`core/router.py:60`](core/router.py#L60) |
| `exactInputSingle` calldata | [`core/wallet.py:67`](core/wallet.py#L67) |
| v3 `Swap` topic, derived not copied | [`core/ledger.py:30`](core/ledger.py#L30) |
| Track records rebuilt from Swap logs | [`core/ledger.py:143`](core/ledger.py#L143) |

Every selector is derived by our own keccak and checked against published values in
`scripts/verify_addresses.py`. `exactInputSingle` resolves to `0x04e45aaf`, `approve` to
`0x095ea7b3`. Feedback for the Uniswap team is in [FEEDBACK.md](FEEDBACK.md).

**Definitive Flash** for asset resolution and execution ([`core/flash.py`](core/flash.py),
[`core/exits.py`](core/exits.py)). Advanced order types confirmed against the live API
rather than a doc: `twap`, `limit`, `stop-loss`, `take-profit`, plus `attachedBracket` on an
entry. `bracket` as a top-level order type is rejected by the endpoint as "not yet
supported", which is why brackets attach instead. `probe_order_types()` re-checks this live.

Measured: routing a $500 AAPLc clip on Uniswap v3 alone filled at $335.33, Flash quoted
$333.87 for the same size. The aggregated venue fills better, so Flash executes and the
direct AMM read audits.

**Dynamic** for non-custodial authority ([`core/wallet.py`](core/wallet.py)). Delegated
access is exactly this product's posture: the follower keeps their embedded wallet and
grants scoped, revocable signing rights. Revoking in Dynamic ends the agent's authority
without us being involved. Three adapters behind one interface, calling the SDK as its
Python docs specify.

## What is verified, what is not

Verified live, re-checkable by anyone:

- Every address and selector, off both chains, by `./mandate.py verify`
- Chain ids confirmed by `eth_chainId` before any read is trusted
- The universe, the 61 refused lookalikes, the 46% ghost-pool figure
- The control surface: 8 of 8 upgradeable, one shared beacon, read off the chain
- 12 entry quotes and 6 protective exits, all signable EIP-712, against the live venue
- Track records rebuilt from raw Uniswap `Swap` logs
- Browser and Python hash a mandate to the same id, gated in CI
- 86 offline Python tests, 24 Solidity unit tests including fuzz
- The bond lifecycle executed against the real USDG contract on a Robinhood Chain fork

Not verified, stated rather than implied:

- **No transaction has ever been broadcast.** No key is held anywhere in this repo. Every
  order comes back as a payload to be signed by the follower's own wallet.
- **The Dynamic adapters have never run.** No environment id or API token was available, so
  `available()` reports False and every call refuses instead of pretending. Running them
  needs `DYNAMIC_ENV_ID` plus `DYNAMIC_API_TOKEN`, nothing else.
- **Flash calls use the integrator key Definitive publishes** in their own docs for
  trade-only use. Set `FLASH_API_KEY` to be credited as the integrator instead.
- **Fill simulation on Uniswap is in-range only.** Crossing a tick needs the initialized-tick
  bitmap, which is not fetched per quote, so a size beyond in-range capacity is reported as
  `exceeds_in_range` rather than extrapolated.
- **Liquidity figures move.** META went from $1.22M to $2.77M and NVDA from $3.18M to
  $4.01M inside one session. Every figure here is a reading, not a constant.
- **The ghost scan in the browser covers USDC pairs only** and finds 10 pools at 40%
  unusable. The Python scan also walks WETH pairs and finds 13 at 46%. Both are stated so
  neither is mistaken for the other.

## Known gaps

Things a reviewer would find, listed here first:

- Strategist fees are computed and owed, not escrowed. There is no settlement contract yet.
- The bond contract is written and tested but **not deployed**. Deploying costs gas. No
  funds were moved for this build.
- The arbiter is a single address. It only ever sees disputed claims. It is public before
  anyone subscribes, but it is a trusted party and should say so rather than hide behind the
  word decentralised.
- Delegation lifecycle (webhook receipt, credential storage, revocation) is typed and wired
  but has no persistence layer.
- The book is a JSON file. Publishing is unauthenticated. Fine for a judge, not for
  money.
- Corporate actions. The implementation behind these tokens carries `mint` and `burn` and
  **no multiplier or rebase getter answered**, so a split is presumably handled by
  reissuance rather than by scaling a factor. Either way a track record spanning one is not
  adjusted yet, which is a correctness gap. An earlier draft of this README asserted a
  multiplier mechanism before checking; `./mandate.py custody` is what settled it.
- Exits are placed as resting quotes and never re-priced. A position that runs needs its
  stop trailed. Nothing does that yet.
- Only the entry side has a depth-aware router. Exits size to the position and trust the
  venue.

## Licence

Apache-2.0. See [LICENSE](LICENSE) and [NOTICE](NOTICE).

## AI disclosure

AI assistance (Claude, Anthropic) was used to build this. The design, the research and the
verification decisions are the author's. Every onchain number in this README was produced by
running the code in this repo against live chains. Every one is reproducible with the
commands above.
