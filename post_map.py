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
            device_scale_factor=2,  # 2x resolution: sharp when zoomed in Discord
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/129.0 Safari/537.36"
            ),
        )
        page.goto(MAP_URL, wait_until="domcontentloaded", timeout=60_000)
        page.wait_for_selector("#multiplayer .grid-wrapper .main-cell", timeout=60_000)
        page.wait_for_timeout(3000)  # let the icons finish loading

        updated = (
            page.locator("#multiplayer p", has_text="Updated for")
            .first.inner_text()
            .strip()
        )
        # The page has several "desert-content-block" sections; use the one
        # that actually contains the map grid (grid + icon legend).
        block = page.locator(
            "#multiplayer .desert-content-block",
            has=page.locator(".grid-wrapper"),
        ).first

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

        # PvE loot tables: one section per source (All Schematics, Downed
        # Ships, each Testing Station, each Wreck), each with tier-tagged tiles.
        # The page holds a PvE set and a PvP set (data-loot="pve" / "pvp").
        loot = page.eval_on_selector_all(
            "#multiplayer .loot-tables-wrapper .loot-section-block",
            """secs => secs.map(s => ({
                mode: (s.closest('.loot-tables-wrapper')?.getAttribute('data-loot') || '').toUpperCase(),
                title: (s.querySelector('.loot-section-title')?.textContent || '').trim(),
                items: [...s.querySelectorAll('.loot-tile')].map(t => ({
                    tier: ([...t.classList].find(c => /-tier$/.test(c)) || '')
                            .replace('-tier', '').toUpperCase(),
                    name: (t.querySelector('.loot-name')?.textContent || '').trim(),
                    chance: (t.querySelector('.loot-drop-chance')?.textContent || '').trim(),
                }))
            }))""",
        )

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
    return updated, pois, loot


TOP_TIERS = ("S", "A")
TIER_ICON = {"S": "🟥", "A": "🟧", "B": "🟨", "C": "🟩"}


def _clip(text, limit):
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _source_label(title, pois):
    """Method lists shared tables under one example location; give them a
    group name plus the squares from this week's map."""
    t = title.lower()
    if t.startswith("downed ship"):
        return "✈️ Downed ships"
    if t.startswith("testing station"):
        squares = [s for s in pois.get(ICON_LABELS["testing-station"], []) if not s.startswith("A")]
        return "🧪 Testing stations" + (f" ({', '.join(sorted(squares))})" if squares else "")
    if t.startswith("wreck ("):
        name = title.split(" - ", 1)[1] if " - " in title else "Wrecks"
        squares = [s for s in pois.get(ICON_LABELS["shipwreck"], []) if not s.startswith("A")]
        return f"🚢 Wrecks – {name}" + (f" ({', '.join(sorted(squares))})" if squares else "")
    return title


def build_loot_embeds(loot, pois):
    """Turn Method's loot sections into Discord embeds (S and A tiers only),
    one schematics overview + one 'where to farm' card per mode (PvE, PvP)."""
    embeds = []
    modes = [m for m in ("PVE", "PVP") if any(s["mode"] == m for s in loot)] or [""]

    for mode in modes:
        secs = [s for s in loot if s["mode"] == mode]
        label = {"PVE": "PvE", "PVP": "PvP"}.get(mode, "")
        color = 0xF1C40F if mode != "PVP" else 0x992D22

        # 1) This week's best schematics (from the "All Schematics" section)
        overview = next((s for s in secs if s["title"].lower() == "all schematics"), None)
        if overview:
            lines = []
            for tier in TOP_TIERS:
                names = sorted({i["name"] for i in overview["items"] if i["tier"] == tier and i["name"]})
                if names:
                    lines.append(f"{TIER_ICON[tier]} **{tier}-tier:** " + ", ".join(names))
            counts = defaultdict(int)
            for i in overview["items"]:
                if i["tier"] in TIER_ICON:
                    counts[i["tier"]] += 1
            summary = " · ".join(f"{t}: {counts[t]}" for t in ("S", "A", "B", "C") if counts[t])
            if lines:
                embeds.append({
                    "title": f"📜 This week's unique schematics — {label}",
                    "description": _clip("\n".join(lines) + f"\n\n*Total by tier — {summary}*", 4000),
                    "color": color,
                })

        # 2) Where to farm: S/A drops per source, with drop chance.
        #    Row A (Mk5) tables have no S/A tiers, so they drop out here.
        fields = []
        for sec in secs:
            if not sec["title"] or sec["title"].lower() == "all schematics":
                continue
            top = [i for i in sec["items"] if i["tier"] in TOP_TIERS and i["name"]]
            if not top:
                continue
            def _pct(c):
                c = c.replace("%", "").strip()
                return 0.5 if c.startswith("<") else float(c or 0)

            # Best tier first, then highest drop chance
            top.sort(key=lambda i: (TOP_TIERS.index(i["tier"]), -_pct(i["chance"]), i["name"]))
            value = "\n".join(
                f"{TIER_ICON[i['tier']]} {i['name']}" + (f" — {i['chance']}" if i["chance"] else "")
                for i in top
            )
            fields.append({
                "name": _clip(_source_label(sec["title"], pois), 250),
                "value": _clip(value, 1020),
                "inline": False,
            })
        if fields:
            embeds.append({
                "title": f"🎯 Where to farm — {label} (S & A tier drop chances)",
                "fields": fields[:25],
                "color": color,
            })

    if embeds:
        embeds[-1]["footer"] = {"text": "B & C tiers and Row A (Mk5) tables: full list on Method.gg"}
        embeds[-1]["url"] = MAP_URL
    return embeds


def post_loot(embeds):
    """Send loot embeds as follow-up messages (one embed per message keeps
    every message well under Discord's size limits)."""
    webhook = os.environ["DISCORD_WEBHOOK_URL"].strip()
    for e in embeds:
        r = requests.post(
            webhook,
            params={"wait": "true"},
            json={
                "username": "Deep Desert Scout",
                "embeds": [e],
                "allowed_mentions": {"parse": []},
            },
            timeout=60,
        )
        print(f"Loot post: Discord replied {r.status_code}")
        r.raise_for_status()


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
    webhook = os.environ["DISCORD_WEBHOOK_URL"].strip()
    image_bytes = IMAGE_FILE.read_bytes()
    print(f"Screenshot size: {len(image_bytes)} bytes")
    if len(image_bytes) < 5_000:
        raise RuntimeError("Screenshot looks empty — not posting.")

    payload = {
        "username": "Deep Desert Scout",
        "content": message,
        "allowed_mentions": {"parse": []},
        # Declare the file explicitly and show it as the embed image,
        # so Discord can't drop it.
        # Declared as a plain attachment (not inside an embed card) so
        # Discord shows it at full size instead of shrinking it.
        "attachments": [{"id": 0, "filename": "deep_desert_map.png"}],
    }
    r = requests.post(
        webhook,
        params={"wait": "true"},
        data={"payload_json": json.dumps(payload)},
        files={"files[0]": ("deep_desert_map.png", image_bytes, "image/png")},
        timeout=60,
    )
    print(f"Discord replied {r.status_code}")
    r.raise_for_status()
    sent = r.json()
    print(f"Attachments on posted message: {len(sent.get('attachments', []))}")
    if not sent.get("attachments"):
        raise RuntimeError(f"Discord accepted the post but dropped the image: {r.text[:500]}")


def main():
    updated, pois, loot = grab_map()
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

    # Loot tables are a bonus: if Method changes that part of the page,
    # still count the map as posted instead of failing the whole run.
    try:
        embeds = build_loot_embeds(loot, pois)
        print(f"Loot sections found: {len(loot)}, embeds to post: {len(embeds)}")
        post_loot(embeds)
    except Exception as e:  # noqa: BLE001
        print(f"Loot tables skipped: {e}")
    print("Posted to Discord.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
