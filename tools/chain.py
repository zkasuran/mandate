"""Minimal, dependency-free Base JSON-RPC reader. Every identifier this project
publishes is read off the chain through here, never from memory or a doc."""
import json, urllib.request

RPC = "https://mainnet.base.org"
UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36")

def rpc(method, params, rpc_url=RPC):
    req = urllib.request.Request(
        rpc_url,
        data=json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode(),
        headers={"content-type": "application/json", "user-agent": UA},
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        out = json.load(r)
    if "error" in out:
        raise RuntimeError(f"{method}: {out['error']}")
    return out["result"]

def call(to, data, block="latest"):
    return rpc("eth_call", [{"to": to, "data": data}, block])

def _dec_string(hexstr):
    b = bytes.fromhex(hexstr[2:])
    if len(b) < 64:                       # bytes32-style string
        return b.rstrip(b"\x00").decode("utf-8", "replace")
    length = int.from_bytes(b[32:64], "big")
    return b[64:64 + length].decode("utf-8", "replace")

def erc20(addr):
    """name, symbol, decimals, totalSupply straight from the contract."""
    out = {"address": addr}
    out["code"] = len(rpc("eth_getCode", [addr, "latest"])) > 2
    if not out["code"]:
        return out
    try: out["name"] = _dec_string(call(addr, "0x06fdde03"))
    except Exception as e: out["name"] = f"<err {e}>"
    try: out["symbol"] = _dec_string(call(addr, "0x95d89b41"))
    except Exception as e: out["symbol"] = f"<err {e}>"
    try: out["decimals"] = int(call(addr, "0x313ce567"), 16)
    except Exception as e: out["decimals"] = None
    try: out["totalSupply"] = int(call(addr, "0x18160ddd"), 16)
    except Exception as e: out["totalSupply"] = None
    return out

if __name__ == "__main__":
    import sys
    print(f"block {int(rpc('eth_blockNumber', []), 16):,}")
    for a in sys.argv[1:]:
        print(json.dumps(erc20(a), indent=1))
