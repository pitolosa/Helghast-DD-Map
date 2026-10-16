"""
Helghast server status card.

Reads our world's live numbers from DuneStatus and keeps ONE message in
#server-status up to date (it edits its own message instead of posting new
ones). Optionally also renames a voice channel to show the player count in
the channel list, e.g. "🟢 Tagir: 180/1500".

Runs from .github/workflows/server-status.yml using the same
DISCORD_BOT_TOKEN as the map bot.
"""

import html
import json
import os
import re
import sys
import time
from datetime import datetime, timezone

import requests

WORLD = os.environ.get("DUNE_WORLD", "Tagir")
WORLD_URL = f"https://dunestatus.com/worlds/{WORLD.lower()}"
STATUS_CHANNEL_ID = os.environ.get("STATUS_CHANNEL_ID", "").strip()
COUNTER_CHANNEL_ID = os.environ.get("COUNTER_CHANNEL_ID", "").strip()
BOT_TOKEN = os.environ.get("DISCORD_BOT_TOKEN", "").strip()
API = "https://discord.com/api/v10"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/129.0 Safari/537.36 HelghastStatus/1.0"
)
CARD_MARKER = "Helghast server status"  # footer text used to find our message

# DuneStatus/Funcom status codes. 20 is what an online world reports.
STATUS_NAMES = {20: ("🟢", "Online", 0x2ECC71)}
DOWN = ("🔴", "Offline / maintenance", 0xE74C3C)


# ------------------------------------------------------------------ data

def fetch_world():
    """Return a dict with the world's status, players and sietches, or None."""
    try:
        r = requests.get(WORLD_URL, headers={"User-Agent": UA}, timeout=30)
        r.raise_for_status()
    except Exception as e:  # noqa: BLE001
        print(f"Couldn't load {WORLD_URL}: {e}")
        return None

    # The page embeds its data as escaped JSON; unescape the quotes.
    text = r.text.replace('\\"', '"')

    # Preferred: decode the "battlegroup" object the page is built from.
    dec = json.JSONDecoder()
    for m in re.finditer(r'"battlegroup":\{', text):
        try:
            bg, _ = dec.raw_decode(text, m.end() - 1)
        except ValueError:
            continue
        if str(bg.get("DisplayName", "")).strip().lower() != WORLD.lower():
            continue
        servers = [
            {"name": str(s.get("DisplayName", "?")).strip(),
             "players": int(s.get("CurrentConcurrentPlayerCount", 0)),
             "cap": int(s.get("MaxConcurrentPlayerCapacity", 0))}
            for s in bg.get("ActiveInitialServers", []) or []
        ]
        return {
            "status": bg.get("BattlegroupStatus"),
            "players": int(bg.get("BattlegroupCurrentActive", 0)),
            "capacity": int(bg.get("BattlegroupMaxPlayerCapacity", 0)),
            "activity": str(bg.get("Activity", "")).title(),
            "region": bg.get("DatacenterId", ""),
            "servers": servers,
        }

    # Fallback: pick the numbers out with patterns.
    name = re.escape(WORLD)
    hits = [m.start() for m in re.finditer(rf'"DisplayName":"{name}"', text)]
    for start in reversed(hits):  # last copy is the full data blob
        chunk = text[max(0, start - 600): start + 20000]
        cur = re.search(r'"BattlegroupCurrentActive":(\d+)', chunk)
        cap = re.search(r'"BattlegroupMaxPlayerCapacity":(\d+)', chunk)
        if not (cur and cap):
            continue
        status = re.search(r'"BattlegroupStatus":(\d+)', chunk)
        activity = re.search(r'"Activity":"(\w+)"', chunk[chunk.find('"DisplayName"'):])
        region = re.search(r'"DatacenterId":"([^"]+)"|terId":"([^"]+)"', chunk)
        servers = []
        arr = chunk.find('"ActiveInitialServers":[')
        if arr >= 0:
            for m in re.finditer(
                r'"CurrentConcurrentPlayerCount":(\d+),"MaxConcurrentPlayerCapacity":(\d+)'
                r'.*?"DisplayName":"([^"]+)"',
                chunk[arr:],
            ):
                servers.append({"name": m.group(3), "players": int(m.group(1)), "cap": int(m.group(2))})
                if len(servers) >= 60:
                    break
        return {
            "status": int(status.group(1)) if status else None,
            "players": int(cur.group(1)),
            "capacity": int(cap.group(1)),
            "activity": (activity.group(1) if activity else "").title(),
            "region": html.unescape(next((g for g in region.groups() if g), "")) if region else "",
            "servers": servers,
        }
    print("Page loaded but the world data wasn't found — DuneStatus may have changed its layout.")
    return None


def bar(n, total, width=12):
    filled = round(width * n / total) if total else 0
    return "█" * filled + "░" * (width - filled)


def build_embed(w):
    now = datetime.now(timezone.utc).isoformat()
    if w is None:
        return {
            "title": f"⚠️ {WORLD} — status unavailable",
            "description": f"Couldn't read live numbers right now. Check [DuneStatus]({WORLD_URL}).",
            "color": 0x95A5A6,
            "url": WORLD_URL,
            "footer": {"text": f"{CARD_MARKER} · updates about every 30 min"},
            "timestamp": now,
        }
    icon, label, color = STATUS_NAMES.get(w["status"], DOWN)
    pct = round(100 * w["players"] / w["capacity"]) if w["capacity"] else 0
    top = sorted(w["servers"], key=lambda s: -s["players"])[:5]
    busiest = "\n".join(f"**{s['name'].replace('Sietch ', '')}** — {s['players']}/{s['cap']}" for s in top) or "—"
    online_sietches = len(w["servers"])
    return {
        "title": f"{icon} {WORLD} — {label}",
        "url": WORLD_URL,
        "color": color,
        "description": (
            f"**Players:** {w['players']:,} / {w['capacity']:,}  ({pct}%)\n"
            f"`{bar(w['players'], w['capacity'])}`\n"
            f"**Activity:** {w['activity'] or '—'} · **Sietches online:** {online_sietches}"
        ),
        "fields": [{"name": "Busiest sietches", "value": busiest, "inline": False}],
        "footer": {"text": f"{CARD_MARKER} · data from DuneStatus · updates about every 30 min"},
        "timestamp": now,
    }


# ------------------------------------------------------------------ Discord

def discord(method, path, **kw):
    for _ in range(4):
        r = requests.request(
            method, API + path, timeout=30,
            headers={"Authorization": f"Bot {BOT_TOKEN}", "User-Agent": "HelghastStatus (github, 1)"},
            **kw,
        )
        if r.status_code == 429:
            wait = float(r.json().get("retry_after", 1))
            if wait > 30:  # channel renames can be limited for minutes; skip instead
                return r
            time.sleep(wait + 0.2)
            continue
        return r
    return r


def upsert_card(embed):
    me = discord("GET", "/users/@me").json()["id"]
    r = discord("GET", f"/channels/{STATUS_CHANNEL_ID}/messages", params={"limit": 50})
    r.raise_for_status()
    mine = [
        m for m in r.json()
        if m["author"]["id"] == me
        and any(CARD_MARKER in (e.get("footer") or {}).get("text", "") for e in m.get("embeds", []))
    ]
    body = {"embeds": [embed], "allowed_mentions": {"parse": []}}
    if mine:
        keep = mine[0]  # newest
        e = discord("PATCH", f"/channels/{STATUS_CHANNEL_ID}/messages/{keep['id']}", json=body)
        print(f"Edited status card {keep['id']}: {e.status_code}")
        e.raise_for_status()
        for extra in mine[1:]:  # tidy up any duplicates
            discord("DELETE", f"/channels/{STATUS_CHANNEL_ID}/messages/{extra['id']}")
    else:
        p = discord("POST", f"/channels/{STATUS_CHANNEL_ID}/messages", json=body)
        print(f"Posted status card: {p.status_code} {p.text[:200]}")
        p.raise_for_status()


def update_counter(w):
    if not COUNTER_CHANNEL_ID:
        return
    if w is None:
        name = f"⚠️ {WORLD}: status unknown"
    else:
        icon = STATUS_NAMES.get(w["status"], DOWN)[0]
        name = f"{icon} {WORLD}: {w['players']}/{w['capacity']}"
    cur = discord("GET", f"/channels/{COUNTER_CHANNEL_ID}")
    if cur.status_code == 200 and cur.json().get("name") == name:
        print("Counter channel already up to date.")
        return
    r = discord("PATCH", f"/channels/{COUNTER_CHANNEL_ID}", json={"name": name})
    print(f"Counter channel -> {name!r}: {r.status_code}")


def main():
    if not BOT_TOKEN or not STATUS_CHANNEL_ID:
        print("DISCORD_BOT_TOKEN and STATUS_CHANNEL_ID are required.")
        return 1
    w = fetch_world()
    print(f"World data: {json.dumps({k: v for k, v in (w or {}).items() if k != 'servers'})}"
          f" · {len((w or {}).get('servers', []))} sietches")
    upsert_card(build_embed(w))
    try:
        update_counter(w)
    except Exception as e:  # noqa: BLE001 - the counter is a bonus
        print(f"Counter update skipped: {e}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
