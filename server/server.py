#!/usr/bin/env python3
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
import json
import math
import os
import sys
import time

import aiohttp
from aiohttp import web

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_STATIC = os.path.join(HERE, "static")


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
def extract_bounds(meta):
    """Return (minX, maxX, minZ, maxZ) from meta.map, or None if unknown."""
    m = (meta or {}).get("map") or {}

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


def build_map_objects(meta, state):
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
            "visible": s.get("visible", pos is not None),
            "x": pos[0] if pos else None,
            "z": pos[2] if pos else None,
            "yaw": s.get("yaw"),
            "health": hp,
            "maxHealth": mh,
            "hpRatio": (hp / mh) if (isinstance(hp, (int, float)) and mh) else None,
        }
        if norm:
            obj["nx"], obj["ny"] = norm[0], norm[1]
        objects.append(obj)
    return objects, bounds


def build_all(meta, state):
    objects, bounds = build_map_objects(meta, state)
    return {
        "schema": 1,
        "active": (state or {}).get("active", False),
        "ts": (state or {}).get("ts"),
        "battleType": (meta or {}).get("battleType"),
        "map": (meta or {}).get("map", {}),
        "bounds": list(bounds) if bounds else None,
        "self": (state or {}).get("self"),
        "objects": objects,
        "roster": (meta or {}).get("roster", []),
        "damage": (state or {}).get("damage", {}),
        "ballistics": (state or {}).get("ballistics", {}),
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

    t0 = time.time()
    while True:
        t = time.time() - t0
        objs = []
        for s in ships:
            ang = s["phase"] + t * 0.05 * (1 if s["teamId"] == 0 else -1)
            x = s["radius"] * math.cos(ang)
            z = s["radius"] * math.sin(ang) + (-6000 if s["teamId"] == 0 else 6000)
            objs.append({
                "uiId": s["uiId"], "vehicleId": s["vehicleId"], "playerId": s["playerId"],
                "teamId": s["teamId"], "relation": 1 if s["teamId"] == 0 else 2,
                "alive": True, "visible": True, "position": [x, 0.0, z],
                "yaw": ang + math.pi / 2,
                "health": 80000 * (0.4 + 0.6 * abs(math.sin(ang))), "maxHealth": 80000,
            })
        store.set(state={
            "schema": 1, "active": True, "ts": t,
            "self": {"playerId": 2000, "teamId": 0, "position": objs[0]["position"],
                     "yaw": objs[0]["yaw"], "health": objs[0]["health"],
                     "maxHealth": 80000, "speed": 30.0, "isObserver": False},
            "ships": objs,
            "damage": {"inflicted": {}, "received": {}, "teamTotal": {}},
            "ballistics": {"available": True, "ammoType": "AP", "penetration": 650},
        })
        await broadcast(app)
        await asyncio.sleep(interval)


# ===========================================================================
# HTTP / WebSocket handlers
# ===========================================================================
@web.middleware
async def cors_middleware(request, handler):
    try:
        resp = await handler(request)
    except web.HTTPException as ex:
        ex.headers["Access-Control-Allow-Origin"] = "*"
        raise
    resp.headers["Access-Control-Allow-Origin"] = "*"
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
    bounds = extract_bounds(s.meta)
    return jr({
        "map": (s.meta or {}).get("map", {}),
        "battleType": (s.meta or {}).get("battleType"),
        "bounds": list(bounds) if bounds else None,
        "boundsKnown": bounds is not None,
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


async def h_ws(request):
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
<p>Local telemetry bridge (aiohttp). Endpoints (JSON, <code>Access-Control-Allow-Origin: *</code>):</p>
<ul>
 <li><span class="tag"><a href="/all">/all</a></span> full merged snapshot</li>
 <li><span class="tag"><a href="/map_obj.json">/map_obj.json</a></span> all visible objects (normalized + world coords)</li>
 <li><span class="tag"><a href="/map_info.json">/map_info.json</a></span> map name, world bounds, battle type</li>
 <li><span class="tag"><a href="/state">/state</a></span> own ship state</li>
 <li><span class="tag"><a href="/indicators">/indicators</a></span> own heading / speed / hp</li>
 <li><span class="tag"><a href="/roster">/roster</a></span> 24-player roster + consumable ranges</li>
 <li><span class="tag"><a href="/damage">/damage</a></span> inflicted / received / team totals</li>
 <li><span class="tag"><a href="/ballistics">/ballistics</a></span> current shell penetration / ricochet</li>
 <li><span class="tag">/ws</span> WebSocket stream (pushes <code>/all</code> on every update)</li>
 <li><span class="tag"><a href="/healthz">/healthz</a></span> server status</li>
 <li><span class="tag"><a href="/overlay">/overlay</a></span> demo minimap overlay</li>
</ul>
</body></html>"""


# ===========================================================================
# bootstrap
# ===========================================================================
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
    p = argparse.ArgumentParser(description="8111-style telemetry server for World of Warships")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8111)
    p.add_argument("--state-file", help="explicit path to state.json")
    p.add_argument("--meta-file", help="explicit path to meta.json (defaults to state dir)")
    p.add_argument("--game-dir", help="World_of_Warships install dir (auto-find state.json)")
    p.add_argument("--poll-interval", type=float, default=0.1, help="file poll interval (s)")
    p.add_argument("--static-dir", default=DEFAULT_STATIC)
    p.add_argument("--demo", action="store_true", help="serve synthetic data (no game needed)")
    return p.parse_args(argv)


def main(argv=None):
    args = parse_args(argv if argv is not None else sys.argv[1:])

    if args.demo:
        source_desc = "DEMO (synthetic data)"
        config = {"demo": True, "interval": max(args.poll_interval, 0.05),
                  "static_dir": args.static_dir, "state_file": None, "meta_file": None}
    else:
        state_file = args.state_file
        if not state_file and args.game_dir:
            state_file = find_state_file(args.game_dir)
            if not state_file:
                print("[warn] could not auto-find state.json under %s/bin/*/res_mods/..." % args.game_dir)
        if not state_file:
            print("ERROR: provide --state-file PATH, or --game-dir DIR, or use --demo")
            return 2
        meta_file = args.meta_file or os.path.join(os.path.dirname(state_file), "meta.json")
        source_desc = "state=%s" % state_file
        config = {"demo": False, "interval": max(args.poll_interval, 0.02),
                  "static_dir": args.static_dir, "state_file": state_file, "meta_file": meta_file}

    print("=" * 64)
    print(" 8111 for WoWS (aiohttp)  --  http://%s:%d/" % (args.host, args.port))
    print(" overlay:        http://%s:%d/overlay" % (args.host, args.port))
    print(" websocket:      ws://%s:%d/ws" % (args.host, args.port))
    print(" source:         %s" % source_desc)
    print("=" * 64)

    app = build_app(config)
    web.run_app(app, host=args.host, port=args.port, access_log=None, print=None)
    return 0


if __name__ == "__main__":
    sys.exit(main())
