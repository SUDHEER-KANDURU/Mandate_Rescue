"""Capture two specific runtime states in the real browser UI:

  1. _ask_state.jpg  - the /api/ask panel showing a rendered result (the
     differentiated 'query assistant isn't configured' error when no LLM key is
     set, or a successful query when one is).
  2. _live_state.jpg - a live pipeline run paused mid-flight (~50/180) with all
     five counters (Processed + the four stage counts) in agreement.

Both are cropped to the relevant panel and kept well under the 8000px image limit.
"""
from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:5000/"
VIEWPORT_W = 1440


def launch(p):
    for launcher in (
        lambda: p.chromium.launch(),
        lambda: p.chromium.launch(channel="chrome"),
        lambda: p.chromium.launch(channel="msedge"),
    ):
        try:
            return launcher()
        except Exception as e:
            print("launch failed:", e)
    raise SystemExit("no browser available")


def shot_ask(page):
    page.goto(BASE, wait_until="networkidle")
    page.wait_for_timeout(800)
    # Click the first example chip to run a real /api/ask request.
    page.click("#ask-chips .chip")
    # Wait until the result box is populated and visible.
    page.wait_for_selector("#ask-result:not(.hidden)")
    page.wait_for_function(
        "document.getElementById('ask-result').textContent.trim().length > 0 && "
        "!document.querySelector('#ask-result .ask-loading')"
    )
    page.wait_for_timeout(400)
    # Crop to the Ask panel card (its parent section).
    card = page.query_selector("#ask-chips")
    # The Ask card is the section containing #ask-result; screenshot that section.
    section = page.evaluate_handle(
        "document.getElementById('ask-result').closest('section')"
    ).as_element()
    section.screenshot(path="_ask_state.jpg", type="jpeg", quality=85)
    txt = page.inner_text("#ask-result")
    print("ASK panel text:\n", txt)


def shot_live(page):
    page.goto(BASE, wait_until="networkidle")
    page.wait_for_timeout(500)
    # Kick off the live run.
    page.click("#btn-run")
    # Wait for the live panel to appear.
    page.wait_for_selector("#live-panel:not(.hidden)", timeout=15000)
    # Poll until the PROCESSED counter reaches the target, then capture immediately.
    # We can't truly "pause" the JS loop, but the counters are committed atomically,
    # so any frame is internally consistent. We grab the frame at >= 50.
    page.wait_for_function(
        "parseInt(document.getElementById('lc-processed').textContent, 10) >= 50",
        timeout=60000,
    )
    section = page.query_selector("#live-panel")
    section.screenshot(path="_live_state.jpg", type="jpeg", quality=85)

    # Read back all five counters to prove they agree.
    counters = page.evaluate(
        """() => {
            const proc = document.getElementById('lc-processed').textContent.trim();
            const stages = Array.from(
                document.querySelectorAll('#pipeline .pnode-count')
            ).map(n => n.textContent.trim());
            const count = document.getElementById('lc-count').textContent.trim();
            const rec = document.getElementById('lc-recovered').textContent.trim();
            const esc = document.getElementById('lc-escalated').textContent.trim();
            return { proc, stages, count, rec, esc };
        }"""
    )
    print("LIVE counters at capture:", counters)
    stage_set = set(counters["stages"]) | {counters["proc"]}
    print("Processed + 4 stage counts all agree:", len(stage_set) == 1,
          "->", counters["proc"], counters["stages"])


with sync_playwright() as p:
    browser = launch(p)
    page = browser.new_page(
        viewport={"width": VIEWPORT_W, "height": 1200}, device_scale_factor=1
    )
    shot_ask(page)
    shot_live(page)
    browser.close()
    print("saved _ask_state.jpg and _live_state.jpg")
