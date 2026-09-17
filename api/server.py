# SPDX-License-Identifier: Apache-2.0
"""Mandate HTTP API. Standard library only.

This exists for agent-to-agent use: another agent discovers mandates, reads a
strategist's recomputed record and subscribes, without a browser. The web app
does not use it, because the web app talks to the chains and the venue
directly. Everything here calls the same `core` modules the CLI does, so the
three surfaces cannot drift into describing different products.

    GET  /api/health                block heights, session, universe size
    GET  /api/universe              resolved tickers plus refused lookalikes
    GET  /api/mandates              published mandates
    POST /api/mandates              publish one, id derived from the rules
    GET  /api/mandates/<id>         one mandate
    GET  /api/mandates/<id>/audit   fills against the rules hashed into the id
    POST /api/subscribe             record a follower's authority and size
    POST /api/run                   run the agent, return every decision
    GET  /api/portfolio/<address>   holdings, rebuilt by replay
    GET  /api/leaderboard           ranked on volume and recomputed outcome
    GET  /api/token                 what $MANDATE is for

Nothing here can move funds.
"""
from __future__ import annotations

import json
import os
import sys
import threading
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import basis, bond, book, chain, chains, spec, universe  # noqa: E402
from agent.executor import Executor  # noqa: E402

CHAIN = os.environ.get("MANDATE_CHAIN", "robinhood")

_lock = threading.Lock()
_universe: dict = {}
_universe_at = [0.0]
UNIVERSE_TTL = 120.0


def get_universe(force: bool = False) -> dict:
    """Resolution is expensive and moves slowly, so it is cached briefly."""
    import time
    with _lock:
        if force or not _universe or time.monotonic() - _universe_at[0] > UNIVERSE_TTL:
            _universe.clear()
            _universe.update(universe.build(chain_key=CHAIN))
            _universe_at[0] = time.monotonic()
        return dict(_universe)


class Handler(BaseHTTPRequestHandler):
    server_version = "Mandate/2.0"

    def log_message(self, fmt, *args):
        sys.stderr.write(f"{self.address_string()} {fmt % args}\n")

    def _send(self, code: int, payload):
        body = json.dumps(payload, default=str).encode()
        self.send_response(code)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.send_header("access-control-allow-origin", "*")
        self.send_header("access-control-allow-headers", "content-type")
        self.send_header("access-control-allow-methods", "GET,POST,OPTIONS")
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> dict:
        n = int(self.headers.get("content-length") or 0)
        return json.loads(self.rfile.read(n) or b"{}")

    def do_OPTIONS(self):
        self._send(204, {})

    def do_GET(self):
        path = urlparse(self.path).path.rstrip("/") or "/"
        try:
            if path == "/api/health":
                return self._send(200, self.health())
            if path == "/api/universe":
                return self._send(200, self.universe())
            if path == "/api/mandates":
                return self._send(200, {"mandates": book.read()["mandates"]})
            if path.startswith("/api/mandates/") and path.endswith("/audit"):
                return self._send(200, self.audit(path.split("/")[3]))
            if path.startswith("/api/mandates/"):
                m = book.get_mandate(path.split("/")[3])
                return self._send(200 if m else 404,
                                  {"mandate": m} if m else {"error": "unknown mandate"})
            if path.startswith("/api/portfolio/"):
                return self._send(200, self.portfolio(path.split("/")[3]))
            if path == "/api/leaderboard":
                return self._send(200, {"leaderboard": book.leaderboard(self.marks())})
            if path == "/api/token":
                return self._send(200, bond.spec())
            return self._send(404, {"error": "no such route", "path": path})
        except Exception as e:
            traceback.print_exc()
            return self._send(500, {"error": str(e)})

    def do_POST(self):
        path = urlparse(self.path).path.rstrip("/")
        try:
            body = self._body()
            if path == "/api/mandates":
                return self._send(200, self.publish(body))
            if path == "/api/subscribe":
                return self._send(200, self.subscribe(body))
            if path == "/api/run":
                return self._send(200, self.run(body))
            return self._send(404, {"error": "no such route", "path": path})
        except (spec.MandateError, ValueError) as e:
            return self._send(400, {"error": str(e)})
        except KeyError as e:
            return self._send(404, {"error": f"unknown {e}"})
        except Exception as e:
            traceback.print_exc()
            return self._send(500, {"error": str(e)})

    # -- handlers ------------------------------------------------------------
    def marks(self) -> dict:
        return {t: i.price for t, i in universe.tradeable(get_universe()).items()}

    def health(self) -> dict:
        u = get_universe()
        c = chains.get(CHAIN)
        return {
            "ok": True,
            "chain": {"key": c.key, "name": c.name, "id": c.chain_id,
                      "block": chain.block_number(CHAIN)},
            "session": basis.session_state().to_dict(),
            "universe": {"verified": len(universe.tradeable(u)), "resolved": len(u)},
            "custody": "none. this service never holds a key or a balance.",
        }

    def universe(self) -> dict:
        u = get_universe()
        return {
            "chain": CHAIN,
            "instruments": {k: v.to_dict() for k, v in u.items()},
            "impostors": universe.impostor_report(u),
            "session": basis.session_state().to_dict(),
        }

    def publish(self, body: dict) -> dict:
        u = get_universe()
        legs = []
        for leg in body["legs"]:
            sym = leg["symbol"].upper()
            inst = u.get(sym)
            if not inst or not inst.verified:
                raise ValueError(f"{sym} did not verify, refusing to build on it")
            legs.append(spec.Leg(sym, inst.address, int(leg["weight_bps"])))
        m = spec.Mandate(
            name=body["name"], strategist=body["strategist"],
            thesis=body["thesis"], legs=tuple(legs),
            execution=spec.Execution(**body.get("execution", {})),
            risk=spec.Risk(**body.get("risk", {})),
            fee_bps=int(body.get("fee_bps", 100)),
            chain_id=chains.get(CHAIN).chain_id)
        m.validate()
        res = book.put_mandate(m.to_dict())
        return {"stored": res["stored"], "mandate": res["mandate"],
                "signing_message": m.signing_message(),
                "canonical_rules": m.canonical().decode()}

    def subscribe(self, body: dict) -> dict:
        res = book.subscribe(body["mandate_id"], body["follower"],
                             float(body["sleeve_usd"]),
                             body.get("wallet_kind", "dynamic-delegated"))
        committed = sum(s["sleeve_usd"] for s in
                        book.subscriptions(body["mandate_id"], active_only=True))
        res["bond_required_usd"] = bond.required_bond(committed)
        res["committed_usd"] = committed
        return res

    def run(self, body: dict) -> dict:
        subs = book.subscriptions(body["mandate_id"], follower=body["follower"],
                                  active_only=True)
        if not subs:
            raise KeyError("active subscription")
        m = book.get_mandate(body["mandate_id"])
        if not m:
            raise KeyError("mandate")
        r = Executor(get_universe(), CHAIN).run(
            subs[0], m, posted_bond_usd=float(body.get("posted_bond_usd", 0)))
        return r.to_dict()

    def audit(self, mandate_id: str) -> dict:
        m = book.get_mandate(mandate_id)
        if not m:
            return {"error": "unknown mandate"}
        return bond.audit(m, book.fills(mandate_id=mandate_id), get_universe())

    def portfolio(self, address: str) -> dict:
        return book.portfolio(address, self.marks(), basis.session_state().open)


def main() -> int:
    port = int(os.environ.get("PORT", "8402"))
    host = os.environ.get("HOST", "127.0.0.1")
    print(f"Mandate API on http://{host}:{port}  (chain: {CHAIN})")
    print("resolving the universe off chain ...")
    u = get_universe(force=True)
    print(f"  {len(universe.tradeable(u))}/{len(u)} tickers verified")
    try:
        ThreadingHTTPServer((host, port), Handler).serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
