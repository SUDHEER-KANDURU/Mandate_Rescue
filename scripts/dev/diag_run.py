"""Diagnose the browser-only stall at ~50/180 during a live run.

Captures ALL console messages + page errors while a single run executes, and polls
the live counter so we can see where/if it stops advancing.
"""
import urllib.request
from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:5000/"


def post(path):
    req = urllib.request.Request(BASE.rstrip("/") + path, method="POST")
    with urllib.request.urlopen(req, timeout=600) as r:
        return r.read()


with sync_playwright() as p:
    browser = p.chromium.launch()
    context = browser.new_context(viewport={"width": 1440, "height": 900})
    context.route("**/*", lambda route: route.continue_(
        headers={**route.request.headers, "cache-control": "no-cache"}))
    page = context.new_page()
    logs = []
    page.on("console", lambda m: logs.append(f"[{m.type}] {m.text}"))
    page.on("pageerror", lambda e: logs.append(f"[PAGEERROR] {e}"))

    post("/api/reset")
    page.goto(BASE + "#overview", wait_until="domcontentloaded")
    page.wait_for_timeout(800)
    page.click("#btn-run")

    last = -1
    stalled_polls = 0
    for i in range(120):  # up to ~120 * 0.75s = 90s
        page.wait_for_timeout(750)
        info = page.evaluate(
            """() => ({
                count: document.getElementById('lc-count').textContent,
                proc: parseInt(document.getElementById('lc-processed').textContent,10)||0,
                complete: !document.getElementById('run-complete').classList.contains('hidden'),
                runBtnDisabled: document.getElementById('btn-run').disabled,
            })"""
        )
        if info["complete"]:
            print(f"COMPLETED at poll {i}: count={info['count']} runBtnDisabled={info['runBtnDisabled']}")
            break
        if info["proc"] == last:
            stalled_polls += 1
        else:
            stalled_polls = 0
        last = info["proc"]
        if stalled_polls == 4:
            print(f"STALLED: processed stuck at {info['proc']} (count={info['count']}) "
                  f"for ~3s at poll {i}")
        if stalled_polls >= 12:
            print(f"GAVE UP: processed stuck at {info['proc']} (count={info['count']})")
            break

    print("--- CONSOLE / ERRORS ---")
    for l in logs:
        print(l)
    browser.close()
