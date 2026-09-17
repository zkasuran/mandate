# SPDX-License-Identifier: Apache-2.0
"""Mandate HTTP API. Standard library only, so it runs anywhere with Python.

Endpoints
    GET  /api/health          block height, session state, cache stats
    GET  /api/market          every tokenized equity with every pool classified
    GET  /api/marks           best certifiable price per symbol
    GET  /api/mandates        the published mandates
    POST /api/mandates        publish one (validated, id derived from the rules)
    GET  /api/mandates/<id>   one mandate with its live plan
    POST /api/quote           depth-aware route for a single clip
    POST /api/simulate        run a mandate for a follower and return every decision
    GET  /api/activity/<sym>  recent real fills from the pool, with a track record
"""
from __future__ import annotations

import json
import os
import sys
import threading
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import basis, chain, depth, ledger, spec, tokens, uniswap  # noqa: E402
from agent.runner import Agent  # noqa: E402

WEB_DIR = os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "web")
STORE = os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "data", "mandates.json")

_registry_lock = threading.Lock()
_registry: dict = {}


def registry() -> dict:
    global _registry
    with _registry_lock:
        if not _registry:
            _registry = tokens.verified()
        return _registry


def load_mandates() -> list:
    if not os.path.exists(STORE):
        return []
    with open(STORE) as fh:
        return json.load(fh)


def save_mandates(rows: list) -> None:
    os.makedirs(os.path.dirname(STORE), exist_ok=True)
    tmp = STORE + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(rows, fh, indent=1)
    os.replace(tmp, STORE)


class Handler(BaseHTTPRequestHandler):
    server_version = "Mandate/1.0"

    def log_message(self, fmt, *args):
        sys.stderr.write(f"{self.address_string()} {fmt % args}\n")

    # -- plumbing ------------------------------------------------------------
    def _send(self, code: int, payload, content_type="application/json"):
        if content_type == "application/json":
            body = json.dumps(payload, default=str).encode()
        else:
            body = payload if isinstance(payload, bytes) else payload.encode()
        self.send_response(code)
        self.send_header("content-type", content_type)
        self.send_header("content-length", str(len(body)))
        self.send_header("access-control-allow-origin", "*")
        self.send_header("access-control-allow-headers", "content-type")
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> dict:
        n = int(self.headers.get("content-length") or 0)
        return json.loads(self.rfile.read(n) or b"{}")

    def do_OPTIONS(self):
        self._send(204, b"", "text/plain")

    # -- routing -------------------------------------------------------------
    def do_GET(self):
        url = urlparse(self.path)
        path, query = url.path, parse_qs(url.query)
        try:
            if path in ("/", "/index.html"):
                return self._static("index.html")
            if path.startswith("/static/"):
                return self._static(os.path.basename(path))
            if path == "/api/health":
                return self._send(200, self.health())
            if path == "/api/market":
                return self._send(200, self.market())
            if path == "/api/marks":
                return self._send(200, self.marks())
            if path == "/api/mandates":
                return self._send(200, {"mandates": load_mandates()})
            if path.startswith("/api/mandates/"):
                return self._send(200, self.one_mandate(path.rsplit("/", 1)[-1]))
            if path.startswith("/api/activity/"):
                sym = path.rsplit("/", 1)[-1]
                look = int(query.get("blocks", ["4000"])[0])
                return self._send(200, self.activity(sym, look))
            return self._send(404, {"error": "no such route", "path": path})
        except Exception as e:
            traceback.print_exc()
            return self._send(500, {"error": str(e)})

    def do_POST(self):
        path = urlparse(self.path).path
        try:
            body = self._body()
            if path == "/api/mandates":
                return self._send(200, self.publish(body))
            if path == "/api/quote":
                return self._send(200, self.quote(body))
            if path == "/api/simulate":
                return self._send(200, self.simulate(body))
            return self._send(404, {"error": "no such route", "path": path})
        except spec.MandateError as e:
            return self._send(400, {"error": str(e)})
        except Exception as e:
            traceback.print_exc()
            return self._send(500, {"error": str(e)})

    def _static(self, name: str):
        fp = os.path.join(WEB_DIR, name)
        if not os.path.isfile(fp):
            return self._send(404, {"error": "not found"})
        ctype = ("text/html" if name.endswith(".html")
                 else "text/css" if name.endswith(".css")
                 else "application/javascript" if name.endswith(".js")
                 else "application/octet-stream")
        with open(fp, "rb") as fh:
            self._send(200, fh.read(), ctype + "; charset=utf-8")

    # -- handlers ------------------------------------------------------------
    def health(self) -> dict:
        return {
            "ok": True,
            "block": chain.block_number(),
            "chain": "base:8453",
            "session": basis.session_state().to_dict(),
            "tokens_verified": sum(1 for v in registry().values() if v.get("ok")),
        }

    def market(self) -> dict:
        reg = registry()
        eth = uniswap.eth_usd_price()
        out, counts = {}, {"live": 0, "thin": 0, "dead": 0, "trapped": 0}
        for sym, meta in reg.items():
            if not meta.get("ok"):
                continue
            reports = depth.analyse_token(meta["address"], meta["decimals"],
                                          eth_usd=eth)
            for r in reports:
                counts[r.status] += 1
            out[sym] = {
                "meta": meta,
                "pools": [r.to_dict() for r in
                          sorted(reports, key=lambda x: -x.quote_usd_held)],
            }
        total = sum(counts.values())
        return {
            "block": chain.block_number(), "eth_usd": eth,
            "tokens": out, "counts": counts, "total_pools": total,
            "unusable": counts["dead"] + counts["trapped"],
            "unusable_pct": (round(100 * (counts["dead"] + counts["trapped"])
                                   / total) if total else 0),
            "session": basis.session_state().to_dict(),
        }

    def marks(self) -> dict:
        agent = Agent(registry=registry())
        reg = registry()
        m = agent.marks([s for s, v in reg.items() if v.get("ok")])
        session = basis.session_state()
        for sym in m:
            m[sym]["certified"] = session.open
        return {"marks": m, "session": session.to_dict(),
                "certified": session.open}

    def publish(self, body: dict) -> dict:
        legs = tuple(spec.Leg(**l) for l in body["legs"])
        m = spec.Mandate(
            name=body["name"], strategist=body["strategist"],
            thesis=body["thesis"], legs=legs,
            execution=spec.Execution(**body.get("execution", {})),
            risk=spec.Risk(**body.get("risk", {})),
            fee_bps=int(body.get("fee_bps", 100)),
            cadence_seconds=int(body.get("cadence_seconds", 86_400)),
        )
        m.validate()
        rows = load_mandates()
        if any(r["mandate_id"] == m.mandate_id() for r in rows):
            return {"published": False, "reason": "identical rules already published",
                    "mandate": m.to_dict()}
        rows.append(m.to_dict())
        save_mandates(rows)
        return {"published": True, "mandate": m.to_dict(),
                "signing_message": m.signing_message()}

    def one_mandate(self, mid: str) -> dict:
        for r in load_mandates():
            if r["mandate_id"] == mid:
                return {"mandate": r}
        return {"error": "unknown mandate id"}

    def quote(self, body: dict) -> dict:
        from core import router
        sym = body["symbol"]
        meta = registry()[sym]
        route = router.plan_buy(
            sym, meta["address"], meta["decimals"],
            int(body["usdc"]),
            max_slippage_bps=int(body.get("max_slippage_bps", 50)),
            min_depth_multiple=int(body.get("min_depth_multiple", 3)))
        return route.to_dict()

    def simulate(self, body: dict) -> dict:
        row = None
        for r in load_mandates():
            if r["mandate_id"] == body["mandate_id"]:
                row = r
                break
        if row is None:
            return {"error": "unknown mandate id"}
        m = spec.load(row)
        agent = Agent(registry=registry())
        result = agent.run(m, body.get("follower", "0x" + "00" * 20),
                           int(body["sleeve_usdc"]))
        return result.to_dict()

    def activity(self, symbol: str, lookback: int) -> dict:
        meta = registry().get(symbol)
        if not meta or not meta.get("ok"):
            return {"error": "unknown symbol"}
        reports = depth.analyse_token(meta["address"], meta["decimals"])
        usable = [r for r in reports
                  if r.status in ("live", "thin")
                  and r.quote.lower() == uniswap.USDC.lower()]
        if not usable:
            return {"symbol": symbol, "fills": [],
                    "note": "no live USDC pool to read fills from"}
        best = max(usable, key=lambda r: r.quote_usd_held)
        fills = ledger.pool_activity(best.pool, symbol, meta["address"],
                                     meta["decimals"], lookback=lookback)
        session = basis.session_state()
        record = ledger.track_record([f for f in fills], symbol, meta["address"],
                                     mark=best.price, mark_certified=session.open)
        record["fills"] = record["fills"][-50:]
        return {"symbol": symbol, "pool": best.pool, "blocks": lookback,
                "record": record, "session": session.to_dict()}


def main() -> int:
    port = int(os.environ.get("PORT", "8402"))
    host = os.environ.get("HOST", "127.0.0.1")
    srv = ThreadingHTTPServer((host, port), Handler)
    print(f"Mandate API on http://{host}:{port}")
    print("warming the registry off Base ...")
    registry()
    print(f"  {sum(1 for v in _registry.values() if v.get('ok'))} tokens verified")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
