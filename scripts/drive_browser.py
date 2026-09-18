#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Drive the app in a real browser and report what it actually does.

The Python engine and the browser client are the same logic twice. Running one
of them is not evidence about the other. Three bugs in this project were only
visible by opening the page: a UTF-8 name rendered as mojibake, a CORS-less
endpoint that emptied a scan while passing in node, plus an endpoint rotation
that moved a pool count without reporting a single failure.

Usage:
    cd web && python3 -m http.server 8777 --bind 127.0.0.1 &
    python3 scripts/drive_browser.py
"""
import asyncio
import sys

URL = "http://127.0.0.1:8777/"


async def main() -> int:
    from playwright.async_api import async_playwright

    errors, console = [], []
    async with async_playwright() as p:
        b = await p.chromium.launch(args=["--no-sandbox"])
        pg = await b.new_page(viewport={"width": 1400, "height": 1100})
        pg.on("console", lambda m: console.append(f"{m.type}: {m.text}"))
        pg.on("pageerror", lambda e: errors.append(str(e)))
        await pg.goto(URL, wait_until="networkidle", timeout=120_000)
        await pg.wait_for_timeout(45_000)          # let the universe resolve

        cards = await pg.eval_on_selector_all(
            "#universe .card", "els=>els.map(e=>e.innerText.replace('\\n',' | '))")
        print("UNIVERSE:", cards)
        print("HUD:", (await pg.inner_text("#hud")).replace("\n", " / "))

        await pg.click('nav button[data-t="build"]')
        await pg.wait_for_timeout(500)
        await pg.click("#b_go")
        await pg.wait_for_timeout(4_000)
        print("PUBLISH:", (await pg.inner_text("#b_out"))[:120].replace("\n", " / "))

        await pg.click('nav button[data-t="run"]')
        await pg.wait_for_timeout(500)
        await pg.click("#r_go")
        await pg.wait_for_timeout(90_000)
        steps = await pg.eval_on_selector_all(
            ".step", "els=>els.map(e=>e.innerText.replace(/\\n/g,' '))")
        print("RUN STEPS:")
        for s in steps[:12]:
            print("   ", s[:132])

        await pg.click('nav button[data-t="folio"]')
        await pg.wait_for_timeout(2_500)
        print("PORTFOLIO:", await pg.eval_on_selector_all(
            "#folio .card", "els=>els.map(e=>e.innerText.replace('\\n',' | '))"))

        await pg.click('nav button[data-t="ghosts"]')
        await pg.wait_for_timeout(500)
        await pg.click("#g_go")
        await pg.wait_for_timeout(90_000)
        print("GHOSTS:", await pg.eval_on_selector_all(
            "#g_out .card", "els=>els.map(e=>e.innerText.replace('\\n',' | '))"))
        await pg.screenshot(path="/tmp/mandate-final.png", full_page=False)

        rate = sum(1 for c in console if "429" in c)
        gone = sum(1 for c in console if "410" in c)
        cors = sum(1 for c in console if "CORS" in c)
        print("PAGE ERRORS:", errors or "none")
        print(f"CONSOLE: {rate} rate-limit, {gone} gone, {cors} CORS")
        await b.close()
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
