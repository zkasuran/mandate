# SPDX-License-Identifier: Apache-2.0
"""Base JSON-RPC access with retry, backoff and endpoint rotation.

Every on-chain identifier this project publishes is read through here at run
time. Nothing is hardcoded from a doc: a doc's example address turned out to
have no code on Base, which is exactly the failure this module exists to catch.
"""
from __future__ import annotations

import json
import time
import threading
import urllib.error
import urllib.request

UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36")

# Public Base endpoints, tried in order. No key required, so the project stays
# reproducible for anyone who clones it.
ENDPOINTS = [
    "https://mainnet.base.org",
    "https://base-rpc.publicnode.com",
    "https://base.drpc.org",
    "https://1rpc.io/base",
]

_lock = threading.Lock()
_last_call = [0.0]
MIN_INTERVAL = 0.12          # ~8 req/s ceiling, well under the public limits


def _throttle() -> None:
    with _lock:
        wait = MIN_INTERVAL - (time.monotonic() - _last_call[0])
        if wait > 0:
            time.sleep(wait)
        _last_call[0] = time.monotonic()


class RpcError(RuntimeError):
    pass


def rpc(method: str, params: list, *, retries: int = 5):
    """One JSON-RPC call. Rotates endpoints and backs off on 429/5xx."""
    payload = json.dumps({"jsonrpc": "2.0", "id": 1,
                          "method": method, "params": params}).encode()
    last = None
    for attempt in range(retries):
        url = ENDPOINTS[attempt % len(ENDPOINTS)]
        _throttle()
        req = urllib.request.Request(
            url, data=payload,
            headers={"content-type": "application/json", "user-agent": UA},
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                out = json.load(r)
            if "error" in out:
                # A revert is a real answer, not a transport problem.
                raise RpcError(f"{method}: {out['error']}")
            return out["result"]
        except urllib.error.HTTPError as e:
            last = e
            if e.code in (429, 500, 502, 503, 504):
                time.sleep(0.4 * (2 ** attempt))
                continue
            raise
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
            last = e
            time.sleep(0.4 * (2 ** attempt))
    raise RpcError(f"{method} failed after {retries} attempts: {last}")


def block_number() -> int:
    return int(rpc("eth_blockNumber", []), 16)


def get_code(addr: str) -> str:
    key = ("code", addr.lower())
    hit = _cache_get(key)
    if hit is not None:
        return hit[0]
    out = rpc("eth_getCode", [addr, "latest"])
    _cache_put(key, out, None)
    return out


def has_code(addr: str) -> bool:
    return len(get_code(addr)) > 2


def call(to: str, data: str, block: str = "latest") -> str:
    return rpc("eth_call", [{"to": to, "data": data}, block])


# --- caching ----------------------------------------------------------------
# Pool addresses, token metadata and factory lookups never change, so they are
# cached forever. Live state (slot0, balances) is cached briefly so one page
# render does not fire the same call a dozen times and trip the public rate
# limit. TTL is short enough that a quote is never stale by more than a block
# or two.

_cache: dict = {}
_cache_lock = threading.Lock()
LIVE_TTL = 8.0          # seconds

IMMUTABLE_SELECTORS = {
    "0x06fdde03",       # name()
    "0x95d89b41",       # symbol()
    "0x313ce567",       # decimals()
    "0x1698ee82",       # getPool()
    "0x0dfe1681",       # token0()
    "0xd21220a7",       # token1()
    "0xddca3f43",       # fee()
}


def _cache_get(key):
    with _cache_lock:
        hit = _cache.get(key)
        if not hit:
            return None
        value, expires = hit
        if expires is not None and time.monotonic() > expires:
            _cache.pop(key, None)
            return None
        return (value,)


def _cache_put(key, value, ttl):
    with _cache_lock:
        _cache[key] = (value, None if ttl is None else time.monotonic() + ttl)


def cache_clear() -> None:
    with _cache_lock:
        _cache.clear()


def cached_call(to: str, data: str, block: str = "latest"):
    """eth_call with revert-tolerance and caching. None means the call reverted."""
    key = ("call", to.lower(), data.lower(), block)
    hit = _cache_get(key)
    if hit is not None:
        return hit[0]
    try:
        out = call(to, data, block)
        value = None if out in ("0x", "") else out
    except RpcError:
        value = None
    ttl = None if data[:10] in IMMUTABLE_SELECTORS else LIVE_TTL
    _cache_put(key, value, ttl)
    return value


def try_call(to: str, data: str, block: str = "latest"):
    """eth_call that returns None on revert instead of raising.

    Probing whether a pool exists is a normal miss, not an error.
    """
    return cached_call(to, data, block)


# --- minimal ABI coding (no third-party deps) --------------------------------

def enc_addr(a: str) -> str:
    return a.lower().replace("0x", "").rjust(64, "0")


def enc_uint(n: int) -> str:
    return f"{n:064x}"


def dec_uint(hexstr: str, word: int = 0) -> int:
    body = hexstr[2:]
    return int(body[word * 64:(word + 1) * 64] or "0", 16)


def dec_addr(hexstr: str, word: int = 0) -> str:
    body = hexstr[2:]
    return "0x" + body[word * 64:(word + 1) * 64][-40:]


def dec_string(hexstr: str) -> str:
    """ERC-20 name/symbol, tolerating the old bytes32 style."""
    b = bytes.fromhex(hexstr[2:])
    if len(b) < 64:
        return b.rstrip(b"\x00").decode("utf-8", "replace")
    length = int.from_bytes(b[32:64], "big")
    if length == 0 or 64 + length > len(b):
        return b[:32].rstrip(b"\x00").decode("utf-8", "replace")
    return b[64:64 + length].decode("utf-8", "replace")


# --- ERC-20 ------------------------------------------------------------------

SEL = {
    "name": "0x06fdde03",
    "symbol": "0x95d89b41",
    "decimals": "0x313ce567",
    "totalSupply": "0x18160ddd",
    "balanceOf": "0x70a08231",
}


def erc20(addr: str) -> dict:
    """Read a token's identity straight off the contract."""
    out = {"address": addr, "code": has_code(addr)}
    if not out["code"]:
        return out
    for key in ("name", "symbol"):
        raw = try_call(addr, SEL[key])
        out[key] = dec_string(raw) if raw else None
    raw = try_call(addr, SEL["decimals"])
    out["decimals"] = dec_uint(raw) if raw else None
    raw = try_call(addr, SEL["totalSupply"])
    out["totalSupply"] = dec_uint(raw) if raw else None
    return out


def balance_of(token: str, owner: str) -> int | None:
    raw = try_call(token, SEL["balanceOf"] + enc_addr(owner))
    return dec_uint(raw) if raw else None
