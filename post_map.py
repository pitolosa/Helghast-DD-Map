"""
Helghast Deep Desert map poster.

Opens the Method.gg Deep Desert Companion, screenshots the current week's
map, lists every point of interest by grid square, and posts it to Discord
through a webhook. It only posts once per map week: the "Updated for" date
is saved in last_posted.txt, and runs are skipped until that date changes.
"""

import json
import os
import pathlib
import sys
from collections import defaultdict

import requests
from playwright.sync_api import sync_playwright

MAP_URL = "https://www.method.gg/dune-awakening/deep-desert-companion"
STATE_FILE = pathlib.Path("last_posted.txt")
IMAGE_FILE = pathlib.Path("deep_desert_map.png")

# Icon file name on Method.gg -> label used in the Discord post
ICON_LABELS = {
    "shipwreck": "🚢 Shipwrecks",
    "large-spice-field": "🟠 Large spice fields",
    "testing-station": "🧪 Testing stations",
    "cave": "🕳️ Caves",
    "titanium": "⚙️ Titanium",
    "stravidium": "💎 Stravidium",
    "taxi-service": "🚕 Taxis",
}

FORCE = os.environ.get("FORCE_POST", "false").lower() == "true"


def grab_map():
    """Return (updated_for_text, {label: [squares]}) and save the screenshot."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(
            viewport={"width": 1400, "height": 2200},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/129.0 Safari/537.36"
            ),
        )
        page.goto(MAP_URL, wait_until="domcontentloaded", timeout=60_000)
        page.wait_for_selector("#multiplayer .grid-wrapper .main-cell", timeout=60_000)
        page.wait_for_timeout(3000)  # let the icons finish loading

        block = page.locator("#multiplayer .desert-content-block").first
        updated = (
            block.locator("p", has_text="Updated for").first.inner_text().strip()
        )

        # Read every icon on the grid: data-main-cell="I:1" -> square "I1"
        cells = page.eval_on_selector_all(
            "#multiplayer .grid-wrapper .main-cell",
            """cells => cells.map(c => ({
                cell: c.getAttribute('data-main-cell'),
                icons: [...c.querySelectorAll('img.grid-icon')]
                        .map(i => i.src.split('/').pop().replace('.png', ''))
            }))""",
        )
        pois = defaultdict(list)
        for c in cells:
            square = (c["cell"] or "").replace(":", "")
            for icon in c["icons"]:
                label = ICON_LABELS.get(icon, icon.replace("-", " ").title())
                if square not in pois[label]:
                    pois[label].append(square)

        # Hide sticky ads, video players and cookie bars so they can't cover the map
        page.evaluate(
            """() => {
                for (const el of document.querySelectorAll('*')) {
                    const pos = getComputedStyle(el).position;
                    if (pos === 'fixed' || pos === 'sticky') el.style.display = 'none';
                }
                document.querySelectorAll('iframe, video').forEach(e => e.remove());
            }"""
        )
        block.scroll_into_view_if_needed()
        page.wait_for_timeout(500)
        block.screenshot(path=str(IMAGE_FILE))
        browser.close()
    return updated, pois


def build_message(updated, pois):
    lines = [
        "🌪️ **New Deep Desert map** — the Coriolis Storm has passed.",
        f"*{updated}*",
        "",
    ]
    order = list(ICON_LABELS.values())
    for label in sorted(pois, key=lambda l: order.index(l) if l in order else 99):
        squares = ", ".join(sorted(pois[label]))
        lines.append(f"**{label}:** {squares}")
    lines += ["", f"Full map + loot tables: <{MAP_URL}>"]
    return "\n".join(lines)


def post_to_discord(message):
    webhook = os.environ["DISCORD_WEBHOOK_URL"]
    with open(IMAGE_FILE, "rb") as f:
        r = requests.post(
            webhook + "?wait=true",
            data={
                "payload_json": json.dumps(
                    {
                        "username": "Deep Desert Scout",
                        "content": message,
                        "allowed_mentions": {"parse": []},
                    }
                )
            },
            files={"files[0]": ("deep_desert_map.png", f, "image/png")},
            timeout=60,
        )
    r.raise_for_status()


def main():
    updated, pois = grab_map()
    print(f"Method.gg says: {updated}")

    last = STATE_FILE.read_text().strip() if STATE_FILE.exists() else ""
    if updated == last and not FORCE:
        print("Already posted this week's map. Nothing to do.")
        return 0

    if not pois:
        print("No map icons found — page layout may have changed. Not posting.")
        return 1

    post_to_discord(build_message(updated, pois))
    STATE_FILE.write_text(updated + "\n")
    print("Posted to Discord.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
