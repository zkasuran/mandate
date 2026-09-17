# Feedback for the Uniswap developer team

Built during Runtime Agent Week. We integrated Uniswap v3 on Base directly, with raw
`eth_call` and `eth_getLogs` and no SDK, to route trades in B20 tokenized equities
(AAPLc, GOOGLc, METAc, NVDAc). This is what we ran into.

## What worked well

**The contracts are the API.** `getPool`, `slot0`, `liquidity`, `token0`, `token1` plus
ERC-20 `balanceOf` were enough to build a complete depth-aware router with no API key and no
dependencies. For an agent that has to justify its decisions to the person whose money it is
moving, reading the AMM directly beats trusting an off-chain quote, because the inputs are
re-checkable by anyone at the same block. That property is why we could publish a number like
"46% of pools are unusable" and expect a reader to verify it rather than believe us.

**The in-range maths is genuinely closed-form.** Implementing
`sqrtP' = sqrtP + dy/L` and its inverse from the whitepaper took under an hour and matched
pool behaviour. Being able to compute an exact fill while liquidity is constant, rather than
estimating, is what let us put a hard slippage gate in front of execution.

**Fee tiers make fragmentation legible.** Enumerating four tiers per pair is cheap and turns
"where is the liquidity" into a finite, answerable question.

## Friction, in the order it cost us time

### 1. There is no cheap way to ask "can this pool actually fill X?"

`liquidity()` returns in-range liquidity, which says nothing on its own about withdrawable
depth. `slot0()` returns a price that is perfectly well-defined on a pool holding nothing.
We ended up calling `balanceOf` on both tokens for every pool just to find out whether a
venue was real. On a 13-pool scan that is 26 extra calls before any routing decision.

A view returning something like "token balances plus in-range liquidity" in one call would
help. So would a documented recommendation to check balances. Either saves every integrator
from rediscovering this. Right now the obvious read (`slot0` plus `liquidity`) is the one that misleads you.

### 2. Initialized-but-empty pools are indistinguishable from real ones at the factory

`getPool` returns an address for pools that were created, initialized at an arbitrary tick
then abandoned. One of them (AAPLc fee-10000) sits at tick `-887272` quoting about
$3.4e40 per share while holding $1,019 of USDC. Nothing in the factory interface flags it.

Every integrator has to build the same classifier. A canonical "is this pool worth routing to"
heuristic in the docs, even an informal one with thresholds, would stop a lot of naive routers
from filling into these. We wrote ours in `core/depth.py` and would happily see it made
unnecessary.

### 3. Crossing ticks requires the bitmap, so quoting gets expensive fast

In-range fills are exact and cheap. The moment a clip exceeds in-range capacity you need
`tickBitmap` plus per-tick `liquidityNet`, which is several more round trips. We chose to
refuse rather than extrapolate, reporting `exceeds_in_range`, because a wrong quote on
someone else's money is worse than no quote. That is the right call for us but it does mean
we cannot serve large clips at all without adopting the Quoter.

A lightweight "max in-range input" view would cover a large share of real routing decisions
without the bitmap walk.

### 4. Tokenized equities break an assumption the AMM does not know about

The underlying equity trades roughly 09:30 to 16:00 New York on weekdays. The token trades
continuously. Outside that window nothing is arbitraging the token back to its underlying, so
the pool price drifts and the mark is not a fair value in any usable sense. We built a session
guard (`core/basis.py`) that refuses to certify a mark taken while the underlying market is
shut.

As real-world assets land on the AMM this stops being our niche problem and becomes a general
one. Guidance on handling asset-specific trading calendars, plus a convention for signalling
them, would help the whole RWA category. Today every team will solve it privately. More
likely not at all.

### 5. Docs assume the SDK

The TypeScript SDKs are good. The documentation is written around them. We were working in
Python with no dependencies, so reconstructing the exact ABI encoding for
`exactInputSingle` (a seven-word static tuple, head-encoded inline rather than offset-encoded)
took longer than it should have. A short "raw calldata" appendix giving the selector and the
encoding layout for the handful of Router methods people actually call would be useful for
anyone integrating from a language without an SDK, which increasingly means agent runtimes.

For reference, the value we needed and eventually derived:
`exactInputSingle((address,address,uint24,address,uint256,uint256,uint160))` is `0x04e45aaf`.

## One thing we would ask for above all

**A "route quality" primitive.** Not a router. Not a price. Something closer to: given a
pair and a size, which of the pools that exist can honestly absorb it. We built it because our
product cannot function without it. We suspect most agent-driven integrations either build
it too or quietly skip it and route badly. The second outcome is the dangerous one, because
it fails silently and the user finds out in the fill price.

## Where the integration lives

Repository README has a line-by-line table. The core files are `core/uniswap.py` (factory and
pool reads), `core/depth.py` (classification and exact in-range fill maths), `core/router.py`
(route selection and refusal) and `core/wallet.py` (SwapRouter02 calldata).

Reproduce the scan behind the numbers above with `python3 scripts/scan_market.py`.
