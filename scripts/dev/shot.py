"""Capture the dashboard for visual review.

The review model rejects any image whose width OR height exceeds 8000px. A
full-page shot of this ~15000px-tall dashboard blows past that. So we:
  1. Render one full-page PNG (_fullpage.png) that contains the whole page.
  2. Slice it locally with Pillow into vertical review tiles (_review_1.jpg ...),
     each well under 8000px tall, at readable resolution.
"""
import os
from playwright.sync_api import sync_playwright
from PIL import Image

VIEWPORT_W = 1440
TILE_H = 3400  # each tile height in px, safely under the 8000px limit
JPEG_QUALITY = 82

FULL_PNG = "_fullpage.png"


def capture():
    with sync_playwright() as p:
        browser = None
        for launcher in (
            lambda: p.chromium.launch(),
            lambda: p.chromium.launch(channel="chrome"),
            lambda: p.chromium.launch(channel="msedge"),
        ):
            try:
                browser = launcher()
                break
            except Exception as e:
                print("launch failed:", e)
        if browser is None:
            raise SystemExit("no browser available")

        page = browser.new_page(
            viewport={"width": VIEWPORT_W, "height": 1000}, device_scale_factor=1
        )
        page.goto("http://127.0.0.1:5000/", wait_until="networkidle")
        page.wait_for_timeout(1500)
        page.screenshot(path=FULL_PNG, full_page=True)
        browser.close()


def slice_tiles():
    # Remove stale tiles from prior runs.
    for f in os.listdir("."):
        if f.startswith("_review_") and f.endswith(".jpg"):
            os.remove(f)

    img = Image.open(FULL_PNG).convert("RGB")
    w, h = img.size
    print("full image size:", w, "x", h)

    tiles = []
    idx = 1
    y = 0
    while y < h:
        bottom = min(y + TILE_H, h)
        tile = img.crop((0, y, w, bottom))
        out = f"_review_{idx}.jpg"
        tile.save(out, "JPEG", quality=JPEG_QUALITY)
        tiles.append(out)
        y = bottom
        idx += 1
    return tiles


if __name__ == "__main__":
    capture()
    tiles = slice_tiles()
    print("saved", FULL_PNG, "and tiles:", ", ".join(tiles))
