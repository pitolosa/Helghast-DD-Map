"""
Helghast Deep Desert map poster.

Runs in two phases (see .github/workflows/deep-desert-map.yml):

  python post_map.py check
      Cheap, no browser. Looks at Method.gg to see if a new map week is out,
      and reads #deep-desert for Officer "!base" commands. Decides whether a
      new map needs to be drawn and writes that plan to plan.json.

  python post_map.py post
      Opens Method.gg in a headless browser, screenshots the map with the
      guild base marked on it, posts it to #deep-desert, then deletes the
      previous map post and the "!base" messages it just used.

Officer / Autarch commands (typed in #deep-desert):
  !base D5                 set the base to D5 (replaces any other marker)
  !base D5 Spice Outpost   same, with a custom label
  !base add F7 Outpost     add a second marker, keep the others
  !base remove F7          remove the marker on F7
  !base clear              remove every marker
Markers reset when a new map week starts (the Coriolis storm wipes bases).
"""

import html
import json
import os
import pathlib
import re
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone

import requests

MAP_URL = "https://www.method.gg/dune-awakening/deep-desert-companion"
GUILD_ID = os.environ.get("DISCORD_GUILD_ID", "1558834458382180393")
CHANNEL_ID = os.environ.get("DISCORD_CHANNEL_ID", "1559637014477799534")
COMMAND_ROLES = {"officer", "autarch"}  # role names allowed to use !base
DEFAULT_LABEL = "Helghast Base"

STATE_FILE = pathlib.Path("state.json")
OLD_STATE_FILE = pathlib.Path("last_posted.txt")  # from the first version
PLAN_FILE = pathlib.Path("plan.json")
IMAGE_FILE = pathlib.Path("deep_desert_map.png")
API = "https://discord.com/api/v10"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/129.0 Safari/537.36"
)

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
BOT_TOKEN = os.environ.get("DISCORD_BOT_TOKEN", "").strip()


# ----------------------------------------------------------------- helpers

def format_title(updated):
    """'Updated for: 13th October > 20th October' -> 'OCTOBER 13TH - OCTOBER 20TH'."""
    text = updated.split(":", 1)[-1]
    parts = [p.strip() for p in re.split(r">|–|—|-|\bto\b", text) if p.strip()]
    pretty = []
    for p in parts[:2]:
        m = re.match(r"(\d+\s*(?:st|nd|rd|th)?)\s+([A-Za-z]+)", p)
        pretty.append(f"{m.group(2)} {m.group(1).replace(' ', '')}".upper() if m else p.upper())
    return " - ".join(pretty) if pretty else text.strip().upper()


def same_week(a, b):
    return bool(a) and bool(b) and format_title(a) == format_title(b)


def load_state():
    state = {"week": "", "map_message_id": None, "last_command_id": None, "markers": []}
    if STATE_FILE.exists():
        try:
            state.update(json.loads(STATE_FILE.read_text()))
        except Exception as e:  # noqa: BLE001
            print(f"state.json unreadable, starting fresh: {e}")
    elif OLD_STATE_FILE.exists():
        state["week"] = OLD_STATE_FILE.read_text().strip()
        # The old version used markers.json; carry those over once.
        mf = pathlib.Path("markers.json")
        if mf.exists():
            try:
                state["markers"] = json.loads(mf.read_text()).get("markers", [])
            except Exception:  # noqa: BLE001
                pass
    return state


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=2) + "\n")


def parse_square(text):
    """'d5', 'D5', 'D-5', 'D:5', 'D 5' -> 'D5'; anything else -> None."""
    m = re.fullmatch(r"\s*([A-Ia-i])\s*[-:]?\s*([1-9])\s*", text or "")
    return f"{m.group(1).upper()}{m.group(2)}" if m else None


def parse_command(content):
    """Return (action, square, label) for a !base message, or None if it isn't one.
    action is 'set' | 'add' | 'remove' | 'clear' | 'bad'."""
    m = re.match(r"^\s*!base\b(.*)$", content or "", re.I | re.S)
    if not m:
        return None
    rest = m.group(1).strip()
    words = rest.split()
    if not words:
        return ("bad", None, None)
    verb = words[0].lower()
    if verb == "clear":
        return ("clear", None, None)
    if verb in ("add", "remove", "delete"):
        action, words = ("add" if verb == "add" else "remove"), words[1:]
    else:
        action = "set"
    if not words:
        return ("bad", None, None)
    # Square may be written as "D5", "D-5", "D 5", "D:5"
    square = parse_square(words[0])
    used = 1
    if not square and len(words) > 1:
        square = parse_square(words[0] + words[1])
        used = 2
    if not square:
        return ("bad", None, None)
    label = " ".join(words[used:]).strip()[:30] or DEFAULT_LABEL
    return (action, square, label)


def apply_command(markers, cmd):
    action, square, label = cmd
    if action == "clear":
        return []
    if action == "set":
        return [{"label": label, "square": square}]
    if action == "add":
        return [m for m in markers if m["square"] != square] + [{"label": label, "square": square}]
    if action == "remove":
        return [m for m in markers if m["square"] != square]
    return markers


# ----------------------------------------------------------------- Discord

def discord(method, path, **kw):
    """Call the Discord API as the bot, waiting out rate limits."""
    for _ in range(5):
        r = requests.request(
            method, API + path, timeout=30,
            headers={"Authorization": f"Bot {BOT_TOKEN}", "User-Agent": "HelghastMapBot (github, 1)"},
            **kw,
        )
        if r.status_code == 429:
            time.sleep(float(r.json().get("retry_after", 1)) + 0.2)
            continue
        return r
    return r


def officer_role_ids():
    r = discord("GET", f"/guilds/{GUILD_ID}/roles")
    r.raise_for_status()
    return {role["id"] for role in r.json() if role["name"].strip().lower() in COMMAND_ROLES}


def read_commands(state):
    """Read #deep-desert since the last check. Returns (commands, newest_id) where
    commands is a list of (message_id, (action, square, label)) from Officers."""
    params = {"limit": 100}
    if state.get("last_command_id"):
        params["after"] = state["last_command_id"]
    r = discord("GET", f"/channels/{CHANNEL_ID}/messages", params=params)
    if r.status_code != 200:
        print(f"Couldn't read #deep-desert ({r.status_code}): {r.text[:300]}")
        return [], state.get("last_command_id")
    msgs = sorted(r.json(), key=lambda m: int(m["id"]))
    newest = msgs[-1]["id"] if msgs else state.get("last_command_id")

    candidates = [(m, parse_command(m.get("content", ""))) for m in msgs if not m.get("webhook_id")]
    candidates = [(m, c) for m, c in candidates if c]
    if not candidates:
        return [], newest

    allowed = officer_role_ids()
    member_roles = {}
    cmds = []
    for m, c in candidates:
        uid = m["author"]["id"]
        if uid not in member_roles:
            mr = discord("GET", f"/guilds/{GUILD_ID}/members/{uid}")
            member_roles[uid] = set(mr.json().get("roles", [])) if mr.status_code == 200 else set()
        who = m["author"].get("global_name") or m["author"]["username"]
        if not (member_roles[uid] & allowed):
            print(f"Ignoring !base from {who}: not an Officer/Autarch")
            continue
        if c[0] == "bad":
            print(f"Ignoring unreadable command from {who}: {m['content']!r}")
            continue
        print(f"Command from {who}: {c}")
        cmds.append((m["id"], c))
    return cmds, newest


def fetch_week_text():
    """Read the 'Updated for ...' line from Method.gg without a browser.
    Returns None if the page can't be fetched or parsed."""
    try:
        r = requests.get(MAP_URL, headers={"User-Agent": UA}, timeout=30)
        r.raise_for_status()
    except Exception as e:  # noqa: BLE001
        print(f"Couldn't fetch Method.gg quickly: {e}")
        return None
    text = html.unescape(re.sub(r"<[^>]+>", " ", r.text))
    m = re.search(r"Updated for:?\s*([0-9]{1,2}\s*(?:st|nd|rd|th)?\s+[A-Za-z]+\s*[>–—-]\s*"
                  r"[0-9]{1,2}\s*(?:st|nd|rd|th)?\s+[A-Za-z]+)", text)
    return f"Updated for: {m.group(1).strip()}" if m else None


# ----------------------------------------------------------------- check

def check():
    state = load_state()
    week = fetch_week_text()
    print(f"Method.gg week: {week!r}  (last posted: {state['week']!r})")

    if week is None:
        # Couldn't tell from the quick check. Only spend browser time on the
        # days the map actually changes (Tue/Wed UTC); "post" re-checks.
        new_week = datetime.now(timezone.utc).weekday() in (1, 2)
        week_known = False
    else:
        new_week = not same_week(week, state["week"])
        week_known = True

    commands, newest = ([], state.get("last_command_id"))
    if BOT_TOKEN:
        commands, newest = read_commands(state)
    else:
        print("No DISCORD_BOT_TOKEN set: !base commands are off.")

    render = FORCE or bool(commands) or new_week
    plan = {
        "week_known": week_known,
        "week": week,
        "commands": commands,
        "newest_id": newest,
    }
    PLAN_FILE.write_text(json.dumps(plan, indent=2))

    if not render and newest != state.get("last_command_id"):
        state["last_command_id"] = newest  # skip past chat we've already read
        save_state(state)

    print(f"render={'true' if render else 'false'}")
    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a") as f:
            f.write(f"render={'true' if render else 'false'}\n")
    return 0


# ----------------------------------------------------------------- post

def grab_map(markers):
    """Screenshot the grid with markers drawn. Returns (updated_text, pois)."""
    from playwright.sync_api import sync_playwright  # only installed when needed

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(
            viewport={"width": 1400, "height": 2200},
            device_scale_factor=2,  # 2x resolution: sharp when zoomed in Discord
            user_agent=UA,
        )
        page.goto(MAP_URL, wait_until="domcontentloaded", timeout=60_000)
        page.wait_for_selector("#multiplayer .grid-wrapper .main-cell", timeout=60_000)
        page.wait_for_timeout(3000)  # let the icons finish loading

        updated = page.locator("#multiplayer p", has_text="Updated for").first.inner_text().strip()

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
        if markers:
            drawn = page.evaluate(
                """markers => markers.map(m => {
                    const cell = document.querySelector(
                        `#multiplayer .grid-wrapper .main-cell[data-main-cell="${m.square[0]}:${m.square.slice(1)}"]`);
                    if (!cell) return m.square + ':missing';
                    cell.style.position = 'relative';
                    cell.style.outline = '3px solid #ff2d2d';
                    cell.style.outlineOffset = '-3px';
                    cell.style.zIndex = '5';
                    const pin = document.createElement('div');
                    pin.textContent = '★';
                    pin.style.cssText = `position:absolute;left:50%;top:45%;
                        transform:translate(-50%,-50%);color:#ff2d2d;font-size:26px;
                        line-height:1;text-shadow:0 0 3px #000,0 0 3px #000;z-index:6;`;
                    const tag = document.createElement('div');
                    tag.textContent = m.label;
                    tag.style.cssText = `position:absolute;left:50%;bottom:2px;transform:translateX(-50%);
                        background:#ff2d2d;color:#fff;font:bold 10px/1.2 Arial,sans-serif;
                        padding:1px 4px;border-radius:3px;white-space:nowrap;z-index:7;`;
                    cell.appendChild(pin);
                    cell.appendChild(tag);
                    return m.square + ':ok';
                })""",
                markers,
            )
            print(f"Markers drawn: {drawn}")

        grid = page.locator("#multiplayer .desert-content-block .grid-wrapper").first
        grid.scroll_into_view_if_needed()
        page.wait_for_timeout(500)
        grid.screenshot(path=str(IMAGE_FILE))
        browser.close()
    return updated, pois


def build_message(updated, pois, markers):
    lines = [f"# 🌪️ DEEP DESERT MAP - {format_title(updated)}", ""]
    for m in markers:
        lines.append(f"**🏠 {m['label']}:** {m['square']}")
    order = list(ICON_LABELS.values())
    for label in sorted(pois, key=lambda l: order.index(l) if l in order else 99):
        lines.append(f"**{label}:** {', '.join(sorted(pois[label]))}")
    return "\n".join(lines)


# Shown under the picture (Discord puts embeds below attachments)
LINK_EMBED = {
    "title": "🔗 Full map + loot tables",
    "url": MAP_URL,
    "description": f"[Open the interactive Deep Desert map on Method.gg]({MAP_URL})",
    "color": 0xE74C3C,
}


def webhook_url():
    return os.environ["DISCORD_WEBHOOK_URL"].strip()


def webhook_id():
    m = re.search(r"/webhooks/(\d+)/", webhook_url())
    return m.group(1) if m else None


def post_to_discord(message):
    """Post one message (text + map picture + link card). Returns its id."""
    image_bytes = IMAGE_FILE.read_bytes()
    print(f"Screenshot size: {len(image_bytes)} bytes")
    if len(image_bytes) < 5_000:
        raise RuntimeError("Screenshot looks empty — not posting.")
    payload = {
        "username": "Deep Desert Scout",
        "content": message[:2000],
        "allowed_mentions": {"parse": []},
        "embeds": [LINK_EMBED],
        "attachments": [{"id": 0, "filename": "deep_desert_map.png"}],
    }
    r = requests.post(
        webhook_url(),
        params={"wait": "true"},
        data={"payload_json": json.dumps(payload)},
        files={"files[0]": ("deep_desert_map.png", image_bytes, "image/png")},
        timeout=60,
    )
    print(f"Discord replied {r.status_code}")
    r.raise_for_status()
    sent = r.json()
    if not sent.get("attachments"):
        raise RuntimeError(f"Discord accepted the post but dropped the image: {r.text[:500]}")
    return sent["id"]


def delete_old_maps(state, new_id):
    """Remove earlier map posts so only the newest one is in the channel."""
    if BOT_TOKEN:
        # The bot can see every earlier Scout post (including ones from before
        # this version kept track of ids). Pinned messages are never touched.
        r = discord("GET", f"/channels/{CHANNEL_ID}/messages", params={"limit": 100})
        if r.status_code == 200:
            wid = webhook_id()
            for m in r.json():
                if m["id"] != new_id and m.get("webhook_id") == wid and not m.get("pinned"):
                    d = discord("DELETE", f"/channels/{CHANNEL_ID}/messages/{m['id']}")
                    print(f"Deleted old map {m['id']}: {d.status_code}")
            return
    old = state.get("map_message_id")
    if old and old != new_id:
        d = requests.delete(f"{webhook_url()}/messages/{old}", timeout=30)
        print(f"Deleted old map {old}: {d.status_code}")


def post():
    state = load_state()
    plan = json.loads(PLAN_FILE.read_text()) if PLAN_FILE.exists() else {
        "week_known": False, "week": None, "commands": [], "newest_id": state.get("last_command_id")}
    commands = plan.get("commands", [])

    # Markers belong to one map week: start over when a new week begins,
    # then apply any commands that came in.
    markers = list(state.get("markers", []))
    week_changed = plan.get("week_known") and not same_week(plan["week"], state["week"])
    if week_changed:
        markers = []
    for _, cmd in commands:
        markers = apply_command(markers, tuple(cmd))

    updated, pois = grab_map(markers)
    print(f"Method.gg says: {updated}")

    if not plan.get("week_known") and not same_week(updated, state["week"]) and state["week"]:
        # The quick check couldn't read the page, but the browser shows a new week.
        markers = []
        for _, cmd in commands:
            markers = apply_command(markers, tuple(cmd))
        updated, pois = grab_map(markers)

    if same_week(updated, state["week"]) and not commands and not FORCE:
        print("Already posted this week's map and no new commands. Nothing to do.")
        state["last_command_id"] = plan.get("newest_id") or state.get("last_command_id")
        save_state(state)
        return 0
    if not pois:
        print("No map icons found — page layout may have changed. Not posting.")
        return 1

    new_id = post_to_discord(build_message(updated, pois, markers))
    print(f"Posted map {new_id}")

    try:
        delete_old_maps(state, new_id)
    except Exception as e:  # noqa: BLE001
        print(f"Couldn't delete old map: {e}")
    for mid, _ in commands:
        d = discord("DELETE", f"/channels/{CHANNEL_ID}/messages/{mid}")
        print(f"Deleted command {mid}: {d.status_code}")

    state.update({
        "week": updated,
        "map_message_id": new_id,
        "last_command_id": plan.get("newest_id") or state.get("last_command_id"),
        "markers": markers,
    })
    save_state(state)
    if OLD_STATE_FILE.exists():
        OLD_STATE_FILE.unlink()
    return 0


if __name__ == "__main__":
    phase = sys.argv[1] if len(sys.argv) > 1 else "post"
    sys.exit(check() if phase == "check" else post())
