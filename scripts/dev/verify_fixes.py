"""End-to-end browser verification of the 5 fixes using Playwright.

Item 1: header is position:sticky and stays pinned (top ~0) at the bottom of the
        Cases section with 180 rows.
Item 2: run the agent 3 times from a fresh reset; each time the pipeline reaches
        180/180 and the Communication (stage 4) node is active, and run-complete shows.
Item 4: clicking "Run chaos suite" shows the spinner + loading text while in flight,
        then renders 7 scenarios.
Also captures screenshots for the report.
"""
import time
import urllib.request
from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:5000/"
VW, VH = 1440, 900


def post(path):
    req = urllib.request.Request(BASE.rstrip("/") + path, method="POST")
    with urllib.request.urlopen(req, timeout=600) as r:
        return r.read()


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


def header_state(page, label):
    s = page.evaluate(
        """() => {
            const h = document.querySelector('header.topbar');
            const cs = getComputedStyle(h);
            const r = h.getBoundingClientRect();
            return {position: cs.position, top: r.top, bottom: r.bottom,
                    scrollY: window.scrollY};
        }"""
    )
    pinned = abs(s["top"]) < 2  # top edge at/near viewport top
    print(f"[{label}] header position={s['position']} rect.top={s['top']:.1f} "
          f"scrollY={s['scrollY']:.0f} pinned={pinned}")
    return s, pinned


with sync_playwright() as p:
    browser = launch(p)
    context = browser.new_context(viewport={"width": VW, "height": VH},
                                  device_scale_factor=1)
    # Force no-cache so edited CSS/JS is always fresh.
    context.route("**/*", lambda route: route.continue_(
        headers={**route.request.headers, "cache-control": "no-cache"}))
    page = context.new_page()
    console_errors = []
    page.on("console", lambda m: console_errors.append(m.text) if m.type == "error" else None)
    page.on("pageerror", lambda e: console_errors.append("PAGEERROR: " + str(e)))

    # Seed + one run so the Cases table has 180 rows.
    post("/api/reset")

    # ---------- Item 2: run the agent 3 times ----------
    page.goto(BASE + "#overview", wait_until="domcontentloaded")
    page.wait_for_timeout(800)
    for i in range(1, 4):
        # Fresh reset via the header button each time.
        page.click("#btn-reset")
        page.wait_for_timeout(2500)  # let reseed + dashboard reload settle
        page.click("#btn-run")
        # Wait until run-complete appears (the run visibly finished) OR timeout.
        try:
            page.wait_for_selector("#run-complete:not(.hidden)", timeout=120000)
        except Exception as e:
            print(f"RUN {i}: run-complete did NOT appear: {e}")
        # Read the live counters + pipeline stage-4 (communication) state.
        state = page.evaluate(
            """() => {
                const proc = document.getElementById('lc-processed').textContent;
                const count = document.getElementById('lc-count').textContent;
                const rcProc = document.getElementById('rc-processed').textContent;
                const rcRec = document.getElementById('rc-recovered').textContent;
                const rcEsc = document.getElementById('rc-escalated').textContent;
                const comm = document.querySelector('#pipeline .pnode[data-stage="communication"]');
                const commActive = comm ? comm.classList.contains('active') : false;
                const commCount = document.querySelector('#pipeline .pnode-count[data-count="communication"]').textContent;
                return {proc, count, rcProc, rcRec, rcEsc, commActive, commCount};
            }"""
        )
        print(f"RUN {i}: rc-processed={state['rcProc']} recovered={state['rcRec']} "
              f"escalated={state['rcEsc']} | live count={state['count']} "
              f"communication active={state['commActive']} stage4-count={state['commCount']}")

    # ---------- Item 1: sticky header at bottom of Cases (180 rows) ----------
    page.click('.nav-item[data-view="cases"]')
    page.wait_for_selector("#view-cases.active", timeout=10000)
    page.wait_for_function("document.querySelectorAll('#cases-tbody tr').length > 0",
                           timeout=20000)
    rows = page.evaluate("() => document.querySelectorAll('#cases-tbody tr').length")
    print(f"Cases table rows: {rows}")
    header_state(page, "cases-top")
    # Scroll to the very bottom of the page.
    page.evaluate("() => window.scrollTo(0, document.body.scrollHeight)")
    page.wait_for_timeout(600)
    s_bottom, pinned_bottom = header_state(page, "cases-bottom")
    page.screenshot(path="_verify_header_bottom.jpg", type="jpeg", quality=85)

    # ---------- Item 4: chaos loading state ----------
    page.evaluate("() => window.scrollTo(0, 0)")
    page.click('.nav-item[data-view="chaos"]')
    page.wait_for_selector("#view-chaos.active", timeout=10000)
    page.wait_for_timeout(300)
    page.click("#chaos-run-btn")
    # Immediately check the loading state is visible (spinner + text) before results.
    page.wait_for_timeout(120)
    loading = page.evaluate(
        """() => {
            const ph = document.getElementById('chaos-placeholder');
            const spin = ph.querySelector('.spinner');
            const btn = document.getElementById('chaos-run-btn');
            return {phVisible: !ph.classList.contains('hidden'),
                    hasSpinner: !!spin,
                    text: ph.textContent.trim(),
                    btnDisabled: btn.disabled,
                    btnLabel: btn.textContent};
        }"""
    )
    print(f"CHAOS loading: visible={loading['phVisible']} spinner={loading['hasSpinner']} "
          f"btnDisabled={loading['btnDisabled']} text={loading['text']!r}")
    page.screenshot(path="_verify_chaos_loading.jpg", type="jpeg", quality=85)
    page.wait_for_selector("#chaos-results:not(.hidden)", timeout=120000)
    page.wait_for_function(
        "document.querySelectorAll('#chaos-results .chaos-scenario').length === 7",
        timeout=120000)
    n_scen = page.evaluate("() => document.querySelectorAll('#chaos-results .chaos-scenario').length")
    print(f"CHAOS results rendered scenarios: {n_scen}")

    print("CONSOLE ERRORS:", console_errors if console_errors else "none")
    browser.close()
