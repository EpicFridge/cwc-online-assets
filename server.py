"""
Witch's Castle Multiplayer server
- Serves index.html
- Knows which characters exist (one per .skel) by reading, in this order:
    1. a local ./cookies folder (if you have one)
    2. your GitHub repo (the character files are hosted there with GitHub Pages)
    3. ./characters.txt (one name per line)  -> manual fallback
    4. ./characters_cache.json               -> last successful list
- Handles the multiplayer WebSocket at /ws
"""
import asyncio
import hmac
import ipaddress
import json
import math
import os
import random
import re
import sys
import time
import urllib.request

from aiohttp import web, WSMsgType

BASE = os.path.dirname(os.path.abspath(__file__))
LOCAL_DIR = os.path.join(BASE, "cookies")             # optional local copy of the character files
CHAR_TXT = os.path.join(BASE, "characters.txt")
CHAR_CACHE_FILE = os.path.join(BASE, "characters_cache.json")
VENDOR_DIR = os.path.join(BASE, "vendor")
PORT = int(os.environ.get("PORT", "8080"))
# Where the character files (.skel/.atlas/.png) are hosted (GitHub Pages). The browser downloads them from there.
ASSET_BASE = os.environ.get("CWC_ASSET_BASE", "https://epicfridge.github.io/cwc-online-assets/cookies").strip().rstrip("/")
# Used only to LIST the characters (GitHub API): owner/repo and the folder inside it.
ASSET_REPO = os.environ.get("CWC_REPO", "epicfridge/cwc-online-assets").strip()
ASSET_PATH = os.environ.get("CWC_PATH", "cookies").strip().strip("/")
MAX_PLAYERS = 40
MAX_PER_IP = int(os.environ.get("MAX_PER_IP", "2"))                 # connections allowed per network
OWNER_CODE = os.environ.get("OWNER_CODE", "epicfridge:067067")      # type this as the username to become owner
OWNER_NAME = "epicfridge (OWNER)"
ALLOWED_EXT = {".skel", ".atlas", ".png"}
NAME_RE = re.compile(r"^[A-Za-z0-9_\-]{1,64}$")

# Spine 4.1 web runtime + three.js are downloaded once into ./vendor
VENDOR = {
    "three.min.js": [
        "https://cdnjs.cloudflare.com/ajax/libs/three.js/r128/three.min.js",
        "https://cdn.jsdelivr.net/npm/three@0.128.0/build/three.min.js",
    ],
    "spine-webgl.js": [
        "https://cdn.jsdelivr.net/npm/@esotericsoftware/spine-webgl@4.1.24/dist/iife/spine-webgl.js",
        "https://unpkg.com/@esotericsoftware/spine-webgl@4.1.24/dist/iife/spine-webgl.js",
        "https://cdn.jsdelivr.net/npm/@esotericsoftware/spine-webgl@4.1/dist/iife/spine-webgl.js",
    ],
}
VENDOR_CHECK = {"three.min.js": b"THREE", "spine-webgl.js": b"spine"}


def ensure_vendor():
    os.makedirs(VENDOR_DIR, exist_ok=True)
    for fname, urls in VENDOR.items():
        path = os.path.join(VENDOR_DIR, fname)
        if os.path.isfile(path) and os.path.getsize(path) > 10000:
            continue
        ok = False
        for url in urls:
            try:
                print(f"[vendor] downloading {fname} from {url}")
                req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
                with urllib.request.urlopen(req, timeout=30) as r:
                    data = r.read()
                if len(data) > 10000 and VENDOR_CHECK[fname] in data:
                    with open(path, "wb") as f:
                        f.write(data)
                    ok = True
                    print(f"[vendor] saved {fname} ({len(data)//1024} KB)")
                    break
            except Exception as e:  # noqa
                print(f"[vendor] failed: {e}")
        if not ok:
            print(f"[vendor] WARNING: could not download {fname}. The browser will try a CDN instead.")


# ---------------------------------------------------------------- characters
CHARS = []          # sorted character names (one per .skel)
CHAR_SET = set()
_last_try = 0.0


def natural_key(t):
    return [int(x) if x.isdigit() else x.lower() for x in re.split(r"(\d+)", t)]


def scan_local():
    out = []
    if os.path.isdir(LOCAL_DIR):
        files = set(os.listdir(LOCAL_DIR))
        for f in files:
            if f.lower().endswith(".skel"):
                n = f[:-5]
                if (n + ".atlas") in files and (n + ".png") in files:
                    out.append(n)
    return out


def fetch_github_names():
    """Lists <ASSET_PATH>/NAME.skel|atlas|png in the repo with the GitHub git-trees API (works for big folders)."""
    for ref in ("HEAD", "main", "master"):
        url = f"https://api.github.com/repos/{ASSET_REPO}/git/trees/{ref}?recursive=1"
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "cwc-server", "Accept": "application/vnd.github+json"})
            with urllib.request.urlopen(req, timeout=25) as r:
                data = json.load(r)
        except Exception as e:  # noqa
            print(f"[characters] GitHub listing ({ref}) failed: {e}")
            continue
        have = {"skel": set(), "atlas": set(), "png": set()}
        prefix = ASSET_PATH + "/" if ASSET_PATH else ""
        for t in data.get("tree", []):
            p = t.get("path", "")
            if not p.startswith(prefix):
                continue
            rest = p[len(prefix):]
            if "/" in rest or "." not in rest:
                continue
            stem, _, ext = rest.rpartition(".")
            if ext.lower() in have:
                have[ext.lower()].add(stem)
        if data.get("truncated"):
            print("[characters] WARNING: GitHub says the file list was truncated (huge repo).")
        names = sorted(have["skel"] & have["atlas"] & have["png"], key=natural_key)
        incomplete = sorted(have["skel"] - (have["atlas"] & have["png"]))
        if incomplete:
            print(f"[characters] skipped {len(incomplete)} skel(s) missing an .atlas or .png, e.g. {incomplete[:3]}")
        if names:
            return names
    return []


def refresh_characters():
    """Blocking (runs in a thread). Updates CHARS / CHAR_SET."""
    global CHARS, CHAR_SET
    src = "local ./cookies folder"
    names = scan_local()
    if not names:
        src = f"GitHub ({ASSET_REPO}/{ASSET_PATH})"
        names = fetch_github_names()
    if not names and os.path.isfile(CHAR_TXT):
        src = "characters.txt"
        with open(CHAR_TXT, encoding="utf-8") as f:
            names = [ln.strip() for ln in f if ln.strip() and not ln.strip().startswith("#")]
    cacheable = src.startswith("local") or src.startswith("GitHub")
    if not names and os.path.isfile(CHAR_CACHE_FILE):
        src = "characters_cache.json (last good list)"
        try:
            with open(CHAR_CACHE_FILE, encoding="utf-8") as f:
                names = json.load(f)
        except Exception:
            names = []
    names = sorted({n for n in names if isinstance(n, str) and NAME_RE.match(n)}, key=natural_key)
    if names:
        CHARS, CHAR_SET = names, set(names)
        if cacheable:
            try:
                with open(CHAR_CACHE_FILE, "w", encoding="utf-8") as f:
                    json.dump(names, f)
            except Exception:
                pass
        print(f"[characters] {len(names)} character(s) from {src}")
    else:
        print("[characters] WARNING: no characters found (GitHub unreachable?). Create characters.txt with one name per line as a fallback.")


async def character_refresher():
    loop = asyncio.get_running_loop()
    while True:
        await asyncio.sleep(600)
        try:
            await loop.run_in_executor(None, refresh_characters)
        except Exception as e:  # noqa
            print("[characters] refresh error:", e)


# ---------------------------------------------------------------- http
async def index(request):
    return web.FileResponse(os.path.join(BASE, "index.html"), headers={"Cache-Control": "no-cache"})


async def api_world(request):
    return web.json_response(world_json(), headers={"Cache-Control": "no-cache"})


async def api_characters(request):
    global _last_try
    if not CHARS and time.time() - _last_try > 30:      # list was empty: try again (at most every 30s)
        _last_try = time.time()
        await asyncio.get_running_loop().run_in_executor(None, refresh_characters)
    return web.json_response({"base": ASSET_BASE, "characters": [{"name": n} for n in CHARS]}, headers={"Cache-Control": "no-cache"})


async def asset(request):
    """Optional local copy: ./cookies/NAME.skel|atlas|png (the browser only uses this if GitHub fails)."""
    fname = request.match_info["file"]
    if os.path.splitext(fname)[1].lower() not in ALLOWED_EXT:
        raise web.HTTPNotFound()
    root = os.path.realpath(LOCAL_DIR)
    path = os.path.realpath(os.path.join(LOCAL_DIR, fname))
    if os.path.dirname(path) != root or not os.path.isfile(path):
        raise web.HTTPNotFound()
    return web.FileResponse(path, headers={"Cache-Control": "public, max-age=86400"})


JELLY_URL = "https://cdn.comic.studio/images/cookierun/props/7ed8145de16e727535eb30d3cf2ffa8d.png?filename=jellybean_01.png"
_jelly_png = {"data": None, "tried": 0.0}


def fetch_jelly_png():
    try:
        req = urllib.request.Request(JELLY_URL, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=20) as r:
            data = r.read()
        if data[:8] == b"\x89PNG\r\n\x1a\n":
            _jelly_png["data"] = data
            print(f"[jelly] image cached ({len(data) // 1024} KB)")
        else:
            print("[jelly] download was not a PNG; the game will draw its own jelly")
    except Exception as e:  # noqa
        print(f"[jelly] could not download the jelly image ({e}); the game will draw its own jelly")


async def jelly_png(request):
    """Serves the jelly picture from this server (same origin = no browser cross-site block)."""
    if _jelly_png["data"] is None and time.time() - _jelly_png["tried"] > 60:
        _jelly_png["tried"] = time.time()
        await asyncio.get_running_loop().run_in_executor(None, fetch_jelly_png)
    if _jelly_png["data"] is None:
        raise web.HTTPNotFound()
    return web.Response(body=_jelly_png["data"], content_type="image/png", headers={"Cache-Control": "public, max-age=86400"})


async def vendor(request):
    fname = request.match_info["file"]
    if fname not in VENDOR:
        raise web.HTTPNotFound()
    path = os.path.join(VENDOR_DIR, fname)
    if not os.path.isfile(path):
        raise web.HTTPNotFound()
    return web.FileResponse(path, headers={"Cache-Control": "public, max-age=86400"})


# ================================================================ WORLD (shared with the client via /api/world)
WORLD_SIZE = 120
BOXES = []   # static solid boxes (players, cars and balls all collide with these)
DECOR = []   # visual-only extras
ZONES = []   # open areas the client keeps lollipops out of
HOOPS = []   # basketball rims: {"x","y","z","r"}


def box(cx, cz, w, d, top, thick=99, color=None, **kw):
    b = {"x": cx, "z": cz, "w": w, "d": d, "top": top, "thick": thick, "color": color}
    b.update(kw)
    BOXES.append(b)


def zone(cx, cz, w, d):
    ZONES.append({"x": cx, "z": cz, "w": w, "d": d})


def walls(cx, cz, w, d, H, color, door=3.0, door_h=2.8, t=0.5):
    """Four walls with a doorway in the middle of the +z side."""
    box(cx, cz - d / 2 + t / 2, w, t, H, color=color)                     # back
    box(cx - w / 2 + t / 2, cz, t, d - 2 * t, H, color=color)             # left
    box(cx + w / 2 - t / 2, cz, t, d - 2 * t, H, color=color)             # right
    seg = (w - door) / 2
    fz = cz + d / 2 - t / 2
    box(cx - w / 2 + seg / 2, fz, seg, t, H, color=color)                 # front-left
    box(cx + w / 2 - seg / 2, fz, seg, t, H, color=color)                 # front-right
    box(cx, fz, door, t, H, thick=H - door_h, color=0xc9965d)             # lintel over the door


def house(cx, cz, w, d, wall=0xffc8de, roof=0x8b5a3c):
    """Walk-in house, door on the +z side, furniture inside, stairs + trampoline to the roof."""
    t, H = 0.5, 4.2
    walls(cx, cz, w, d, H, wall)
    box(cx, cz + d / 2 + 1.0, 3.6, 1.6, 0.25, color=0xd9b58a)             # doorstep
    box(cx, cz, w + 1.4, d + 1.4, H + 0.5, thick=0.5, color=roof)          # roof
    box(cx + w / 4, cz - d / 4, 1.2, 1.2, H + 2.1, thick=1.6, color=0xb35c44)  # chimney
    DECOR.append({"t": "floor", "cx": cx, "cz": cz, "w": w - 2 * t, "d": d - 2 * t, "color": 0xc99a5b})
    box(cx - w / 4, cz - d / 2 + t + 0.9, 3.2, 1.4, 1.0, color=0x9ec5ff)                 # couch
    box(cx - w / 4, cz - d / 2 + t + 0.25, 3.2, 0.5, 1.7, color=0x7fa8e8)                 # couch back
    box(cx + w / 8, cz, 2.2, 1.4, 0.9, color=0xe0b07a)                                    # table
    box(cx + w / 2 - t - 1.6, cz - d / 2 + t + 1.9, 3.0, 3.6, 0.7, color=0xffd1e8)        # bed
    box(cx - w / 2 + t + 0.5, cz + d / 6, 0.9, 3.0, 1.9, color=0x8b5a3c)                  # shelf
    ex = cx + w / 2                                                                       # stairs to the roof
    box(ex + 6.9, cz, 2.4, 3.0, 1.2, color=0xffe58a)
    box(ex + 4.5, cz, 2.4, 3.0, 2.4, color=0xffc29a)
    box(ex + 2.1, cz, 2.4, 3.0, 3.6, color=0xff9ec7)
    box(cx - w / 2 - 4.0, cz, 4, 4, 0.5, thick=0.5, color=0x2b2d6e, kind="tramp", bounce=17.0)


def dining_set(x, z, top_color, stool_color, umbrella=None):
    box(x, z, 0.3, 0.3, 0.85, color=0x8b5a3c)                               # table leg
    box(x, z, 1.8, 1.8, 1.0, thick=0.15, color=top_color)                   # table top
    for dx in (-1.45, 1.45):
        box(x + dx, z, 0.6, 0.6, 0.6, color=stool_color)                    # stools
    if umbrella:
        DECOR.append({"t": "umbrella", "x": x, "z": z, "y": 1.0, "r": 1.9, "color": umbrella})


def restaurant(cx, cz):
    """COOKIE CAFE: counter, kitchen, tables inside, patio with umbrellas outside (door faces +z)."""
    w, d, H = 22.0, 14.0, 4.8
    walls(cx, cz, w, d, H, 0xfff0d6, door=4.0, door_h=3.6)
    box(cx, cz, w + 1.2, d + 1.2, H + 0.5, thick=0.5, color=0xe0507a, nojelly=True)      # roof
    DECOR.append({"t": "floor", "cx": cx, "cz": cz, "w": w - 1, "d": d - 1, "color": 0xffffff, "color2": 0xff9ec7, "checker": 1.0})
    DECOR.append({"t": "text", "x": cx, "y": H + 1.5, "z": cz + d / 2 - 0.1, "w": 11, "h": 2.0,
                  "lines": ["COOKIE CAFE"], "bg": "#e0507a", "fg": "#ffffff"})
    box(cx, cz + d / 2 + 1.3, 9.0, 2.6, 4.3, thick=0.25, color=0xff6f91, stripes=0xffffff)  # awning
    # counter (gap at the right end so you can walk into the kitchen)
    box(cx - 1.5, cz - d / 2 + 4.2, 18.0, 1.0, 1.15, color=0xd98c5f)
    box(cx - 7.0, cz - d / 2 + 4.2, 0.9, 0.6, 1.6, thick=0.45, color=0x444455)             # cash register
    # kitchen along the back wall
    box(cx - 7.5, cz - d / 2 + 1.1, 2.4, 1.2, 1.0, color=0x3d3d48)                         # stove
    box(cx - 4.5, cz - d / 2 + 1.1, 2.4, 1.2, 1.0, color=0x3d3d48)                         # stove
    box(cx - 1.5, cz - d / 2 + 1.1, 2.0, 1.2, 1.0, color=0xc8d6e0)                         # sink
    box(cx + 2.0, cz - d / 2 + 1.1, 1.6, 1.2, 2.7, color=0xe8f4ff)                         # fridge
    box(cx + 5.5, cz - d / 2 + 1.1, 3.0, 1.2, 1.0, color=0xd9b58a)                         # prep table
    DECOR.append({"t": "text", "x": cx - 3, "y": 3.5, "z": cz - d / 2 + 0.3, "w": 7, "h": 2.4,
                  "lines": ["~ MENU ~", "Jelly Pie ........ 5", "Hot Cocoa ........ 3", "Rainbow Cake ..... 8"],
                  "bg": "#3a2a24", "fg": "#ffe9b0"})
    # dining room
    for x, z in [(cx - 7, cz + 1.2), (cx - 7, cz + 4.6), (cx + 7, cz + 1.2), (cx + 7, cz + 4.6), (cx + 1.5, cz + 1.2)]:
        dining_set(x, z, 0xffffff, 0xff9ec7)
    # patio outside
    for x in (cx - 7.5, cx + 7.5):
        dining_set(x, cz + d / 2 + 5.0, 0xfff1e0, 0x9ef0d0, umbrella=0xff6f91 if x < cx else 0x5fb8ff)
    DECOR.append({"t": "floor", "cx": cx, "cz": cz + d / 2 + 4.5, "w": w, "d": 7, "color": 0xe8d2b0})
    zone(cx, cz + d / 2 + 4.5, w + 2, 9)


def tree(x, z, h=2.6, r=2.2, color=0x7ccf6a):
    box(x, z, 0.6, 0.6, h, hidden=True)                                       # trunk collider
    DECOR.append({"t": "tree", "x": x, "z": z, "h": h, "r": r, "color": color})


def bench(x, z, along_x=True, face=1):
    """face = which way the backrest is (1 = +, -1 = -) on the axis across the bench."""
    if along_x:
        box(x, z, 2.6, 0.8, 0.6, color=0xb07848)
        box(x, z - face * 0.45, 2.6, 0.15, 1.35, thick=0.75, color=0x8b5a3c)
    else:
        box(x, z, 0.8, 2.6, 0.6, color=0xb07848)
        box(x - face * 0.45, z, 0.15, 2.6, 1.35, thick=0.75, color=0x8b5a3c)


def park(cx, cz):
    """SUGAR PARK: fenced, paths, fountain, pond, trees, benches, flower beds, lamps."""
    w, d = 34.0, 26.0
    DECOR.append({"t": "park", "cx": cx, "cz": cz, "w": w, "d": d})
    zone(cx, cz, w + 2, d + 2)
    gap, fh, ft = 4.0, 0.9, 0.25
    for side in (-1, 1):                                       # fence along x (north/south sides)
        z = cz + side * d / 2
        seg = (w - gap) / 2
        box(cx - w / 2 + seg / 2, z, seg, ft, fh, color=0xffffff)
        box(cx + w / 2 - seg / 2, z, seg, ft, fh, color=0xffffff)
    for side in (-1, 1):                                       # fence along z (east/west sides)
        x = cx + side * w / 2
        seg = (d - gap) / 2
        box(x, cz - d / 2 + seg / 2, ft, seg, fh, color=0xffffff)
        box(x, cz + d / 2 - seg / 2, ft, seg, fh, color=0xffffff)
    # fountain in the middle
    for bw, bd in ((4.4, 1.84), (1.84, 4.4), (3.4, 3.4)):                # octagon-shaped basin (drawn by the client)
        box(cx, cz, bw, bd, 0.7, hidden=True)
    box(cx, cz, 1.0, 1.0, 2.5, hidden=True)
    DECOR.append({"t": "fountain", "x": cx, "z": cz})
    # pond (walkable water) in the north-west corner
    DECOR.append({"t": "pond", "x": cx - 10, "z": cz - 6, "r": 3.6})
    # trees (some cherry blossom)
    for i, (dx, dz) in enumerate([(-13, 9), (-6, 10), (6, 10), (13, 9), (13, -9), (6, -10), (-2, -10), (14, 2), (-15, 2)]):
        tree(cx + dx, cz + dz, color=0xffb7d5 if i % 3 == 1 else 0x7ccf6a)
    # benches facing the fountain
    bench(cx, cz + 5.0, True, 1)
    bench(cx, cz - 5.0, True, -1)
    bench(cx + 5.5, cz, False, 1)
    bench(cx - 5.5, cz, False, -1)
    # flower beds (low enough to step onto)
    for dx, dz in [(9, 5), (-9, 5), (9, -5)]:
        box(cx + dx, cz + dz, 4.0, 1.4, 0.3, color=0x8b5a3c)
        DECOR.append({"t": "flowers", "x": cx + dx, "z": cz + dz, "w": 4.0, "d": 1.4, "y": 0.3})
    # lamps around the plaza
    for dx, dz in [(4, 4), (-4, 4), (4, -4), (-4, -4)]:
        box(cx + dx, cz + dz, 0.25, 0.25, 3.2, hidden=True)
        DECOR.append({"t": "lamp", "x": cx + dx, "z": cz + dz, "h": 3.2})
    # entrance sign on the east side (faces the middle of the map)
    sx = cx + w / 2 + 0.6
    box(sx, cz - 2.8, 0.3, 0.3, 3.6, color=0x8b5a3c)
    box(sx, cz + 2.8, 0.3, 0.3, 3.6, color=0x8b5a3c)
    DECOR.append({"t": "text", "x": sx, "y": 3.4, "z": cz, "w": 5.4, "h": 1.2, "ry": math.pi / 2,
                  "lines": ["SUGAR PARK"], "bg": "#5fb06a", "fg": "#ffffff"})


def hoop(side, cx, cz):
    """Basketball hoop on the +x (side=1) or -x (side=-1) end of a court."""
    rim_x = cx + side * 12.4
    HOOPS.append({"x": rim_x, "y": 4.2, "z": cz, "r": 0.6})
    box(cx + side * 13.15, cz, 0.15, 2.4, 5.4, thick=1.5, color=0xffffff)              # backboard
    box(cx + side * 14.3, cz, 0.4, 0.4, 5.6, color=0x555566)                            # pole
    box(cx + side * 13.75, cz, 1.1, 0.25, 5.25, thick=0.25, color=0x555566)             # arm
    DECOR.append({"t": "hoop", "x": rim_x, "y": 4.2, "z": cz, "r": 0.6})


def court(cx, cz):
    """Basketball court with two hoops and bleachers."""
    w, d = 28.0, 15.0
    DECOR.append({"t": "court", "cx": cx, "cz": cz, "w": w, "d": d})
    zone(cx, cz, w + 4, d + 2)
    hoop(1, cx, cz)
    hoop(-1, cx, cz)
    for i in range(3):                                                                  # bleachers
        box(cx, cz - d / 2 - 1.2 - i * 1.2, 20.0, 1.2, 0.6 * (i + 1), color=[0x8fd3ff, 0xc7a6ff, 0xff9ec7][i])
    DECOR.append({"t": "text", "x": cx, "y": 4.2, "z": cz - d / 2 - 4.8, "w": 9, "h": 1.6,
                  "lines": ["COOKIE COURT"], "bg": "#f08a2e", "fg": "#ffffff"})
    box(cx - 4.2, cz - d / 2 - 4.8, 0.3, 0.3, 3.6, color=0x555566)
    box(cx + 4.2, cz - d / 2 - 4.8, 0.3, 0.3, 3.6, color=0x555566)


PITCH = {"cx": 60.0, "cz": -20.0, "w": 50.0, "d": 30.0}
GOAL_W_X = PITCH["cx"] - PITCH["w"] / 2    # 35 (west goal line)
GOAL_E_X = PITCH["cx"] + PITCH["w"] / 2    # 85 (east goal line)
RING = {"cx": 0.0, "cz": 72.0, "r0": 28.0, "r1": 38.0}
COURT = {"cx": 0.0, "cz": -55.0}


def goal(line_x, direction, cz):
    """Soccer goal. direction=-1 opens toward +x (west goal), +1 opens toward -x (east goal)."""
    ox = line_x
    back = ox + direction * 3.0
    box(ox, cz - 4.0, 0.4, 0.4, 3.0, color=0xffffff)
    box(ox, cz + 4.0, 0.4, 0.4, 3.0, color=0xffffff)
    box(ox, cz, 0.4, 8.4, 3.0, thick=0.4, color=0xffffff)
    box(back, cz, 0.3, 8.4, 3.0, color=0xeeeeee, kind="net")
    box((ox + back) / 2, cz - 4.0, 3.0, 0.3, 3.0, color=0xeeeeee, kind="net")
    box((ox + back) / 2, cz + 4.0, 3.0, 0.3, 3.0, color=0xeeeeee, kind="net")
    box((ox + back) / 2, cz, 3.0, 8.4, 3.0, thick=0.2, color=0xeeeeee, kind="net")


def build_world():
    # --- original map (staircase / tower / bridge / pyramid / parkour / islands)
    for x, z, top in [(-6, 2, 1.1), (-10, -3, 2.2), (-10, -9, 3.3), (-4, -12, 4.4), (2, -12, 5.5)]:
        box(x, z, 4, 4, top)
    box(9, -12, 8, 8, 6.6, color=0xffd1e8)
    box(9, -3, 2.4, 10, 6.6, thick=0.6, color=0xc7a6ff)
    for z, top in [(6, 5.5), (11, 4.4), (16, 3.3), (21, 2.2)]:
        box(9, z, 4, 4, top)
    box(9, -12, 4.4, 4.4, 7.6, thick=1.0, hidden=True)            # cake collider
    DECOR.append({"t": "cake", "x": 9, "z": -12, "y": 6.6})
    box(-22, 14, 12, 12, 1.2, color=0xffe58a)
    box(-22, 14, 8, 8, 2.4, color=0xffc29a)
    box(-22, 14, 4, 4, 3.6, color=0xff9ec7)
    for z, top, col in [(-6, 1.4, 0x9ef0d0), (-10, 2.6, 0x8fd3ff), (-14, 3.8, 0xc7a6ff), (-18, 2.6, 0xff9ec7)]:
        box(-24, z, 2, 2, top, color=col)
    box(-24, -22, 6, 6, 3.8, color=0xffe58a)
    for x, top in [(16, 1.1), (21, 2.2), (26, 3.3)]:
        box(x, 8, 4, 4, top)
    box(26, 2, 4, 4, 3.3, thick=0.8, color=0x9ef0d0)
    box(26, -4, 3, 3, 3.3, thick=0.8, color=0xffe58a)
    box(31, -4, 3, 3, 4.4, thick=0.8, color=0xff9ec7)
    box(36, -4, 5, 5, 5.5, thick=0.8, color=0xc7a6ff)
    box(0, 26, 20, 2, 1.4, color=0x8fd3ff)
    box(-38, -38, 3, 3, 12, color=0xff9ec7)
    box(38, 38, 3, 3, 9, color=0x9ef0d0)

    # --- houses
    house(-60, 28, 16, 12)
    house(66, 44, 10, 8, wall=0xbfe9ff, roof=0xb2455e)

    # --- soccer pitch with two goals + scoreboard
    DECOR.append({"t": "pitch", **PITCH})
    zone(PITCH["cx"], PITCH["cz"], PITCH["w"] + 16, PITCH["d"] + 16)
    goal(GOAL_W_X, -1, PITCH["cz"])
    goal(GOAL_E_X, +1, PITCH["cz"])
    box(PITCH["cx"] - 7, -38, 0.6, 0.6, 4.0, color=0x8b5a3c)
    box(PITCH["cx"] + 7, -38, 0.6, 0.6, 4.0, color=0x8b5a3c)
    DECOR.append({"t": "sign", "x": PITCH["cx"], "y": 5.2, "z": -38})

    # --- trampolines
    box(22, 14, 4, 4, 0.5, thick=0.5, color=0x2b2d6e, kind="tramp", bounce=15.0)
    box(-30, -32, 4, 4, 0.5, thick=0.5, color=0x2b2d6e, kind="tramp", bounce=16.0)
    box(40, 30, 5, 5, 0.5, thick=0.5, color=0x2b2d6e, kind="tramp", bounce=22.0)   # sky trampoline
    box(47, 30, 6, 6, 8.5, thick=1.0, color=0x9ef0d0)                              # sky island

    # --- big stepped ziggurat
    for i, (size, col) in enumerate(zip(range(28, 4, -4), [0xffe58a, 0xffc29a, 0xff9ec7, 0xc7a6ff, 0x8fd3ff, 0x9ef0d0])):
        box(-85, -60, size, size, 1.2 * (i + 1), color=col)

    # --- corner landmarks
    for sx in (-1, 1):
        for sz in (-1, 1):
            box(sx * 105, sz * 105, 4, 4, 14, color=[0xff9ec7, 0x9ef0d0, 0xffe58a, 0xc7a6ff][(sx + 1 + (sz + 1) // 2)])

    # --- race track
    DECOR.append({"t": "road", **RING})

    # --- NEW: cafe, park, basketball court
    restaurant(36, -66)
    park(-60, -14)
    court(COURT["cx"], COURT["cz"])


build_world()

BALL_KINDS = {
    "soccer":  {"r": 0.55, "m": 4.0,  "e": 0.72, "drag": 0.55},
    "beach":   {"r": 1.0,  "m": 1.2,  "e": 0.82, "drag": 0.35},
    "bowling": {"r": 0.5,  "m": 30.0, "e": 0.25, "drag": 0.28},
    "bouncy":  {"r": 0.45, "m": 3.0,  "e": 0.94, "drag": 0.5},
    "basket":  {"r": 0.32, "m": 0.6,  "e": 0.78, "drag": 0.45},
}
GRABBABLE = {"basket"}
SPAWNABLE = {"soccer", "beach", "basket"}
MAX_SPAWNED = 60
BALL_DEFS = [
    {"id": "soccer", "kind": "soccer", "x": PITCH["cx"], "z": PITCH["cz"]},
    {"id": "beach1", "kind": "beach", "x": 3.0, "z": 14.0},
    {"id": "beach2", "kind": "beach", "x": -6.0, "z": 14.0},
    {"id": "bowl", "kind": "bowling", "x": 0.0, "z": 19.0},
    {"id": "bounce1", "kind": "bouncy", "x": 4.0, "z": 22.0},
    {"id": "bounce2", "kind": "bouncy", "x": -4.0, "z": 22.0},
    {"id": "bball1", "kind": "basket", "x": COURT["cx"], "z": COURT["cz"]},
    {"id": "bball2", "kind": "basket", "x": COURT["cx"] - 4.0, "z": COURT["cz"] + 3.0},
    {"id": "bball3", "kind": "basket", "x": COURT["cx"] + 4.0, "z": COURT["cz"] - 3.0},
]
for _d in BALL_DEFS:
    _d["r"] = BALL_KINDS[_d["kind"]]["r"]
CAR_DEFS = [
    {"id": "car1", "color": 0xff7ab0, "x": 14.0, "z": 14.0, "h": -0.8},
    {"id": "car2", "color": 0x5fe0b5, "x": -14.0, "z": 14.0, "h": 0.8},
    {"id": "car3", "color": 0xffd84a, "x": RING["cx"] + 31.0, "z": RING["cz"], "h": 0.0},
    {"id": "car4", "color": 0x5fb8ff, "x": RING["cx"] + 35.0, "z": RING["cz"], "h": 0.0},
    {"id": "car5", "color": 0xc7a6ff, "x": 22.0, "z": -46.0, "h": math.pi},
]
CAR_RESPAWN = 15.0


def world_json():
    return {
        "size": WORLD_SIZE, "boxes": BOXES, "decor": DECOR, "zones": ZONES, "hoops": HOOPS,
        "balls": [{k: d[k] for k in ("id", "kind", "r", "x", "z")} for d in BALL_DEFS],
        "cars": CAR_DEFS,
    }


# ================================================================ JELLIES (collectibles)
JELLY_COUNT = 50
AABBS = [(b["x"] - b["w"] / 2, b["x"] + b["w"] / 2, b["top"] - min(b["thick"], b["top"]), b["top"],
          b["z"] - b["d"] / 2, b["z"] + b["d"] / 2) for b in BOXES]
TOPPABLE = [b for b in BOXES if b["w"] >= 1.6 and b["d"] >= 1.6 and b["top"] <= 8.6
            and not b.get("kind") and not b.get("hidden") and not b.get("nojelly")]


def free_at(x, y, z, r=0.5, hgt=1.6):
    """True when nothing solid fills the column [y, y+hgt] around (x, z)."""
    for (x0, x1, y0, y1, z0, z1) in AABBS:
        if x0 - r < x < x1 + r and z0 - r < z < z1 + r and y0 < y + hgt and y1 > y + 0.01:
            return False
    return True


def jelly_spot():
    lim = WORLD_SIZE - 6
    for _ in range(80):
        if random.random() < 0.22 and TOPPABLE:
            b = random.choice(TOPPABLE)
            x = b["x"] + random.uniform(-b["w"] / 2 + 0.6, b["w"] / 2 - 0.6)
            z = b["z"] + random.uniform(-b["d"] / 2 + 0.6, b["d"] / 2 - 0.6)
            y = b["top"]
        else:
            x, z, y = random.uniform(-lim, lim), random.uniform(-lim, lim), 0.0
        if free_at(x, y, z):
            return [round(x, 2), round(y, 2), round(z, 2)]
    return [round(random.uniform(-15, 15), 2), 0.0, round(random.uniform(-15, 15), 2)]


JELLIES = [jelly_spot() for _ in range(JELLY_COUNT)]


# ================================================================ PHYSICS (balls) - server authoritative
G = 22.0
BALLS = []
CARS = []
SCORE = [0, 0]   # [pink, mint]
_spawn_n = 0
RIM_PTS = []     # (x, y, z, hoop index) points around every rim, balls bounce off them
for _hi, _h in enumerate(HOOPS):
    for _k in range(20):
        _a = _k / 20 * math.tau
        RIM_PTS.append((_h["x"] + math.cos(_a) * _h["r"], _h["y"], _h["z"] + math.sin(_a) * _h["r"], _hi))


def make_ball(bid, kind, x, y, z, spawned=False):
    b = {"id": bid, "kind": kind, **BALL_KINDS[kind], "hx": x, "hy": y, "hz": z, "spawned": spawned}
    reset_ball(b)
    return b


def reset_ball(b):
    b.update(x=b["hx"], z=b["hz"], y=max(b["hy"], b["r"]), vx=0.0, vy=0.0, vz=0.0, asleep=True, idle=0.0,
             respawn_at=0.0, scored=False, dirty=True, holder=None, last_basket=0.0)


def ball_info(b):
    return [b["id"], round(b["x"], 2), round(b["y"], 2), round(b["z"], 2), b["kind"], b["r"], b.get("holder")]


def ball_by_id(bid):
    return next((b for b in BALLS if b["id"] == bid), None)


def car_home(c):
    d = next(d for d in CAR_DEFS if d["id"] == c["id"])
    c.update(x=d["x"], y=0.0, z=d["z"], h=d["h"], v=0.0, driver=None, t=0.0, dead=False, respawn_at=0.0)


def init_state():
    BALLS.clear()
    CARS.clear()
    for d in BALL_DEFS:
        BALLS.append(make_ball(d["id"], d["kind"], d["x"], d["r"], d["z"]))
    for d in CAR_DEFS:
        c = {"id": d["id"]}
        car_home(c)
        CARS.append(c)


init_state()


def car_public(c, **extra):
    out = {"id": c["id"], "x": round(c["x"], 2), "y": round(c["y"], 2), "z": round(c["z"], 2), "h": round(c["h"], 3),
           "v": round(c["v"], 2), "driver": c["driver"], "dead": c["dead"]}
    out.update(extra)
    return out


def closest_on_seg(px, py, pz, ax, ay, az, bx, by, bz):
    abx, aby, abz = bx - ax, by - ay, bz - az
    l2 = abx * abx + aby * aby + abz * abz
    t = 0.0 if l2 < 1e-9 else max(0.0, min(1.0, ((px - ax) * abx + (py - ay) * aby + (pz - az) * abz) / l2))
    return ax + abx * t, ay + aby * t, az + abz * t


def pushers(now):
    """Things that can shove a ball: players (capsules) and cars (capsules along their length)."""
    out = []
    for p in players.values():
        if p.get("car"):
            continue
        stale = now - p["pt"] > 0.3
        vx = 0.0 if stale else p["vx"]
        vz = 0.0 if stale else p["vz"]
        out.append((p["x"], p["y"] + 0.4, p["z"], p["x"], p["y"] + 1.4, p["z"], 0.4, vx, 0.0, vz, 70.0))
    for c in CARS:
        if c["dead"]:
            continue
        fx, fz = math.sin(c["h"]), math.cos(c["h"])
        v = c["v"] if (c["driver"] and now - c["t"] < 0.4) else 0.0
        out.append((c["x"] + fx, c["y"] + 0.6, c["z"] + fz, c["x"] - fx, c["y"] + 0.6, c["z"] - fz, 1.0, fx * v, 0.0, fz * v, 900.0))
    return out


def touching(b, push):
    for (ax, ay, az, bx, by, bz, rad, *_rest) in push:
        qx, qy, qz = closest_on_seg(b["x"], b["y"], b["z"], ax, ay, az, bx, by, bz)
        lim = b["r"] + rad + 0.03
        if (b["x"] - qx) ** 2 + (b["y"] - qy) ** 2 + (b["z"] - qz) ** 2 < lim * lim:
            return True
    return False


def bounce_off_point(b, px, py, pz, pr, e):
    r = b["r"]
    dx, dy, dz = b["x"] - px, b["y"] - py, b["z"] - pz
    d2 = dx * dx + dy * dy + dz * dz
    lim = r + pr
    if d2 >= lim * lim or d2 < 1e-12:
        return
    d = math.sqrt(d2)
    nx, ny, nz = dx / d, dy / d, dz / d
    pen = lim - d
    b["x"] += nx * pen; b["y"] += ny * pen; b["z"] += nz * pen
    vn = b["vx"] * nx + b["vy"] * ny + b["vz"] * nz
    if vn < 0:
        b["vx"] -= (1 + e) * vn * nx; b["vy"] -= (1 + e) * vn * ny; b["vz"] -= (1 + e) * vn * nz


def step_ball(b, h, push):
    r = b["r"]
    b["vy"] -= G * h
    sp = math.sqrt(b["vx"] ** 2 + b["vy"] ** 2 + b["vz"] ** 2)
    if sp > 30:
        k = 30 / sp
        b["vx"] *= k; b["vy"] *= k; b["vz"] *= k
    b["x"] += b["vx"] * h
    b["y"] += b["vy"] * h
    b["z"] += b["vz"] * h
    supported = False
    if b["y"] < r:
        b["y"] = r
        if b["vy"] < 0:
            b["vy"] = -b["vy"] * b["e"]
            if abs(b["vy"]) < 1.5:
                b["vy"] = 0.0
        supported = True
    # static boxes
    for (x0, x1, y0, y1, z0, z1) in AABBS:
        if b["x"] < x0 - r or b["x"] > x1 + r or b["y"] < y0 - r or b["y"] > y1 + r or b["z"] < z0 - r or b["z"] > z1 + r:
            continue
        cx = min(max(b["x"], x0), x1); cy = min(max(b["y"], y0), y1); cz = min(max(b["z"], z0), z1)
        dx, dy, dz = b["x"] - cx, b["y"] - cy, b["z"] - cz
        d2 = dx * dx + dy * dy + dz * dz
        if d2 >= r * r:
            continue
        if d2 > 1e-12:
            d = math.sqrt(d2)
            nx, ny, nz = dx / d, dy / d, dz / d
            pen = r - d
        else:   # centre is inside the box: leave through the nearest face
            opts = [(b["x"] - x0, -1, 0, 0), (x1 - b["x"], 1, 0, 0), (b["y"] - y0, 0, -1, 0),
                    (y1 - b["y"], 0, 1, 0), (b["z"] - z0, 0, 0, -1), (z1 - b["z"], 0, 0, 1)]
            m = min(opts)
            pen = m[0] + r
            nx, ny, nz = m[1], m[2], m[3]
        b["x"] += nx * pen; b["y"] += ny * pen; b["z"] += nz * pen
        vn = b["vx"] * nx + b["vy"] * ny + b["vz"] * nz
        if vn < 0:
            e = 0.0 if -vn < 1.5 else b["e"]
            b["vx"] -= (1 + e) * vn * nx; b["vy"] -= (1 + e) * vn * ny; b["vz"] -= (1 + e) * vn * nz
        if ny > 0.6:
            supported = True
    # basketball rims
    for hp in HOOPS:
        if abs(b["y"] - hp["y"]) < r + 0.1 and math.hypot(b["x"] - hp["x"], b["z"] - hp["z"]) < hp["r"] + r + 0.1:
            for (px, py, pz, _hi) in RIM_PTS:
                bounce_off_point(b, px, py, pz, 0.05, 0.55)
            break
    # players / cars
    for (ax, ay, az, bx, by, bz, rad, pvx, pvy, pvz, pm) in push:
        qx, qy, qz = closest_on_seg(b["x"], b["y"], b["z"], ax, ay, az, bx, by, bz)
        dx, dy, dz = b["x"] - qx, b["y"] - qy, b["z"] - qz
        lim = r + rad
        d2 = dx * dx + dy * dy + dz * dz
        if d2 >= lim * lim:
            continue
        d = math.sqrt(d2)
        if d < 1e-6:
            nx, ny, nz, d = 0.0, 1.0, 0.0, 0.0
        else:
            nx, ny, nz = dx / d, dy / d, dz / d
        pen = lim - d
        b["x"] += nx * pen; b["y"] += ny * pen; b["z"] += nz * pen
        vn = (b["vx"] - pvx) * nx + (b["vy"] - pvy) * ny + (b["vz"] - pvz) * nz
        if vn < 0:
            k = (1 + 0.6) * pm / (pm + b["m"])
            b["vx"] -= vn * nx * k; b["vy"] -= vn * ny * k; b["vz"] -= vn * nz * k
            if pvx * pvx + pvz * pvz > 9 and -vn > 2.0 and ny < 0.5:
                b["vy"] += min(3.0, -vn * 0.12)     # a little loft on a proper kick
    # drag + world edge
    f = math.exp(-(b["drag"] if supported else 0.04) * h)
    b["vx"] *= f; b["vz"] *= f
    if supported and b["vx"] ** 2 + b["vz"] ** 2 < 0.09:
        b["vx"] *= 0.9; b["vz"] *= 0.9
    lim = WORLD_SIZE - 1.0
    if abs(b["x"]) > lim:
        b["x"] = math.copysign(lim, b["x"]); b["vx"] *= -0.5
    if abs(b["z"]) > lim:
        b["z"] = math.copysign(lim, b["z"]); b["vz"] *= -0.5
    b["sup"] = supported


def pair_collide():
    free = [b for b in BALLS if not b["holder"]]
    n = len(free)
    for i in range(n):
        a = free[i]
        for j in range(i + 1, n):
            c = free[j]
            dx, dy, dz = c["x"] - a["x"], c["y"] - a["y"], c["z"] - a["z"]
            lim = a["r"] + c["r"]
            d2 = dx * dx + dy * dy + dz * dz
            if d2 >= lim * lim or d2 < 1e-12:
                continue
            if a["asleep"] and c["asleep"]:
                continue
            d = math.sqrt(d2)
            nx, ny, nz = dx / d, dy / d, dz / d
            ia, ic = 1.0 / a["m"], 1.0 / c["m"]
            pen = lim - d
            a["x"] -= nx * pen * ia / (ia + ic); a["y"] -= ny * pen * ia / (ia + ic); a["z"] -= nz * pen * ia / (ia + ic)
            c["x"] += nx * pen * ic / (ia + ic); c["y"] += ny * pen * ic / (ia + ic); c["z"] += nz * pen * ic / (ia + ic)
            vn = (c["vx"] - a["vx"]) * nx + (c["vy"] - a["vy"]) * ny + (c["vz"] - a["vz"]) * nz
            if vn < 0:
                j_ = -(1 + 0.8) * vn / (ia + ic)
                a["vx"] -= j_ * ia * nx; a["vy"] -= j_ * ia * ny; a["vz"] -= j_ * ia * nz
                c["vx"] += j_ * ic * nx; c["vy"] += j_ * ic * ny; c["vz"] += j_ * ic * nz
            a["asleep"] = False; c["asleep"] = False


def goal_check(b):
    """Returns 'mint' if the ball is inside the west goal, 'pink' if inside the east goal."""
    cz = PITCH["cz"]
    if abs(b["z"] - cz) < 3.8 and b["y"] < 2.9:
        if GOAL_W_X - 3.0 < b["x"] < GOAL_W_X - 0.3:
            return "mint"
        if GOAL_E_X + 0.3 < b["x"] < GOAL_E_X + 3.0:
            return "pink"
    return None


def basket_check(b, px, py, pz, now):
    """Did the ball's centre drop down through a rim during this tick?"""
    if now - b["last_basket"] < 1.0:
        return False
    for hp in HOOPS:
        if py > hp["y"] >= b["y"]:
            t = (py - hp["y"]) / max(py - b["y"], 1e-6)
            cx = px + (b["x"] - px) * t
            cz = pz + (b["z"] - pz) * t
            if math.hypot(cx - hp["x"], cz - hp["z"]) < hp["r"] - 0.1:
                b["last_basket"] = now
                return True
    return False


def phys_tick(dt, now):
    moved, events = [], []
    push = pushers(now)
    for b in BALLS:
        if b["holder"]:
            hp = players.get(b["holder"])
            if hp:
                b["x"], b["y"], b["z"] = hp["x"], hp["y"] + 2.0, hp["z"]
            else:
                b["holder"] = None
                b["asleep"] = False
            continue
        if b["respawn_at"] and now >= b["respawn_at"]:
            reset_ball(b)
        if b["asleep"] and touching(b, push):
            b["asleep"] = False
        b["px"], b["py"], b["pz"] = b["x"], b["y"], b["z"]
    sub = 3
    h = dt / sub
    for _ in range(sub):
        for b in BALLS:
            if not b["asleep"] and not b["holder"]:
                step_ball(b, h, push)
        pair_collide()
    for b in BALLS:
        if b["holder"]:
            continue
        if b["asleep"]:
            b["idle"] += dt
            if b["idle"] > 60 and math.hypot(b["x"] - b["hx"], b["z"] - b["hz"]) > 2.0:
                reset_ball(b)       # nobody touched it for a minute: send it home
            if b["dirty"]:
                moved.append(b); b["dirty"] = False
            continue
        b["idle"] = 0.0
        if b["kind"] == "soccer" and not b["scored"]:
            team = goal_check(b)
            if team:
                b["scored"] = True
                b["respawn_at"] = now + 3.0
                SCORE[0 if team == "pink" else 1] += 1
                events.append({"t": "goal", "team": team, "score": list(SCORE)})
        if b["kind"] == "basket" and "py" in b and basket_check(b, b["px"], b["py"], b["pz"], now):
            shooter = players.get(b.get("thrower_id") or "")
            n = 0
            if shooter:
                shooter["baskets"] = shooter.get("baskets", 0) + 1
                n = shooter["baskets"]
            events.append({"t": "basket", "by": b.get("thrower") or "Someone", "n": n,
                           "x": round(b["x"], 2), "y": round(b["y"], 2), "z": round(b["z"], 2)})
        sp2 = b["vx"] ** 2 + b["vy"] ** 2 + b["vz"] ** 2
        if b.get("sup") and sp2 < 0.04 and b["y"] <= b["r"] + 0.6:
            b["vx"] = b["vy"] = b["vz"] = 0.0
            b["asleep"] = True
        moved.append(b)
        b["dirty"] = False
    for c in CARS:                                    # wrecked cars come back after CAR_RESPAWN seconds
        if c["dead"] and now >= c["respawn_at"]:
            car_home(c)
            events.append({"t": "car", **car_public(c, snap=True)})
    return [[b["id"], round(b["x"], 2), round(b["y"], 2), round(b["z"], 2)] for b in moved], events


async def physics_loop():
    last = time.time()
    while True:
        await asyncio.sleep(1 / 30)
        now = time.time()
        dt = min(now - last, 0.1)
        last = now
        if not players:
            continue
        try:
            moved, events = phys_tick(dt, now)
            if moved:
                await broadcast({"t": "phys", "b": moved})
            for ev in events:
                if ev["t"] == "goal":
                    print(f"[goal] {ev['team']} scores {ev['score']}")
                elif ev["t"] == "basket":
                    print(f"[basket] {ev['by']} scored")
                await broadcast(ev)
        except Exception as e:  # noqa
            print("[physics] error:", e)


def drop_ball(p, fx=0.0, fz=1.0, aim=0.5, thrown=False):
    """Releases the ball a player holds. thrown=True launches it (aim-assisted toward a hoop in front)."""
    b = ball_by_id(p.get("hold"))
    p["hold"] = None
    if not b or b["holder"] != p["id"]:
        return None
    b["holder"] = None
    b["asleep"] = False
    b["idle"] = 0.0
    l = math.hypot(fx, fz)
    if l < 1e-6:
        fx, fz, l = 0.0, 1.0, 1.0
    fx, fz = fx / l, fz / l
    b["x"] = p["x"] + fx * 0.7
    b["z"] = p["z"] + fz * 0.7
    b["y"] = p["y"] + 2.2
    b["vx"] = b["vy"] = b["vz"] = 0.0
    if thrown:
        target, best = None, 1e9
        for hp in HOOPS:
            dx, dz = hp["x"] - b["x"], hp["z"] - b["z"]
            dh = math.hypot(dx, dz)
            if 0.5 < dh < 17 and (dx * fx + dz * fz) / dh > math.cos(math.radians(40)) and dh < best:
                target, best = hp, dh
        if target:
            dh = best
            sigma = 0.06 + 0.022 * dh                       # far shots miss more often
            tx = target["x"] + random.gauss(0, sigma)
            tz = target["z"] + random.gauss(0, sigma)
            ty = target["y"] + 0.05
            dx, dy, dz = tx - b["x"], ty - b["y"], tz - b["z"]
            T = max(0.55, min(1.5, 0.5 + 0.06 * dh))
            b["vx"] = dx / T * 1.02
            b["vz"] = dz / T * 1.02
            b["vy"] = (dy + 0.5 * G * T * T) / T
        else:
            elev = max(0.15, min(1.25, aim))
            speed = 12.0
            b["vx"] = fx * math.cos(elev) * speed
            b["vz"] = fz * math.cos(elev) * speed
            b["vy"] = math.sin(elev) * speed
        b["thrower"] = p["name"]
        b["thrower_id"] = p["id"]
    return b


def explode_car(c, now):
    """Wreck a car: balls nearby get blasted, it comes back home after CAR_RESPAWN seconds."""
    c["dead"] = True
    c["respawn_at"] = now + CAR_RESPAWN
    c["v"] = 0.0
    drv = players.get(c["driver"] or "")
    if drv:
        drv["car"] = None
    c["driver"] = None
    for b in BALLS:
        if b["holder"]:
            continue
        dx, dy, dz = b["x"] - c["x"], b["y"] - c["y"], b["z"] - c["z"]
        d = math.sqrt(dx * dx + dy * dy + dz * dz)
        if d < 9.0:
            k = 16.0 * (1 - d / 9.0)
            if d > 1e-3:
                b["vx"] += dx / d * k; b["vz"] += dz / d * k
            b["vy"] += 4.0 + k * 0.6
            b["asleep"] = False


async def free_car(p):
    """Release the car a player is driving (exit, disconnect, kick)."""
    cid = p.get("car")
    if not cid:
        return
    p["car"] = None
    for c in CARS:
        if c["id"] == cid and c["driver"] == p["id"]:
            c["driver"] = None
            c["v"] = 0.0
            await broadcast({"t": "car", **car_public(c)})


async def release_hold(p):
    b = drop_ball(p)
    if b:
        await broadcast({"t": "hold", "id": b["id"], "by": None})


# ---------------------------------------------------------------- websocket
players = {}  # id -> dict
next_id = 1
_seq = 0


def pid_seq():
    global _seq
    _seq += 1
    return _seq
CTRL = re.compile(r"[\x00-\x1f\x7f]")


def clean_text(s, limit):
    if not isinstance(s, str):
        return ""
    s = CTRL.sub(" ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s[:limit]


def num(v, lo, hi, default=0.0):
    try:
        v = float(v)
    except (TypeError, ValueError):
        return default
    if v != v:  # NaN
        return default
    return max(lo, min(hi, v))


def public(p):
    return {"id": p["id"], "name": p["name"], "char": p["char"], "x": p["x"], "y": p["y"], "z": p["z"], "owner": p["owner"], "tired": p.get("tired", False)}


def client_ip(request):
    """Real visitor IP. Behind cloudflared the tunnel connects from localhost and
    puts the visitor's address in CF-Connecting-IP, so only trust that header then."""
    remote = request.remote or "?"
    ip = remote
    if remote in ("127.0.0.1", "::1"):
        h = request.headers.get("CF-Connecting-IP") or request.headers.get("X-Forwarded-For", "").split(",")[0].strip()
        if h:
            ip = h
    try:
        a = ipaddress.ip_address(ip)
        if a.version == 6 and a.ipv4_mapped:
            a = a.ipv4_mapped
        if a.version == 6:   # IPv6 users can rotate inside their /64, so group by it
            return str(ipaddress.ip_network(f"{a}/64", strict=False).network_address)
        return str(a)
    except ValueError:
        return ip


def admin_snapshot():
    """First (non-owner) player from an IP is 'normal', later ones from the same IP are alts of that first one."""
    first = {}
    normal, alts = [], []
    for p in sorted(players.values(), key=lambda q: q["seq"]):
        if p["owner"]:
            normal.append({"id": p["id"], "name": p["name"]})
        elif p["ip"] not in first:
            first[p["ip"]] = p
            normal.append({"id": p["id"], "name": p["name"]})
        else:
            alts.append({"id": p["id"], "name": p["name"], "of": first[p["ip"]]["name"]})
    return {"t": "admin", "total": len(players), "max": MAX_PLAYERS, "normal": normal, "alts": alts}


async def push_admin():
    owners = [p for p in players.values() if p["owner"]]
    if owners:
        data = json.dumps(admin_snapshot())
        await asyncio.gather(*(send(p, data) for p in owners), return_exceptions=True)


async def send(p, msg):
    try:
        await p["ws"].send_str(msg if isinstance(msg, str) else json.dumps(msg))
    except Exception:
        pass


async def broadcast(msg, exclude=None):
    data = json.dumps(msg)
    targets = [p for pid, p in players.items() if pid != exclude]
    if targets:
        await asyncio.gather(*(send(p, data) for p in targets), return_exceptions=True)


async def ws_handler(request):
    global next_id, _spawn_n
    ws = web.WebSocketResponse(heartbeat=15, max_msg_size=4096)
    await ws.prepare(request)
    me = None
    ip = client_ip(request)
    try:
        while True:
            try:
                msg = await ws.receive(timeout=90)
            except asyncio.TimeoutError:
                break  # nothing at all for 90s -> dead connection
            if msg.type != WSMsgType.TEXT:
                if msg.type in (WSMsgType.CLOSE, WSMsgType.CLOSING, WSMsgType.CLOSED, WSMsgType.ERROR):
                    break
                continue
            try:
                data = json.loads(msg.data)
            except ValueError:
                continue
            if not isinstance(data, dict):
                continue
            t = data.get("t")

            if t == "ping":
                await ws.send_str(json.dumps({"t": "pong", "c": data.get("c")}))
                continue

            if me is None:
                if t != "join":
                    continue
                raw = clean_text(data.get("name"), 40)
                is_owner = bool(OWNER_CODE) and hmac.compare_digest(raw.encode(), OWNER_CODE.encode())
                char = data.get("char")
                if not isinstance(char, str) or not NAME_RE.match(char) or (CHAR_SET and char not in CHAR_SET):
                    await ws.send_str(json.dumps({"t": "error", "m": "Unknown character."}))
                    break
                if is_owner:
                    name = OWNER_NAME
                else:
                    name = raw[:16].strip() or f"Cookie{random.randint(1000, 9999)}"
                    norm = re.sub(r"[^a-z0-9]", "", name.lower())
                    if norm.startswith("epicfridge") or re.search(r"\(\s*owner\s*\)", raw, re.I):
                        await ws.send_str(json.dumps({"t": "error", "m": "That username is reserved."}))
                        break
                    if len(players) >= MAX_PLAYERS:
                        await ws.send_str(json.dumps({"t": "error", "m": "Server is full."}))
                        break
                    if sum(1 for p in players.values() if p["ip"] == ip) >= MAX_PER_IP:
                        await ws.send_str(json.dumps({"t": "error", "m": f"Too many players from your network (max {MAX_PER_IP})."}))
                        print(f"[limit] blocked extra connection from {ip}")
                        break
                pid = str(next_id)
                next_id += 1
                me = {
                    "id": pid, "name": name, "char": char, "ws": ws, "ip": ip, "owner": is_owner, "seq": pid_seq(),
                    "x": random.uniform(-3, 3), "y": 0.0, "z": random.uniform(6, 10),
                    "chat_times": [], "tired": False, "car": None, "pt": time.time(), "vx": 0.0, "vz": 0.0,
                    "hold": None, "jellies": 0, "jt": [], "spawn_t": [], "baskets": 0,
                }
                others = [public(p) for p in players.values()]
                players[pid] = me
                await ws.send_str(json.dumps({
                    "t": "welcome", "id": pid, "name": name,
                    "spawn": {"x": me["x"], "y": me["y"], "z": me["z"]}, "owner": is_owner,
                    "players": others,
                    "balls": [ball_info(b) for b in BALLS],
                    "jellies": JELLIES, "jn": 0,
                    "cars": [car_public(c) for c in CARS], "score": list(SCORE),
                }))
                await broadcast({"t": "join", "p": public(me)}, exclude=pid)
                print(f"[+] {name} joined as {char} from {ip} ({len(players)} online)")
                await push_admin()
                continue

            if t == "s":
                nx = num(data.get("x"), -130, 130, me["x"])
                ny = num(data.get("y"), -50, 200, me["y"])
                nz = num(data.get("z"), -130, 130, me["z"])
                now = time.time()
                dt = now - me["pt"]
                if 0.02 < dt < 0.5:
                    me["vx"] = max(-16.0, min(16.0, 0.4 * me["vx"] + 0.6 * (nx - me["x"]) / dt))
                    me["vz"] = max(-16.0, min(16.0, 0.4 * me["vz"] + 0.6 * (nz - me["z"]) / dt))
                else:
                    me["vx"] = me["vz"] = 0.0
                me["pt"] = now
                me["x"], me["y"], me["z"] = nx, ny, nz
                if me["car"]:
                    continue
                await broadcast({"t": "s", "id": me["id"], "x": me["x"], "y": me["y"], "z": me["z"]}, exclude=me["id"])
            elif t == "emote":
                try:
                    n = int(data.get("n"))                # 0 = cancel the current emote, 9 = tired-mode toggle
                except (TypeError, ValueError):
                    continue
                if not 0 <= n <= 9:
                    continue
                msg = {"t": "emote", "id": me["id"], "n": n}
                if n == 9:
                    me["tired"] = bool(data.get("on"))
                    msg["on"] = me["tired"]
                await broadcast(msg, exclude=me["id"])
            elif t == "char":
                new = data.get("char")
                now = time.time()
                if (isinstance(new, str) and NAME_RE.match(new) and (not CHAR_SET or new in CHAR_SET)
                        and new != me["char"] and now - me.get("char_t", 0) > 1.5):
                    me["char"] = new
                    me["char_t"] = now
                    await broadcast({"t": "char", "id": me["id"], "char": new}, exclude=me["id"])
            elif t == "car_enter":
                car = next((c for c in CARS if c["id"] == data.get("id")), None)
                if (car and car["driver"] is None and not car["dead"] and not me["car"] and abs(car["x"] - me["x"]) < 7
                        and abs(car["z"] - me["z"]) < 7 and abs(car["y"] - me["y"]) < 4):
                    await release_hold(me)
                    car["driver"] = me["id"]; car["v"] = 0.0; car["t"] = time.time(); me["car"] = car["id"]
                    await broadcast({"t": "car", **car_public(car)})
                elif car:
                    await send(me, {"t": "car", **car_public(car, snap=True)})     # resync this client
            elif t == "car_exit":
                await free_car(me)
            elif t == "c":
                car = next((c for c in CARS if c["id"] == me["car"]), None)
                if car and car["driver"] == me["id"] and not car["dead"]:
                    car["x"] = num(data.get("x"), -130, 130, car["x"]); car["z"] = num(data.get("z"), -130, 130, car["z"])
                    car["y"] = num(data.get("y"), -5, 60, car["y"]); car["h"] = num(data.get("h"), -1000, 1000, car["h"])
                    car["v"] = num(data.get("v"), -40, 40, 0.0); car["t"] = time.time()
                    me["x"], me["y"], me["z"] = car["x"], car["y"], car["z"]
                    await broadcast({"t": "c", **car_public(car)}, exclude=me["id"])
            elif t == "car_boom":
                car = next((c for c in CARS if c["id"] == me["car"]), None)
                if car and car["driver"] == me["id"] and not car["dead"]:
                    explode_car(car, time.time())
                    print(f"[boom] {me['name']} wrecked {car['id']}")
                    await broadcast({"t": "boom", "id": car["id"], "x": round(car["x"], 2), "y": round(car["y"], 2),
                                     "z": round(car["z"], 2), "by": me["id"]})
                    await broadcast({"t": "car", **car_public(car)})
            elif t == "jelly":
                i = data.get("i")
                if not isinstance(i, int) or not 0 <= i < len(JELLIES):
                    continue
                now = time.time()
                me["jt"] = [x for x in me["jt"] if now - x < 1.0]
                if len(me["jt"]) >= 12:
                    continue
                jx, jy, jz = JELLIES[i]
                if abs(jx - me["x"]) < 4.5 and abs(jz - me["z"]) < 4.5 and -3.5 < jy - me["y"] < 4.5:
                    me["jt"].append(now)
                    me["jellies"] += 1
                    JELLIES[i] = jelly_spot()
                    await broadcast({"t": "jelly", "i": i, "p": JELLIES[i], "by": me["id"], "n": me["jellies"]})
                else:
                    await send(me, {"t": "jelly", "i": i, "p": JELLIES[i]})
            elif t == "grab":
                b = ball_by_id(data.get("id"))
                if (b and b["kind"] in GRABBABLE and not b["holder"] and not me["hold"] and not me["car"]
                        and abs(b["x"] - me["x"]) < 3.6 and abs(b["z"] - me["z"]) < 3.6 and -2.5 < b["y"] - me["y"] < 4.0):
                    b["holder"] = me["id"]; me["hold"] = b["id"]
                    b["vx"] = b["vy"] = b["vz"] = 0.0; b["asleep"] = True
                    await broadcast({"t": "hold", "id": b["id"], "by": me["id"]})
            elif t == "throw":
                b = drop_ball(me, num(data.get("fx"), -1, 1, 0.0), num(data.get("fz"), -1, 1, 1.0), num(data.get("aim"), 0, 1.4, 0.5), thrown=True)
                if b:
                    await broadcast({"t": "hold", "id": b["id"], "by": None})
            elif t == "spawn" and me["owner"]:
                kind = data.get("kind")
                now = time.time()
                me["spawn_t"] = [x for x in me["spawn_t"] if now - x < 1.0]
                if kind not in SPAWNABLE or len(me["spawn_t"]) >= 8:
                    continue
                if sum(1 for b in BALLS if b["spawned"]) >= MAX_SPAWNED:
                    await send(me, {"t": "notice", "m": f"Spawn limit reached ({MAX_SPAWNED}). Remove some first."})
                    continue
                me["spawn_t"].append(now)
                fx, fz = num(data.get("fx"), -1, 1, 0.0), num(data.get("fz"), -1, 1, 1.0)
                l = math.hypot(fx, fz) or 1.0
                _spawn_n += 1
                b = make_ball(f"s{_spawn_n}", kind, me["x"] + fx / l * 1.8, me["y"] + 2.5, me["z"] + fz / l * 1.8, spawned=True)
                b["asleep"] = False
                BALLS.append(b)
                await broadcast({"t": "ball_add", "b": ball_info(b)})
            elif t == "despawn_all" and me["owner"]:
                gone = [b for b in BALLS if b["spawned"]]
                for b in gone:
                    hp = players.get(b["holder"] or "")
                    if hp:
                        hp["hold"] = None
                BALLS[:] = [b for b in BALLS if not b["spawned"]]
                if gone:
                    await broadcast({"t": "ball_del", "ids": [b["id"] for b in gone]})
                await send(me, {"t": "notice", "m": f"Removed {len(gone)} spawned object(s)."})
            elif t == "reset_cars" and me["owner"]:
                for c in CARS:
                    drv = players.get(c["driver"] or "")
                    if drv:
                        drv["car"] = None
                    car_home(c)
                    await broadcast({"t": "car", **car_public(c, snap=True)})
            elif t == "reset_score" and me["owner"]:
                SCORE[0] = SCORE[1] = 0
                await broadcast({"t": "score", "score": list(SCORE)})
            elif t == "admin_list" and me["owner"]:
                await send(me, admin_snapshot())
            elif t == "kick" and me["owner"]:
                target = players.get(str(data.get("id", "")))
                if target and not target["owner"]:
                    print(f"[kick] {target['name']} was kicked by {me['name']}")
                    await send(target, {"t": "kicked"})
                    try:
                        await target["ws"].close(code=4001, message=b"kicked")
                    except Exception:
                        pass
            elif t == "chat":
                text = clean_text(data.get("m"), 200)
                if not text:
                    continue
                now = time.time()
                me["chat_times"] = [x for x in me["chat_times"] if now - x < 6]
                if len(me["chat_times"]) >= 5:
                    continue  # rate limited
                me["chat_times"].append(now)
                print(f"[chat] {me['name']}: {text}")
                await broadcast({"t": "chat", "id": me["id"], "name": me["name"], "m": text})
    except Exception as e:  # noqa
        print("[ws] error:", e)
    finally:
        if me and me.get("car"):
            await free_car(me)
        if me and me.get("hold"):
            await release_hold(me)
        if me and players.get(me["id"]) is me:
            del players[me["id"]]
            await broadcast({"t": "leave", "id": me["id"], "name": me["name"]})
            print(f"[-] {me['name']} left ({len(players)} online)")
            await push_admin()
        if not ws.closed:
            await ws.close()
    return ws


# ---------------------------------------------------------------- main
async def on_startup(app):
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, ensure_vendor)
    await loop.run_in_executor(None, refresh_characters)
    _jelly_png["tried"] = time.time()
    await loop.run_in_executor(None, fetch_jelly_png)
    app["physics"] = asyncio.ensure_future(physics_loop())
    app["chars"] = asyncio.ensure_future(character_refresher())
    print(f"[assets] character files are loaded by the browser from: {ASSET_BASE or 'this PC (./cookies)'}")


def main():
    app = web.Application()
    app.on_startup.append(on_startup)
    app.add_routes([
        web.get("/", index),
        web.get("/index.html", index),
        web.get("/api/characters", api_characters),
        web.get("/api/world", api_world),
        web.get("/cookies/{file}", asset),
        web.get("/vendor/{file}", vendor),
        web.get("/jelly.png", jelly_png),
        web.get("/ws", ws_handler),
    ])
    print(f"Witch's Castle server running on http://localhost:{PORT}")
    web.run_app(app, host="0.0.0.0", port=PORT, print=None)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
