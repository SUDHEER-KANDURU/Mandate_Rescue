"""Phase 1 visual review: capture the four requested app-shell states at 1440px.

  1. _p1_overview_empty.jpg  - Overview on load, empty state (before seeding).
  2. _p1_overview_run.jpg    - Overview after a full agent run (KPIs + charts).
  3. _p1_cases_drawer.jpg    - Cases section with the table and a case drawer open.
  4. _p1_chaos.jpg           - Chaos Suite view after running (7-scenario results).

Also prints the sidebar + content geometry at 1440px to prove the sidebar stays
visible and does not overlap/crowd the content.
"""
import time
import urllib.request
from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:5000/"
VIEWPORT_W = 1440
VIEWPORT_H = 900


def post(path):
    req = urllib.request.Request(BASE.rstrip("/") + path, method="POST")
    with urllib.request.urlopen(req, timeout=600) as r:
        body = r.read()
        print(f"POST {path} -> {r.status} {body[:120]!r}")
        return body


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


def geometry(page, label):
    g = page.evaluate(
        """() => {
            const sb = document.getElementById('sidebar').getBoundingClientRect();
            const ct = document.querySelector('.content').getBoundingClientRect();
            return {sb:{x:sb.x,w:sb.width,r:sb.right}, ct:{x:ct.x,w:ct.width,r:ct.right}, iw: window.innerWidth};
        }"""
    )
    overlap = g["sb"]["r"] > g["ct"]["x"] + 1
    print(f"[{label}] innerWidth={g['iw']} sidebar(x={g['sb']['x']:.0f} w={g['sb']['w']:.0f} right={g['sb']['r']:.0f}) "
          f"content(x={g['ct']['x']:.0f} w={g['ct']['w']:.0f}) overlap={overlap}")


with sync_playwright() as p:
    browser = launch(p)
    context = browser.new_context(viewport={"width": VIEWPORT_W, "height": VIEWPORT_H},
                                  device_scale_factor=1)
    context.route("**/api/**", lambda route: route.continue_(
        headers={**route.request.headers, "cache-control": "no-cache"}))
    page = context.new_page()

    # Seed fresh data + run the agent up front so the populated states are ready.
    post("/api/reset")
    post("/api/run-agent")

    # --- State 2: Overview after a run ---
    page.goto(BASE + "#overview", wait_until="domcontentloaded")
    page.wait_for_function(
        "!document.getElementById('dashboard').classList.contains('hidden') && "
        "document.getElementById('kpi-at-risk').textContent.indexOf('\u20B9') !== -1",
        timeout=25000,
    )
    page.wait_for_timeout(1500)
    geometry(page, "overview-run")
    page.screenshot(path="_p1_overview_run.jpg", type="jpeg", quality=85)

    # --- State 3: Cases section + open a case drawer ---
    page.goto(BASE, wait_until="domcontentloaded")
    page.wait_for_function(
        "document.querySelectorAll('#cases-tbody tr').length > 0", timeout=20000
    )
    # Navigate to Cases by clicking the sidebar item (real user path).
    page.click('.nav-item[data-view="cases"]')
    page.wait_for_selector("#view-cases.active", timeout=10000)
    page.wait_for_timeout(600)
    page.click("#cases-tbody tr:first-child")
    page.wait_for_selector("#drawer:not(.hidden)", timeout=10000)
    # Wait for the drawer body to populate (summary grid rendered).
    page.wait_for_function(
        "document.querySelector('#drawer-body .detail-grid') !== null", timeout=10000
    )
    page.wait_for_timeout(1000)
    geometry(page, "cases-drawer")
    page.screenshot(path="_p1_cases_drawer.jpg", type="jpeg", quality=85)

    # Close the drawer so its overlay doesn't intercept later clicks.
    page.click("#drawer-close")
    page.wait_for_selector("#drawer", state="hidden", timeout=5000)

    # --- State 4: Chaos Suite view after running ---
    page.click('.nav-item[data-view="chaos"]')
    page.wait_for_selector("#view-chaos.active", timeout=10000)
    page.wait_for_timeout(300)
    page.click("#chaos-run-btn")
    page.wait_for_selector("#chaos-results:not(.hidden)", timeout=120000)
    page.wait_for_function(
        "document.querySelectorAll('#chaos-results .chaos-scenario').length === 7", timeout=120000
    )
    page.wait_for_timeout(600)
    geometry(page, "chaos")
    page.screenshot(path="_p1_chaos.jpg", type="jpeg", quality=85)

    # --- State 1 (captured last to avoid caching a stale empty status): empty state ---
    # Clear all rows directly so /api/status reports seeded=false, then load fresh.
    import os, sys
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "backend"))
    import db as _db
    _conn = _db.get_connection()
    try:
        _db.init_db(_conn)
        _conn.execute("DELETE FROM audit_log")
        _conn.execute("DELETE FROM mandate_failures")
        _conn.commit()
    finally:
        _conn.close()

    page.goto(BASE, wait_until="domcontentloaded")
    page.wait_for_selector("#empty-state:not(.hidden)", timeout=10000)
    page.wait_for_timeout(800)
    geometry(page, "overview-empty")
    page.screenshot(path="_p1_overview_empty.jpg", type="jpeg", quality=85)

    # Restore a populated DB so the app is left in a usable state.
    post("/api/reset")
    post("/api/run-agent")

    browser.close()
    print("saved _p1_overview_empty.jpg, _p1_overview_run.jpg, _p1_cases_drawer.jpg, _p1_chaos.jpg")
