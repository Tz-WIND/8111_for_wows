#!/usr/bin/env python3
# MIT License
# Copyright (c) 2026 Tz-WIND/Lex_q
#
# SPDX-License-Identifier: MIT
"""
8111 for WoWS -- local HTTP + WebSocket server (aiohttp).

Reads the JSON telemetry written by the in-game collector mod
(PnFMods/WowsExtractor) and exposes it over a War-Thunder-8111-style REST API
plus a WebSocket stream, for use by overlays, OBS, bots and analysis tools.

Design notes
------------
* The in-game Python sandbox cannot open localhost sockets, so the mod writes
  files and THIS process (a normal Python program) serves them. That is the
  same "file bridge" approach used by other WoWS data tools.
* Built on aiohttp; managed with uv (see ../pyproject.toml).
* Partial reads of state.json are tolerated: if a read/parse fails we keep the
  last good snapshot, so the mod's non-atomic writes never break a client.

Run (via uv):
    uv run python server/server.py --demo
    uv run python server/server.py --game-dir "D:/Games/World_of_Warships"
    uv run python server/server.py --state-file "<path>/state.json"
"""

import argparse
import asyncio
import ipaddress
import json
import math
import os
import sys
import time

import aiohttp
from aiohttp import web

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_STATIC = os.path.join(HERE, "static")

# Sibling module: the generated map-recognition table (tools/gen_maps.py).
if HERE not in sys.path:
    sys.path.insert(0, HERE)
import maps  # noqa: E402


# ===========================================================================
# shared state store
# ===========================================================================
class Store:
    def __init__(self):
        self.version = 0
        self.meta = {}
        self.state = {}
        self.last_update = 0.0
        self.ws_clients = set()      # set[web.WebSocketResponse]

    def set(self, meta=None, state=None):
        if meta is not None:
            self.meta = meta
        if state is not None:
            self.state = state
        self.last_update = time.time()
        self.version += 1


# ===========================================================================
# coordinate normalization + view models  (pure functions)
# ===========================================================================
def _numeric_bounds(m):
    """World bounds straight from explicit numbers the mod emitted, or None.

    Prefers explicit min/max (runtime arena data); falls back to width/height.
    """
    m = m or {}

    def num(*keys):
        for k in keys:
            v = m.get(k)
            if isinstance(v, (int, float)):
                return float(v)
        return None

    minx, maxx = num("minX"), num("maxX")
    minz, maxz = num("minZ", "minY"), num("maxZ", "maxY")
    if None not in (minx, maxx, minz, maxz) and maxx > minx and maxz > minz:
        return (minx, maxx, minz, maxz)

    w = num("width", "worldSize", "size")
    h = num("height") or w
    if w and h:
        return (-w / 2.0, w / 2.0, -h / 2.0, h / 2.0)
    return None


def resolve_map_info(meta):
    """Identify the map and its world bounds.

    Combines two sources, in priority order:
      1. explicit numeric bounds the mod read from the live arena (most exact);
      2. the generated recognition table keyed by the in-game space name
         (origin-centred per-map bounds from `space.settings`).

    Returns {id, name, raw, bounds, boundsSource}. bounds is None when the map
    is unknown and no numbers were provided (the overlay then auto-fits).
    """
    m = (meta or {}).get("map") or {}
    candidates = [
        m.get("id"), m.get("spaceId"), m.get("geometry"), m.get("geometryName"),
        m.get("spaceName"), m.get("mapPath"), m.get("name"), m.get("mapName"),
    ]
    raw = None
    hit = None
    for candidate in candidates:
        if candidate is None or candidate == "":
            continue
        if raw is None:
            raw = candidate
        hit = maps.resolve_map(candidate)
        if hit:
            raw = candidate
            break
        for part in str(candidate).replace("\\", "/").split("/"):
            hit = maps.resolve_map(part)
            if hit:
                raw = part
                break
        if hit:
            break

    bounds = _numeric_bounds(m)
    source = "runtime" if bounds else None
    if bounds is None and hit and hit.get("bounds"):
        bounds = tuple(hit["bounds"])
        source = "table"

    name = (hit["name"] if hit else None) or m.get("name") or m.get("mapName") or raw
    map_id = hit["id"] if hit else None
    return {"id": map_id, "name": name, "raw": raw,
            "bounds": bounds, "boundsSource": source}


def merge_map_out(meta, info):
    """Merge resolved map identity into the mod's raw map object for API output."""
    map_out = dict((meta or {}).get("map", {}) or {})
    if info["id"]:
        map_out["id"] = info["id"]
    if info["name"]:
        map_out["name"] = info["name"]
    if info.get("raw"):
        map_out["raw"] = info["raw"]
    return map_out


def extract_bounds(meta):
    """Return (minX, maxX, minZ, maxZ) for the current map, or None if unknown."""
    return resolve_map_info(meta)["bounds"]


def normalize(pos, bounds):
    """World [x, y, z] -> (nx, ny) in [0,1], origin top-left, north up."""
    if not pos or bounds is None:
        return None
    minx, maxx, minz, maxz = bounds
    try:
        nx = (pos[0] - minx) / (maxx - minx)
        ny = 1.0 - (pos[2] - minz) / (maxz - minz)
        return [nx, ny]
    except Exception:
        return None


def normalize_direct(nx, ny):
    """Pass through trusted minimap-normalized coordinates from the collector."""
    if not isinstance(nx, (int, float)) or not isinstance(ny, (int, float)):
        return None
    if isinstance(nx, bool) or isinstance(ny, bool):
        return None
    if not math.isfinite(nx) or not math.isfinite(ny):
        return None
    if nx < -0.01 or nx > 1.01 or ny < -0.01 or ny > 1.01:
        return None
    return [min(1.0, max(0.0, float(nx))), min(1.0, max(0.0, float(ny)))]


def build_map_objects(meta, state, bounds=None):
    if bounds is None:
        bounds = extract_bounds(meta)
    roster = {}
    for r in (meta or {}).get("roster", []) or []:
        pid = r.get("playerId")
        if pid is not None:
            roster[pid] = r

    objects = []
    for s in (state or {}).get("ships", []) or []:
        pid = s.get("playerId")
        meta_r = roster.get(pid, {})
        pos = s.get("position")
        norm = normalize(pos, bounds)
        if norm is None:
            norm = normalize_direct(s.get("nx", s.get("mapX")),
                                    s.get("ny", s.get("mapY")))
        mh = s.get("maxHealth") or meta_r.get("maxHealth")
        hp = s.get("health")
        team = s.get("teamId")
        if team is None:
            team = meta_r.get("teamId")
        rel = s.get("relation")
        if rel not in (0, 1, 2):
            rel = meta_r.get("relation")
        obj = {
            "uiId": s.get("uiId"),
            "vehicleId": s.get("vehicleId"),
            "playerId": pid,
            "teamId": team,
            "relation": rel,
            "type": s.get("shipType") or meta_r.get("shipType"),
            "name": s.get("name") or meta_r.get("shipName"),
            "playerName": meta_r.get("name"),
            "tier": meta_r.get("shipTier"),
            "alive": s.get("alive"),
            "visible": s.get("visible", pos is not None or norm is not None),
            "x": pos[0] if pos else None,
            "z": pos[2] if pos else None,
            "yaw": s.get("yaw"),
            "health": hp,
            "maxHealth": mh,
            "hpRatio": (hp / mh) if (isinstance(hp, (int, float)) and mh) else None,
        }
        if norm:
            obj["nx"], obj["ny"] = norm[0], norm[1]

        # last-known ("ghost") position for ships that lit up then went dark
        last_pos = s.get("lastPosition")
        if last_pos:
            obj["lastX"], obj["lastZ"] = last_pos[0], last_pos[2]
            lnorm = normalize(last_pos, bounds)
            if lnorm is None:
                lnorm = normalize_direct(s.get("lastNx", s.get("lastMapX")),
                                         s.get("lastNy", s.get("lastMapY")))
            if lnorm:
                obj["lastNx"], obj["lastNy"] = lnorm[0], lnorm[1]
            if s.get("lastYaw") is not None:
                obj["lastYaw"] = s.get("lastYaw")
            if s.get("lastHealth") is not None:
                obj["lastHealth"] = s.get("lastHealth")
            obj["lastSeenTs"] = s.get("lastSeenTs")
            obj["staleSeconds"] = s.get("staleSeconds")
        elif s.get("lastNx") is not None or s.get("lastMapX") is not None:
            lnorm = normalize_direct(s.get("lastNx", s.get("lastMapX")),
                                     s.get("lastNy", s.get("lastMapY")))
            if lnorm:
                obj["lastNx"], obj["lastNy"] = lnorm[0], lnorm[1]
                if s.get("lastYaw") is not None:
                    obj["lastYaw"] = s.get("lastYaw")
                if s.get("lastHealth") is not None:
                    obj["lastHealth"] = s.get("lastHealth")
                obj["lastSeenTs"] = s.get("lastSeenTs")
                obj["staleSeconds"] = s.get("staleSeconds")

        objects.append(obj)
    return objects, bounds


def build_all(meta, state):
    info = resolve_map_info(meta)
    objects, bounds = build_map_objects(meta, state, info["bounds"])
    map_out = merge_map_out(meta, info)
    battle_type = (meta or {}).get("battleType") or (meta or {}).get("gameMode")
    return {
        "schema": 1,
        "active": (state or {}).get("active", False),
        "ts": (state or {}).get("ts"),
        "battleType": battle_type,
        "gameMode": (meta or {}).get("gameMode"),
        "map": map_out,
        "bounds": list(bounds) if bounds else None,
        "boundsSource": info["boundsSource"],
        "self": (state or {}).get("self"),
        "objects": objects,
        "roster": (meta or {}).get("roster", []),
        "damage": (state or {}).get("damage", {}),
        "ballistics": (state or {}).get("ballistics", {}),
        "diag": (state or {}).get("diag"),
    }


# ===========================================================================
# background producers
# ===========================================================================
def read_json(path):
    try:
        with open(path, "rb") as f:
            return json.loads(f.read().decode("utf-8"))
    except Exception:
        return None


async def broadcast(app):
    store = app["store"]
    if not store.ws_clients:
        return
    payload = json.dumps(build_all(store.meta, store.state))
    dead = []
    for ws in list(store.ws_clients):
        try:
            await ws.send_str(payload)
        except Exception:
            dead.append(ws)
    for ws in dead:
        store.ws_clients.discard(ws)


async def file_watcher(app, state_file, meta_file, interval):
    store = app["store"]
    state_mtime = None
    meta_mtime = None
    while True:
        changed = False
        try:
            mt = await asyncio.to_thread(os.path.getmtime, meta_file)
            if mt != meta_mtime:
                data = await asyncio.to_thread(read_json, meta_file)
                if data is not None:
                    store.meta = data
                    meta_mtime = mt
                    changed = True
        except OSError:
            pass
        try:
            st = await asyncio.to_thread(os.path.getmtime, state_file)
            if st != state_mtime:
                data = await asyncio.to_thread(read_json, state_file)
                if data is not None:
                    store.state = data
                    state_mtime = st
                    changed = True
        except OSError:
            pass
        if changed:
            store.last_update = time.time()
            store.version += 1
            await broadcast(app)
        await asyncio.sleep(interval)


async def demo_generator(app, interval):
    store = app["store"]
    HALF = 21000.0
    names = ["Yamato", "Montana", "Kremlin", "Shimakaze", "Gearing",
             "Des Moines", "Zao", "Worcester", "Halland", "Smolensk"]
    types = ["Battleship", "Battleship", "Battleship", "Destroyer", "Destroyer",
             "Cruiser", "Cruiser", "Cruiser", "Destroyer", "Cruiser"]
    ships = []
    for i in range(10):
        team = 0 if i < 5 else 1
        ships.append({"uiId": 1000 + i, "vehicleId": 1000 + i, "playerId": 2000 + i,
                      "teamId": team, "name": names[i], "type": types[i], "tier": 10,
                      "phase": i * 0.6, "radius": 6000 + (i % 5) * 1800})

    roster = [{
        "playerId": s["playerId"], "teamId": s["teamId"],
        "relation": 1 if s["teamId"] == 0 else 2,
        "name": "Player_%d" % s["playerId"], "shipName": s["name"],
        "shipType": s["type"], "shipTier": s["tier"],
        "consumables": ([{"title": "RLSSearch", "params": {"distShip": 11700}}]
                        if s["type"] == "Cruiser" else []),
    } for s in ships]
    store.set(meta={
        "schema": 1, "battleType": "DemoBattle", "selfPlayerId": 2000,
        "map": {"name": "Demo Atoll", "minX": -HALF, "maxX": HALF,
                "minZ": -HALF, "maxZ": HALF},
        "roster": roster,
    })

    last = {}   # uiId -> last visible {pos, yaw, health, ts}; drives demo ghosts
    t0 = time.time()
    while True:
        t = time.time() - t0
        objs = []
        for s in ships:
            ang = s["phase"] + t * 0.05 * (1 if s["teamId"] == 0 else -1)
            x = s["radius"] * math.cos(ang)
            z = s["radius"] * math.sin(ang) + (-6000 if s["teamId"] == 0 else 6000)
            yaw = ang + math.pi / 2
            health = 80000 * (0.4 + 0.6 * abs(math.sin(ang)))
            # allies always visible; enemies blink in/out to simulate spotting
            visible = (s["teamId"] == 0) or (math.sin(t * 0.25 + s["phase"] * 1.7) > -0.35)
            obj = {"uiId": s["uiId"], "vehicleId": s["vehicleId"], "playerId": s["playerId"],
                   "teamId": s["teamId"], "relation": 1 if s["teamId"] == 0 else 2,
                   "alive": True, "maxHealth": 80000}
            if visible:
                last[s["uiId"]] = {"pos": [x, 0.0, z], "yaw": yaw, "health": health, "ts": t}
                obj.update({"visible": True, "position": [x, 0.0, z],
                            "yaw": yaw, "health": health})
            else:
                obj["visible"] = False
                seen = last.get(s["uiId"])
                if seen is not None:
                    obj["lastPosition"] = seen["pos"]
                    obj["lastYaw"] = seen["yaw"]
                    obj["lastHealth"] = seen["health"]
                    obj["lastSeenTs"] = seen["ts"]
                    obj["staleSeconds"] = t - seen["ts"]
            objs.append(obj)
        self_obj = objs[0]   # ship 0 is an always-visible ally
        vis_allies = sum(1 for o in objs if o.get("teamId") == 0 and o.get("visible"))
        vis_enemies = sum(1 for o in objs if o.get("teamId") == 1 and o.get("visible"))
        ghost_n = sum(1 for o in objs if not o.get("visible") and o.get("lastPosition"))
        store.set(state={
            "schema": 1, "active": True, "ts": t,
            "self": {"playerId": 2000, "teamId": 0, "position": self_obj.get("position"),
                     "yaw": self_obj.get("yaw"), "health": self_obj.get("health"),
                     "maxHealth": 80000, "speed": 30.0, "isObserver": False},
            "ships": objs,
            "damage": {"inflicted": {}, "received": {}, "teamTotal": {}},
            "ballistics": {"available": True, "ammoType": "AP", "penetration": 650},
            "diag": {"totalShips": 10, "allies": 5, "alliesVisible": vis_allies,
                     "enemies": 5, "enemiesVisible": vis_enemies, "ghosts": ghost_n},
        })
        await broadcast(app)
        await asyncio.sleep(interval)


# ===========================================================================
# HTTP / WebSocket handlers
# ===========================================================================
def is_loopback_host(host):
    if not host:
        return False
    host = str(host).strip().strip("[]").lower()
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def default_allowed_origins(port):
    return {
        "http://127.0.0.1:%d" % port,
        "http://localhost:%d" % port,
        "http://[::1]:%d" % port,
    }


def parse_allowed_origins(raw, port):
    if raw is None or raw == "":
        return default_allowed_origins(port)
    if isinstance(raw, (list, tuple, set)):
        parts = raw
    else:
        parts = str(raw).split(",")
    origins = set()
    for item in parts:
        origin = str(item).strip().rstrip("/")
        if not origin:
            continue
        if origin.lower() in ("none", "off", "false"):
            continue
        origins.add(origin)
    return origins


def parse_bool(raw, default=False):
    if raw is None or raw == "":
        return default
    if isinstance(raw, bool):
        return raw
    val = str(raw).strip().lower()
    if val in ("1", "true", "yes", "on"):
        return True
    if val in ("0", "false", "no", "off"):
        return False
    return default


def origin_allowed(origin, allowed_origins):
    if not origin:
        return False
    normalized = origin.strip().rstrip("/")
    return "*" in allowed_origins or normalized in allowed_origins


def append_vary(resp, value):
    existing = resp.headers.get("Vary")
    if not existing:
        resp.headers["Vary"] = value
        return
    parts = [p.strip() for p in existing.split(",")]
    if value.lower() not in [p.lower() for p in parts]:
        resp.headers["Vary"] = existing + ", " + value


def apply_cors_headers(request, resp):
    origin = request.headers.get("Origin")
    allowed_origins = request.app["config"].get("allowed_origins", set())
    if not origin_allowed(origin, allowed_origins):
        return False
    resp.headers["Access-Control-Allow-Origin"] = (
        "*" if "*" in allowed_origins else origin.strip().rstrip("/")
    )
    if "*" not in allowed_origins:
        append_vary(resp, "Origin")
    resp.headers["Access-Control-Allow-Methods"] = "GET, OPTIONS"
    req_headers = request.headers.get("Access-Control-Request-Headers")
    if req_headers:
        resp.headers["Access-Control-Allow-Headers"] = req_headers
    return True


@web.middleware
async def cors_middleware(request, handler):
    if request.method == "OPTIONS":
        if request.headers.get("Origin") and not origin_allowed(
                request.headers.get("Origin"),
                request.app["config"].get("allowed_origins", set())):
            return web.Response(status=403)
        resp = web.Response(status=204)
        apply_cors_headers(request, resp)
        resp.headers.setdefault("Cache-Control", "no-store")
        return resp
    try:
        resp = await handler(request)
    except web.HTTPException as ex:
        apply_cors_headers(request, ex)
        raise
    apply_cors_headers(request, resp)
    resp.headers.setdefault("Cache-Control", "no-store")
    return resp


def jr(obj, status=200):
    return web.json_response(obj, status=status)


async def h_index(request):
    return web.Response(text=INDEX_HTML, content_type="text/html")


async def h_overlay(request):
    path = os.path.join(request.app["static_dir"], "overlay.html")
    if not os.path.isfile(path):
        return jr({"error": "overlay.html not found", "path": path}, 404)
    return web.FileResponse(path)


async def h_healthz(request):
    store = request.app["store"]
    return jr({
        "ok": True,
        "battleActive": (store.state or {}).get("active", False),
        "ageSeconds": round(time.time() - store.last_update, 2) if store.last_update else None,
        "version": store.version,
        "wsClients": len(store.ws_clients),
        "hasMeta": bool(store.meta),
        "hasState": bool(store.state),
    })


async def h_all(request):
    s = request.app["store"]
    return jr(build_all(s.meta, s.state))


async def h_map_obj(request):
    s = request.app["store"]
    objects, _ = build_map_objects(s.meta, s.state)
    return jr(objects)


async def h_map_info(request):
    s = request.app["store"]
    info = resolve_map_info(s.meta)
    map_out = merge_map_out(s.meta, info)
    battle_type = (s.meta or {}).get("battleType") or (s.meta or {}).get("gameMode")
    return jr({
        "map": map_out,
        "mapId": info["id"],
        "mapName": info["name"],
        "battleType": battle_type,
        "gameMode": (s.meta or {}).get("gameMode"),
        "bounds": list(info["bounds"]) if info["bounds"] else None,
        "boundsKnown": info["bounds"] is not None,
        "boundsSource": info["boundsSource"],
    })


async def h_state(request):
    s = request.app["store"]
    return jr((s.state or {}).get("self") or {})


async def h_indicators(request):
    self_ = (request.app["store"].state or {}).get("self") or {}
    return jr({
        "yaw": self_.get("yaw"), "speed": self_.get("speed"),
        "health": self_.get("health"), "maxHealth": self_.get("maxHealth"),
        "position": self_.get("position"), "isObserver": self_.get("isObserver"),
    })


async def h_roster(request):
    return jr((request.app["store"].meta or {}).get("roster", []))


async def h_damage(request):
    return jr((request.app["store"].state or {}).get("damage", {}))


async def h_ballistics(request):
    return jr((request.app["store"].state or {}).get("ballistics", {}))


async def h_debug(request):
    s = request.app["store"]
    state = s.state or {}
    diag = state.get("diag", {})
    ships_raw = state.get("ships", [])
    per_ship = []
    for sh in ships_raw:
        per_ship.append({
            "uiId": sh.get("uiId"),
            "playerId": sh.get("playerId"),
            "relation": sh.get("relation"),
            "teamId": sh.get("teamId"),
            "alive": sh.get("alive"),
            "visible": sh.get("visible"),
            "hasPosition": sh.get("position") is not None,
            "hasLastPosition": sh.get("lastPosition") is not None,
            "staleSeconds": sh.get("staleSeconds"),
            "name": sh.get("name"),
            "shipType": sh.get("shipType"),
        })
    return jr({"diag": diag, "ships": per_ship})


async def h_ws(request):
    origin = request.headers.get("Origin")
    if origin and not origin_allowed(
            origin, request.app["config"].get("allowed_origins", set())):
        raise web.HTTPForbidden(text="WebSocket Origin not allowed")
    store = request.app["store"]
    ws = web.WebSocketResponse(heartbeat=20.0)
    await ws.prepare(request)
    store.ws_clients.add(ws)
    try:
        await ws.send_str(json.dumps(build_all(store.meta, store.state)))
        async for msg in ws:
            if msg.type == aiohttp.WSMsgType.ERROR:
                break
            # client -> server messages are ignored
    finally:
        store.ws_clients.discard(ws)
    return ws


async def h_not_found(request):
    return jr({"error": "not found", "path": request.path}, 404)


INDEX_HTML = """<!doctype html><html><head><meta charset="utf-8">
<title>8111 for WoWS</title>
<style>
 body{font:14px/1.5 system-ui,Segoe UI,Arial;margin:2rem;max-width:820px;color:#222}
 h1{font-size:1.4rem} code{background:#f2f2f2;padding:1px 5px;border-radius:4px}
 li{margin:.25rem 0} a{color:#0a58ca}
 .tag{display:inline-block;min-width:135px;font-weight:600}
</style></head><body>
<h1>8111 for World of Warships</h1>
<p>Local telemetry bridge (aiohttp). Browser origins are checked for HTTP CORS and WebSocket handshakes.</p>
<ul>
 <li><span class="tag"><a href="/all">/all</a></span> full merged snapshot</li>
 <li><span class="tag"><a href="/map_obj.json">/map_obj.json</a></span> all visible objects (normalized + world coords)</li>
 <li><span class="tag"><a href="/map_info.json">/map_info.json</a></span> map name, world bounds, battle type</li>
 <li><span class="tag"><a href="/state">/state</a></span> own ship state</li>
 <li><span class="tag"><a href="/indicators">/indicators</a></span> own heading / speed / hp</li>
 <li><span class="tag"><a href="/roster">/roster</a></span> 24-player roster + consumable ranges</li>
 <li><span class="tag"><a href="/damage">/damage</a></span> inflicted / received / team totals</li>
 <li><span class="tag"><a href="/ballistics">/ballistics</a></span> current shell penetration / ricochet</li>
 <li><span class="tag"><a href="/debug">/debug</a></span> per-ship visibility diagnostics</li>
 <li><span class="tag">/ws</span> WebSocket stream (pushes <code>/all</code> on every update)</li>
 <li><span class="tag"><a href="/healthz">/healthz</a></span> server status</li>
 <li><span class="tag"><a href="/overlay">/overlay</a></span> demo minimap overlay</li>
</ul>
</body></html>"""


# ===========================================================================
# bootstrap
# ===========================================================================
def load_config_file(path):
    """Parse a trivial `key = value` config (same format the in-game mod reads).
    Missing file -> empty dict. '#' or ';' begins a comment."""
    cfg = {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            text = f.read()
    except OSError:
        return cfg
    except Exception:
        return cfg
    for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        line = line.strip()
        if not line or line[0] in "#;" or "=" not in line:
            continue
        i = line.find("=")
        key = line[:i].strip().lower()
        val = line[i + 1:].strip()
        if key:
            cfg[key] = val
    return cfg


def find_state_file(game_dir):
    """Locate state.json under <game_dir>/bin/<build>/res_mods/PnFMods/WowsExtractor/."""
    bin_dir = os.path.join(game_dir, "bin")
    if not os.path.isdir(bin_dir):
        return None
    builds = []
    for name in os.listdir(bin_dir):
        full = os.path.join(bin_dir, name)
        if os.path.isdir(full):
            try:
                builds.append((int(name), full))
            except ValueError:
                builds.append((-1, full))
    builds.sort(reverse=True)
    for _, full in builds:
        candidate = os.path.join(full, "res_mods", "PnFMods", "WowsExtractor", "state.json")
        if os.path.isdir(os.path.dirname(candidate)):
            return candidate
    return None


def build_app(config):
    config = dict(config)
    config.setdefault("allowed_origins", set())
    app = web.Application(middlewares=[cors_middleware])
    app["store"] = Store()
    app["config"] = config
    app["static_dir"] = config["static_dir"]

    app.router.add_get("/", h_index)
    app.router.add_get("/healthz", h_healthz)
    app.router.add_get("/all", h_all)
    app.router.add_get("/map_obj.json", h_map_obj)
    app.router.add_get("/map_obj", h_map_obj)
    app.router.add_get("/map_info.json", h_map_info)
    app.router.add_get("/map_info", h_map_info)
    app.router.add_get("/state", h_state)
    app.router.add_get("/indicators", h_indicators)
    app.router.add_get("/roster", h_roster)
    app.router.add_get("/damage", h_damage)
    app.router.add_get("/ballistics", h_ballistics)
    app.router.add_get("/debug", h_debug)
    app.router.add_get("/ws", h_ws)
    app.router.add_get("/overlay", h_overlay)
    app.router.add_get("/overlay.html", h_overlay)
    app.router.add_route("*", "/{tail:.*}", h_not_found)

    async def on_startup(app):
        cfg = app["config"]
        if cfg["demo"]:
            app["bg"] = asyncio.create_task(demo_generator(app, cfg["interval"]))
        else:
            app["bg"] = asyncio.create_task(
                file_watcher(app, cfg["state_file"], cfg["meta_file"], cfg["interval"]))

    async def on_cleanup(app):
        task = app.get("bg")
        if task:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        for ws in list(app["store"].ws_clients):
            await ws.close()

    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    return app


def parse_args(argv):
    # Defaults are intentionally None so we can tell "user passed this flag" apart
    # from "use the config.ini value (or the built-in default)". See resolve in main().
    p = argparse.ArgumentParser(description="8111-style telemetry server for World of Warships")
    p.add_argument("--config", help="path to config.ini (default: repo-root config.ini)")
    p.add_argument("--host", default=None)
    p.add_argument("--port", type=int, default=None)
    p.add_argument("--state-file", help="explicit path to state.json")
    p.add_argument("--meta-file", help="explicit path to meta.json (defaults to state dir)")
    p.add_argument("--game-dir", help="World_of_Warships install dir (auto-find state.json)")
    p.add_argument("--poll-interval", type=float, default=None, help="file poll interval (s)")
    p.add_argument("--static-dir", default=None)
    p.add_argument("--demo", action="store_true", help="serve synthetic data (no game needed)")
    p.add_argument("--allow-remote", action="store_true", default=None,
                   help="allow listening on non-loopback hosts such as 0.0.0.0")
    p.add_argument("--allowed-origin", action="append", default=None,
                   help="CORS origin to allow; repeat or use config allowed_origins")
    return p.parse_args(argv)


def main(argv=None):
    args = parse_args(argv if argv is not None else sys.argv[1:])

    # Load config.ini (repo root by default). Precedence: CLI flag > config.ini > built-in default.
    repo_root = os.path.dirname(HERE)
    config_path = args.config or os.path.join(repo_root, "config.ini")
    file_cfg = load_config_file(config_path)
    cfg_loaded = bool(file_cfg)

    def pick(cli_val, key, default, cast=str):
        if cli_val is not None:
            return cli_val
        raw = file_cfg.get(key)
        if raw is None or raw == "":
            return default
        try:
            return cast(raw)
        except (TypeError, ValueError):
            print("[warn] config.ini: bad value for %s=%r, using %r" % (key, raw, default))
            return default

    host = pick(args.host, "host", "127.0.0.1")
    port = pick(args.port, "port", 8111, int)
    poll_interval = pick(args.poll_interval, "poll_interval", 0.1, float)
    static_dir = pick(args.static_dir, "static_dir", DEFAULT_STATIC)
    game_dir = pick(args.game_dir, "game_dir", None)
    state_file = pick(args.state_file, "state_file", None)
    meta_file = pick(args.meta_file, "meta_file", None)
    allow_remote = (args.allow_remote if args.allow_remote is not None
                    else parse_bool(file_cfg.get("allow_remote"), False))
    allowed_origins = parse_allowed_origins(
        args.allowed_origin if args.allowed_origin is not None else file_cfg.get("allowed_origins"),
        port)

    if not is_loopback_host(host) and not allow_remote:
        print("ERROR: refusing to listen on non-loopback host %r by default." % host)
        print("       Use --allow-remote or set allow_remote = true if you intend LAN access.")
        return 2

    if args.demo:
        source_desc = "DEMO (synthetic data)"
        config = {"demo": True, "interval": max(poll_interval, 0.05),
                  "static_dir": static_dir, "state_file": None, "meta_file": None,
                  "allowed_origins": allowed_origins}
    else:
        if not state_file and game_dir:
            state_file = find_state_file(game_dir)
            if not state_file:
                print("[warn] could not auto-find state.json under %s/bin/*/res_mods/..." % game_dir)
        if not state_file:
            print("ERROR: set 'game_dir' in %s, or pass --game-dir/--state-file, or use --demo"
                  % config_path)
            return 2
        if not meta_file:
            meta_file = os.path.join(os.path.dirname(state_file), "meta.json")
        source_desc = "state=%s" % state_file
        config = {"demo": False, "interval": max(poll_interval, 0.02),
                  "static_dir": static_dir, "state_file": state_file, "meta_file": meta_file,
                  "allowed_origins": allowed_origins}

    print("=" * 64)
    print(" 8111 for WoWS (aiohttp)  --  http://%s:%d/" % (host, port))
    print(" overlay:        http://%s:%d/overlay" % (host, port))
    print(" websocket:      ws://%s:%d/ws" % (host, port))
    print(" config:         %s" % (config_path if cfg_loaded else "%s (not found, using defaults)" % config_path))
    print(" source:         %s" % source_desc)
    print(" CORS origins:   %s" % (", ".join(sorted(allowed_origins)) if allowed_origins else "(none)"))
    if "*" in allowed_origins:
        print("[warn] CORS wildcard enabled; any webpage can read this local telemetry endpoint.")
    print("=" * 64)

    app = build_app(config)
    web.run_app(app, host=host, port=port, access_log=None, print=None)
    return 0


if __name__ == "__main__":
    sys.exit(main())
