"""Phase 2 verification: command palette (Cmd/Ctrl+K).

Verifies open/close, filtering, arrow navigation, and selection, then captures a
screenshot of the palette open with a mix of section + action + case results.
"""
import urllib.request
from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:5000/"


def post(path):
    req = urllib.request.Request(BASE.rstrip("/") + path, method="POST")
    with urllib.request.urlopen(req, timeout=600) as r:
        return r.read()


def launch(p):
    for launcher in (lambda: p.chromium.launch(),
                     lambda: p.chromium.launch(channel="chrome"),
                     lambda: p.chromium.launch(channel="msedge")):
        try:
            return launcher()
        except Exception as e:
            print("launch failed:", e)
    raise SystemExit("no browser available")


# Ensure there is data so case-search returns results.
post("/api/reset")
post("/api/run-agent")

with sync_playwright() as p:
    browser = launch(p)
    page = browser.new_page(viewport={"width": 1440, "height": 900}, device_scale_factor=1)
    page.goto(BASE, wait_until="domcontentloaded")
    page.wait_for_function("document.querySelectorAll('#cases-tbody tr').length > 0", timeout=20000)
    page.wait_for_timeout(500)

    def is_open():
        return not page.eval_on_selector("#cmdk-overlay", "el => el.classList.contains('hidden')")

    # 1) Open with Ctrl+K.
    page.keyboard.press("Control+k")
    page.wait_for_selector("#cmdk-overlay:not(.hidden)", timeout=5000)
    print("open after Ctrl+K:", is_open())

    # 2) Empty query shows sections + actions (10 items: 7 sections + 3 actions).
    count_empty = page.eval_on_selector_all("#cmdk-list .cmdk-item", "els => els.length")
    print("results on empty query:", count_empty)

    # 3) Filtering: type a section fuzzy query.
    page.fill("#cmdk-input", "comp")
    page.wait_for_timeout(200)
    titles = page.eval_on_selector_all("#cmdk-list .cmdk-item .cmdk-title", "els => els.map(e => e.textContent)")
    print("results for 'comp':", titles)

    # 4) Close with Escape.
    page.keyboard.press("Escape")
    page.wait_for_timeout(150)
    print("open after Escape:", is_open())

    # 5) Re-open, search a customer id substring, capture the screenshot.
    page.keyboard.press("Control+k")
    page.wait_for_selector("#cmdk-overlay:not(.hidden)", timeout=5000)
    # Use a query that yields sections + actions + cases together: "run" hits the
    # Run action; but to show cases we search a common id prefix. Show a blended view
    # by searching "cust00" — matches many case ids; sections/actions won't match so
    # instead capture two states.

    # 5a) Screenshot: a section+action query ("re" -> Reports, Reset demo, etc.)
    page.fill("#cmdk-input", "re")
    page.wait_for_timeout(250)
    # Move selection down once to show keyboard highlight on a non-first row.
    page.keyboard.press("ArrowDown")
    page.wait_for_timeout(150)
    active = page.eval_on_selector("#cmdk-list .cmdk-item.active .cmdk-title", "e => e.textContent")
    print("active after one ArrowDown (query 're'):", active)
    page.screenshot(path="_p2_cmdk_actions.jpg", type="jpeg", quality=85)

    # 5b) Screenshot: a customer-id search showing case results.
    page.fill("#cmdk-input", "cust")
    # Wait until case results render (cases are pre-loaded, so this is quick).
    page.wait_for_function(
        "Array.from(document.querySelectorAll('#cmdk-list .cmdk-kind'))"
        ".some(e => e.textContent === 'Case')",
        timeout=8000,
    )
    page.wait_for_timeout(150)
    case_titles = page.eval_on_selector_all(
        "#cmdk-list .cmdk-item .cmdk-kind", "els => els.map(e => e.textContent)")
    print("kinds for 'cust':", case_titles[:12])
    page.screenshot(path="_p2_cmdk_cases.jpg", type="jpeg", quality=85)

    # 6) Selection performs navigation: pick the first result on a section query.
    page.fill("#cmdk-input", "chaos")
    page.wait_for_timeout(200)
    page.keyboard.press("Enter")
    page.wait_for_timeout(300)
    chaos_active = page.eval_on_selector("#view-chaos", "el => el.classList.contains('active')")
    print("chaos view active after selecting 'chaos':", chaos_active)
    print("palette closed after select:", not is_open())

    browser.close()
    print("saved _p2_cmdk_actions.jpg and _p2_cmdk_cases.jpg")
