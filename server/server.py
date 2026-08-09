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
import copy
import ipaddress
import json
import math
import os
import sys
import time
import uuid

import aiohttp
from aiohttp import web

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_STATIC = os.path.join(HERE, "static")

# --- v1 envelope ---------------------------------------------------------
# Every snapshot carries service identity, a monotonic cursor and per-domain
# availability on top of the original flat "schema: 1" body. Consumers written
# against the older flat shape keep working: nothing was renamed or removed.
SERVICE_ID = "8111-for-wows"
API_VERSION = "1.0"

STATUS_WAITING = "waiting"
STATUS_LIVE = "live"
STATUS_STALE = "stale"
STATUS_ENDED = "ended"

AVAIL_AVAILABLE = "available"
AVAIL_UNKNOWN = "unknown"
AVAIL_STALE = "stale"
AVAIL_UNSUPPORTED = "unsupported"

# Domains this service can produce. Ones that need game APIs the collector mod
# does not expose yet are declared unsupported instead of silently missing, so a
# consumer can tell "no data this frame" apart from "never available here".
SUPPORTED_DOMAINS = ("self", "objects", "roster", "damage", "ballistics", "map")
UNSUPPORTED_DOMAINS = ("kills", "capturePoints", "torpedoes", "consumables")

# Domains derived from the per-battle meta file; they do not go stale mid-battle
# the way the ~10 Hz state file does.
META_DOMAINS = ("roster", "map")

STALE_MIN_SECONDS = 2.0
STALE_POLL_FACTOR = 5.0

EMPTY_DAMAGE = {"inflicted": {}, "received": {}, "teamTotal": {}}


def stale_after(poll_interval):
    """How long live data may go unchanged before the frame counts as stale."""
    try:
        interval = float(poll_interval or 0.0)
    except (TypeError, ValueError):
        interval = 0.0
    return max(STALE_MIN_SECONDS, STALE_POLL_FACTOR * interval)

# Sibling module: the generated map-recognition table (tools/gen_maps.py).
if HERE not in sys.path:
    sys.path.insert(0, HERE)
import maps  # noqa: E402


# ===========================================================================
# service envelope contract (additive; legacy flat keys stay untouched)
# ===========================================================================
# apiVersion is "major.minor". Consumers reject an unknown major; a missing
# apiVersion means a pre-envelope (legacy) producer.
SERVICE_ID = "8111_for_wows"
API_VERSION = "1.0"
FILE_SCHEMA_VERSION = 1
HEALTH_ERROR_MAX_CHARS = 240

# Data domains this service can serve, and the schema version of each. A client
# uses this to decide which detectors it may arm; it is intentionally static.
CAPABILITIES = {
    "self": "1.0",
    "objects": "1.0",
    "roster": "1.0",
    "damage": "1.0",
    "ballistics": "1.0",
    "map": "1.0",
}

# Fixed empty-value contract: damage is always three tables, ballistics always
# carries an explicit availability flag. Keeps a client from guessing shapes.
EMPTY_DAMAGE = {"inflicted": {}, "received": {}, "teamTotal": {}}
EMPTY_BALLISTICS = {"available": False}


def _gen_instance_id():
    """A per-process id. Distinguishes restarts of this service to consumers."""
    return uuid.uuid4().hex[:12]


def _has_supported_file_schema(payload):
    return (isinstance(payload, dict)
            and type(payload.get("schema")) is int
            and payload["schema"] == FILE_SCHEMA_VERSION)


def _finite_number(value):
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False
    try:
        return math.isfinite(value)
    except (OverflowError, ValueError):
        return False


def _valid_json_tree(value, seen=None):
    """Reject non-JSON values, cycles, and non-finite numbers at any depth."""
    if value is None or isinstance(value, (str, bool)):
        return True
    if isinstance(value, (int, float)):
        return _finite_number(value)
    if not isinstance(value, (dict, list)):
        return False

    if seen is None:
        seen = set()
    marker = id(value)
    if marker in seen:
        return False
    seen.add(marker)
    try:
        if isinstance(value, dict):
            return (all(isinstance(key, str) for key in value)
                    and all(_valid_json_tree(item, seen)
                            for item in value.values()))
        return all(_valid_json_tree(item, seen) for item in value)
    finally:
        seen.remove(marker)


def _valid_optional_number(obj, key):
    return key not in obj or obj[key] is None or _finite_number(obj[key])


def _valid_optional_id(obj, key):
    if key not in obj or obj[key] is None:
        return True
    value = obj[key]
    if isinstance(value, str):
        return True
    return _finite_number(value)


def _valid_optional_position(obj, key):
    if key not in obj or obj[key] is None:
        return True
    value = obj[key]
    return (isinstance(value, (list, tuple))
            and len(value) >= 3
            and all(_finite_number(x) for x in value[:3]))


def valid_meta_payload(payload):
    """Whether a decoded meta.json object is safe for all server readers."""
    if not _has_supported_file_schema(payload) or not _valid_json_tree(payload):
        return False
    if "map" in payload and not isinstance(payload["map"], dict):
        return False
    if "roster" in payload:
        roster = payload["roster"]
        if not isinstance(roster, list) or not all(isinstance(x, dict) for x in roster):
            return False
        for row in roster:
            if (not _valid_optional_id(row, "playerId")
                    or not _valid_optional_number(row, "maxHealth")):
                return False
    if "extensions" in payload and not isinstance(payload["extensions"], dict):
        return False
    return True


def valid_state_payload(payload):
    """Whether a decoded state.json object is safe and has an explicit state."""
    if (not _has_supported_file_schema(payload)
            or type(payload.get("active")) is not bool
            or not _valid_json_tree(payload)):
        return False
    if "self" in payload and payload["self"] is not None \
            and not isinstance(payload["self"], dict):
        return False
    self_obj = payload.get("self")
    if isinstance(self_obj, dict):
        if (not _valid_optional_id(self_obj, "playerId")
                or not _valid_optional_position(self_obj, "position")
                or not _valid_optional_number(self_obj, "health")
                or not _valid_optional_number(self_obj, "maxHealth")):
            return False
    if "ships" in payload:
        ships = payload["ships"]
        if not isinstance(ships, list) or not all(isinstance(x, dict) for x in ships):
            return False
        for ship in ships:
            if (not _valid_optional_id(ship, "playerId")
                    or not _valid_optional_position(ship, "position")
                    or not _valid_optional_position(ship, "lastPosition")
                    or not _valid_optional_number(ship, "health")
                    or not _valid_optional_number(ship, "maxHealth")):
                return False
    for key in ("damage", "ballistics", "extensions"):
        if key in payload and not isinstance(payload[key], dict):
            return False
    damage = payload.get("damage")
    if isinstance(damage, dict):
        for key in ("inflicted", "received", "teamTotal"):
            if key in damage and not isinstance(damage[key], dict):
                return False
    if "diag" in payload and payload["diag"] is not None \
            and not isinstance(payload["diag"], dict):
        return False
    return True


def _source_time(value, fallback):
    """Use a finite source timestamp, clamped against future filesystem clocks."""
    try:
        fallback_time = float(fallback)
        if not math.isfinite(fallback_time):
            raise ValueError("non-finite fallback timestamp")
    except (OverflowError, TypeError, ValueError):
        fallback_time = time.time()
    if not _finite_number(value):
        return fallback_time
    return min(float(value), fallback_time)


def domain_availability(meta, state, status):
    """Per-frame availability of each capability domain.

    Values: "available" (data present this frame), "unknown" (domain absent),
    "stale" (whole snapshot is stale so live-derived data can't be trusted).
    """
    st = state or {}
    mt = meta or {}
    ballistics = st.get("ballistics")

    def av(present, live_derived=True):
        if status == "stale" and live_derived:
            return "stale"
        return "available" if present else "unknown"

    return {
        "self": av("self" in st and isinstance(st["self"], dict)),
        "objects": av("ships" in st and isinstance(st["ships"], list)),
        "roster": av(
            "roster" in mt and isinstance(mt["roster"], list),
            live_derived=False),
        "damage": av("damage" in st and isinstance(st["damage"], dict)),
        "ballistics": av(
            isinstance(ballistics, dict)
            and type(ballistics.get("available")) is bool
            and ballistics["available"]),
        "map": av(
            "map" in mt and isinstance(mt["map"], dict),
            live_derived=False),
    }


def _valid_extension(name, value):
    if not isinstance(name, str) or not isinstance(value, dict):
        return False
    parts = name.split(".")
    if (len(parts) < 2
            or any(not part for part in parts)
            or any(not all(char.isalnum() or char in "_-" for char in part)
                   for part in parts)):
        return False
    return (isinstance(value.get("schema"), str)
            and bool(value["schema"].strip())
            and "data" in value)


def _collect_extensions(meta, state):
    """Pass through namespaced extensions without dropping future metadata.

    Anything that is not an object carrying a "data" field is ignored: unknown
    extensions may be dropped but must never leak an unvetted shape downstream.
    """
    out = {}
    for src in (meta or {}), (state or {}):
        if not isinstance(src, dict):
            continue
        ext = src.get("extensions")
        if not isinstance(ext, dict):
            continue
        for name, val in ext.items():
            if _valid_extension(name, val):
                out[name] = dict(val)
    return out


# ===========================================================================
# shared state store
# ===========================================================================
class Store:
    """Owns the current snapshot plus the (instanceId, seq) cursor.

    `seq` is the monotonic content cursor: it advances only when the served
    payload changes (a valid data update, or a status flip such as live->stale).
    Repeat REST reads do NOT advance it, so any given (instanceId, seq) maps to
    exactly one payload -- byte-identical whether fetched via /all or /ws.
    """

    def __init__(self, instance_id=None, source_kind="file",
                 poll_interval=0.1, mode=None):
        self.version = 0                 # legacy /healthz counter (kept as-is)
        self.seq = 0                     # content cursor (see class docstring)
        self.instance_id = instance_id or _gen_instance_id()
        self.battle_id = None
        self.meta = {}
        self.state = {}
        self.last_state_update = None
        self.last_meta_update = None
        self.revision_at = None
        self.state_received = False
        self.status = "waiting"          # waiting | live | stale | ended
        self.source_kind = source_kind   # "file" | "demo"
        self.poll_interval = poll_interval
        self.mode = mode or ("demo" if "demo" in source_kind else "live")
        self.ws_clients = set()          # set[web.WebSocketResponse]
        self._battle_active = False       # was the last valid state an active battle?
        self._battle_seen = False
        self._battle_counter = 0
        self._cache_seq = -1
        self._cache_obj = None
        self._cache_json = None

    def _touch(self, now):
        self.revision_at = now
        self.version += 1
        self.seq += 1
        self._cache_seq = -1             # invalidate the snapshot cache

    def apply(self, now, meta=None, state=None, *,
              meta_updated_at=None, state_updated_at=None):
        """Validate and record file-bridge data; return whether any input won."""
        if meta is None and state is None:
            return False
        if meta is not None and not valid_meta_payload(meta):
            return False
        if state is not None and not valid_state_payload(state):
            return False

        accept_meta = meta is not None
        accept_state = state is not None
        # Compute every fallible derived value before mutating the store. An
        # invalid/overflowing source clock must not leave a half-applied frame.
        meta_source_time = (
            _source_time(meta_updated_at, now) if accept_meta else None)
        state_source_time = (
            _source_time(state_updated_at, now) if accept_state else None)

        if accept_meta:
            self.meta = meta
            self.last_meta_update = meta_source_time
        if accept_state:
            self.state = state
            self.last_state_update = state_source_time
            self.state_received = True
            self._update_battle_and_status()
        self._touch(now)
        return True

    def _update_battle_and_status(self):
        """Resolve battleId and derive live/ended/waiting from real game state.

        A disconnect or bad read never reaches here (the caller keeps the last
        good snapshot), so this can only emit `ended` on a genuine active->
        inactive transition -- staleness is handled separately.
        """
        st = self.state or {}
        active = st["active"]
        mod_id = st.get("battleId")
        if mod_id is None:
            mod_id = (self.meta or {}).get("battleId")
        if mod_id is not None:
            self.battle_id = str(mod_id)
        if active:
            self._battle_seen = True
            if not self._battle_active:
                # A new battle began this frame.
                if mod_id is None:
                    self._battle_counter += 1
                    self.battle_id = "%s-b%d" % (self.instance_id, self._battle_counter)
            self._battle_active = True
            self.status = "live"
        else:
            # `waiting` means no valid state has ever arrived. An explicit
            # inactive frame is a terminal state even when it is the first read.
            self.status = "ended"
            self._battle_active = False

    def tick_staleness(self, now, poll_interval):
        """Flip live->stale when updates stop arriving. Returns True if changed.

        Never fabricates `ended`: a silent producer is `stale`, not a battle end.
        """
        if self.status != "live":
            return False
        threshold = max(2.0, 5.0 * poll_interval)
        if (self.last_state_update is not None
                and (now - self.last_state_update) > threshold):
            self.status = "stale"
            self._touch(now)
            return True
        return False

    def snapshot(self):
        """Return (obj, json_str) for the current cursor, cached per seq."""
        if self._cache_seq == self.seq and self._cache_obj is not None:
            return copy.deepcopy(self._cache_obj), self._cache_json
        obj = build_snapshot(self)
        js = json.dumps(obj, allow_nan=False)
        self._cache_obj = obj
        self._cache_json = js
        self._cache_seq = self.seq
        return copy.deepcopy(obj), js

    @property
    def source_status(self):
        """Compatibility name for consumers of the earlier Store contract."""
        return self.status

    @property
    def last_update(self):
        """The source frame time, not the time of a derived status change."""
        return (round(self.last_state_update, 3)
                if self.last_state_update is not None else None)

    @property
    def last_active_update(self):
        return self.last_state_update

    def set(self, meta=None, state=None):
        """Compatibility ingest path; production file ingestion uses apply()."""
        now = time.time()
        if state == {}:
            if meta is not None and valid_meta_payload(meta):
                self.meta = meta
                self.last_meta_update = now
            self.state = {}
            self.status = "waiting"
            self._touch(now)
            return

        battle_seen_before = self._battle_seen
        if not self.apply(now, meta=meta, state=state):
            return
        # The earlier in-process API treated an initial inactive frame as
        # waiting. The file bridge keeps the stricter v1 behavior in apply().
        if state is not None and not state.get("active") and not battle_seen_before:
            self.status = "waiting"
            self._cache_seq = -1

    def derive_status(self, now=None):
        """waiting -> live -> stale -> ended, from data age only.

        A torn file, a parse failure or a dead collector must never look like a
        finished battle, so `ended` requires the collector to actually say the
        battle is no longer active.
        """
        now = time.time() if now is None else now
        state = self.state or {}
        if not state:
            return STATUS_WAITING
        if state.get("active"):
            last = self.last_state_update if self.last_state_update is not None else now
            if (now - last) > stale_after(self.poll_interval):
                return STATUS_STALE
            return STATUS_LIVE
        return STATUS_ENDED if self._battle_seen else STATUS_WAITING

    def refresh_status(self, now=None):
        """Re-evaluate staleness. Returns True when the status (and cursor) moved.

        This advances `seq` without touching `last_update`: the frame content is
        genuinely different (a new status), but the underlying data really is
        that old, so `/healthz.ageSeconds` must keep climbing.
        """
        status = self.derive_status(now)
        if status == self.status:
            return False
        self.status = status
        self.version += 1
        self.seq += 1
        self._cache_seq = -1
        return True


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
        if not _finite_number(nx) or not _finite_number(ny):
            return None
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


def _finite_ratio(numerator, denominator):
    if not _finite_number(numerator) or not _finite_number(denominator) \
            or denominator == 0:
        return None
    try:
        ratio = numerator / denominator
    except (ArithmeticError, TypeError, ValueError):
        return None
    return ratio if _finite_number(ratio) else None


def _nonneg_number(value):
    """True when value is a real number (not bool)."""
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def is_ship_dead(ship):
    """Dead ships must not keep last-seen ghost markers on the overlay.

    `alive` may arrive as False or 0 depending on the collector/build, and a
    ship that despawned after the killing blow may only show health/lastHealth
    at zero.
    """
    if not isinstance(ship, dict):
        return False
    alive = ship.get("alive")
    if alive is False or alive == 0:
        return True
    for key in ("health", "lastHealth"):
        value = ship.get(key)
        if _nonneg_number(value) and value <= 0:
            return True
    return False


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
        # Prefer collector minimap nx/ny when present. World positions for
        # spotted-only ships may come from an unstable map->world fit and can
        # jump when that fit flips axes; mapPosition itself stays correct.
        norm = normalize_direct(s.get("nx", s.get("mapX")),
                                s.get("ny", s.get("mapY")))
        if norm is None:
            norm = normalize(pos, bounds)
        mh = s.get("maxHealth") or meta_r.get("maxHealth")
        hp = s.get("health")
        team = s.get("teamId")
        if team is None:
            team = meta_r.get("teamId")
        rel = s.get("relation")
        if rel not in (0, 1, 2):
            rel = meta_r.get("relation")
        dead = is_ship_dead(s)
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
            "alive": False if dead else s.get("alive"),
            "visible": False if dead else s.get("visible", pos is not None or norm is not None),
            "x": pos[0] if pos else None,
            "z": pos[2] if pos else None,
            "yaw": s.get("yaw"),
            "health": hp,
            "maxHealth": mh,
            "hpRatio": _finite_ratio(hp, mh),
        }
        if not dead and norm:
            obj["nx"], obj["ny"] = norm[0], norm[1]

        # last-known ("ghost") position for ships that lit up then went dark.
        # Dead ships must not keep these markers -- overlay treats them as 灭点.
        if not dead:
            last_pos = s.get("lastPosition")
            if last_pos:
                obj["lastX"], obj["lastZ"] = last_pos[0], last_pos[2]
            lnorm = normalize_direct(s.get("lastNx", s.get("lastMapX")),
                                     s.get("lastNy", s.get("lastMapY")))
            if lnorm is None and last_pos:
                lnorm = normalize(last_pos, bounds)
            if lnorm:
                obj["lastNx"], obj["lastNy"] = lnorm[0], lnorm[1]
                if s.get("lastYaw") is not None:
                    obj["lastYaw"] = s.get("lastYaw")
                if s.get("lastHealth") is not None:
                    obj["lastHealth"] = s.get("lastHealth")
                obj["lastSeenTs"] = s.get("lastSeenTs")
                obj["staleSeconds"] = s.get("staleSeconds")
            elif last_pos is not None:
                if s.get("lastYaw") is not None:
                    obj["lastYaw"] = s.get("lastYaw")
                if s.get("lastHealth") is not None:
                    obj["lastHealth"] = s.get("lastHealth")
                obj["lastSeenTs"] = s.get("lastSeenTs")
                obj["staleSeconds"] = s.get("staleSeconds")

        objects.append(obj)
    return objects, bounds


def normalize_damage(damage):
    """Always three tables, so a consumer never has to branch on missing keys."""
    out = dict(EMPTY_DAMAGE)
    if isinstance(damage, dict):
        for key in EMPTY_DAMAGE:
            value = damage.get(key)
            out[key] = value if isinstance(value, dict) else {}
        for key, value in damage.items():
            if key not in out:
                out[key] = value
    return out


def normalize_ballistics(ballistics):
    if not isinstance(ballistics, dict) or not ballistics:
        return {"available": False}
    out = dict(ballistics)
    out["available"] = bool(out.get("available"))
    return out


def build_all(meta, state):
    """The original flat snapshot body (`schema: 1`), with empty values pinned.

    Absent data has one fixed shape per domain -- `self` is null, lists are
    empty, damage is three empty tables, ballistics is `{"available": false}` --
    so consumers can diff frames without treating "missing" as a value change.
    """
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
        "self": (state or {}).get("self") or None,
        "objects": objects,
        "roster": (meta or {}).get("roster") or [],
        "damage": normalize_damage((state or {}).get("damage")),
        "ballistics": normalize_ballistics((state or {}).get("ballistics")),
        "diag": (state or {}).get("diag"),
    }


def _normalized_damage(value):
    damage = value if isinstance(value, dict) else {}
    return {
        key: table if isinstance(table := damage.get(key), dict) else {}
        for key in ("inflicted", "received", "teamTotal")
    }


def _normalized_ballistics(value):
    ballistics = dict(value) if isinstance(value, dict) else {}
    available = ballistics.get("available")
    ballistics["available"] = (
        available if type(available) is bool else False)
    return ballistics


def build_capabilities(extensions=None):
    """Return supported schema versions plus explicit unsupported domains."""
    capabilities = dict(CAPABILITIES)
    for name in UNSUPPORTED_DOMAINS:
        capabilities.setdefault(name, None)
    for name, value in (extensions or {}).items():
        capabilities[name] = value["schema"]
    return capabilities


def build_snapshot(store):
    """The full served payload: legacy /all body + the additive envelope.

    Legacy flat keys and object field names from build_all() are preserved; the
    envelope (serviceId/apiVersion/instanceId/seq/battleId/source/capabilities/
    availability/extensions) and the fixed empty-value contract are layered on.
    """
    body = build_all(store.meta, store.state)
    st = store.state or {}

    # Fixed empty-value contract: arrays default [], damage is three tables,
    # ballistics always states availability.
    body["objects"] = body.get("objects") or []
    body["roster"] = body.get("roster") or []
    body["damage"] = _normalized_damage(st.get("damage"))
    body["ballistics"] = _normalized_ballistics(st.get("ballistics"))

    extensions = _collect_extensions(store.meta, st)
    capabilities = build_capabilities(extensions)
    availability = domain_availability(store.meta, st, store.status)
    for name in UNSUPPORTED_DOMAINS:
        availability.setdefault(name, AVAIL_UNSUPPORTED)
    for name, value in extensions.items():
        if value.get("available") is True:
            availability[name] = (
                "stale" if store.status == "stale" else "available")
        else:
            availability[name] = "unknown"

    body.update({
        "serviceId": SERVICE_ID,
        "apiVersion": API_VERSION,
        "instanceId": store.instance_id,
        "seq": store.seq,
        "battleId": store.battle_id,
        "source": {
            "kind": store.source_kind,
            "mode": store.mode,
            "status": store.status,
            "updatedAt": (round(store.last_state_update, 3)
                          if store.last_state_update is not None else None),
        },
        "capabilities": capabilities,
        "availability": availability,
        "extensions": extensions,
    })
    return body


# ===========================================================================
# background producers
# ===========================================================================
def read_json(path):
    try:
        with open(path, "rb") as f:
            return json.loads(f.read().decode("utf-8"))
    except Exception:
        return None


class IngestionHealth:
    """Small observable record for watcher progress and bounded failures."""

    def __init__(self):
        self.iteration = 0
        self.rejected_revision = None
        self.error = None

    def begin_iteration(self):
        self.iteration += 1

    def reject(self, source, reason, signature=None):
        revision = {
            "iteration": self.iteration,
            "source": str(source),
            "reason": _bounded_health_error(reason),
        }
        if signature is not None:
            revision["signature"] = {
                "mtime_ns": signature[0],
                "size": signature[1],
                "inode": signature[2],
            }
        self.rejected_revision = revision

    def record_error(self, error):
        if isinstance(error, BaseException):
            message = "%s: %s" % (type(error).__name__, error)
        else:
            message = str(error)
        self.error = _bounded_health_error(message)


STORE_KEY = web.AppKey("store", Store)
CONFIG_KEY = web.AppKey("config", dict)
STATIC_DIR_KEY = web.AppKey("static_dir", str)
BACKGROUND_TASK_KEY = web.AppKey("background_task", asyncio.Task)
INGESTION_HEALTH_KEY = web.AppKey("ingestion_health", IngestionHealth)


def _ingestion_health(app):
    health = app.get(INGESTION_HEALTH_KEY)
    if health is None:
        health = IngestionHealth()
        app[INGESTION_HEALTH_KEY] = health
    return health


def _file_signature(stat_result):
    return (stat_result.st_mtime_ns, stat_result.st_size, stat_result.st_ino)


def _signature_time(signature):
    return signature[0] / 1_000_000_000.0


async def broadcast(app):
    store = app[STORE_KEY]
    if not store.ws_clients:
        return
    _, payload = store.snapshot()
    dead = []
    for ws in list(store.ws_clients):
        try:
            await ws.send_str(payload)
        except Exception:
            dead.append(ws)
    for ws in dead:
        store.ws_clients.discard(ws)


async def file_watcher(app, state_file, meta_file, interval):
    store = app[STORE_KEY]
    health = _ingestion_health(app)
    paths = {"meta": meta_file, "state": state_file}
    validators = {"meta": valid_meta_payload, "state": valid_state_payload}
    acknowledged = {"meta": None, "state": None}
    retry_sources = set()

    while True:
        health.begin_iteration()
        changed = False
        try:
            before = {}
            for source, path in paths.items():
                try:
                    stat_result = await asyncio.to_thread(os.stat, path)
                    before[source] = _file_signature(stat_result)
                except OSError:
                    before[source] = None

            candidates = [
                source for source in paths
                if source in retry_sources
                or before[source] != acknowledged[source]
            ]
            payloads = {}
            rejection = None
            unreadable = []

            for source in candidates:
                signature = before[source]
                if signature is None:
                    rejection = (source, "missing", signature)
                    break
                data = await asyncio.to_thread(read_json, paths[source])
                try:
                    after_read = _file_signature(
                        await asyncio.to_thread(os.stat, paths[source]))
                except OSError:
                    after_read = None
                if after_read != signature:
                    rejection = (source, "changed_during_read", signature)
                    break
                if data is None:
                    # A half-written JSON file must be retried, but it must not
                    # block an independently valid update from the other file.
                    unreadable.append((source, "invalid_payload", signature))
                    continue
                if not validators[source](data):
                    rejection = (source, "invalid_payload", signature)
                    break
                payloads[source] = data

            # Re-stat both files after the whole batch. This catches state
            # changing while meta is read (and vice versa), preventing a mixed
            # frame even when only one side looked new at iteration start.
            if candidates and rejection is None:
                for source, path in paths.items():
                    try:
                        final_signature = _file_signature(
                            await asyncio.to_thread(os.stat, path))
                    except OSError:
                        final_signature = None
                    if final_signature != before[source]:
                        rejection = (
                            source, "changed_during_read", before[source])
                        break

            if rejection is not None:
                source, reason, signature = rejection
                retry_sources.update(candidates)
                retry_sources.add(source)
                health.reject(source, reason, signature)
            elif payloads:
                now = time.time()
                committed = store.apply(
                    now,
                    meta=payloads.get("meta"),
                    state=payloads.get("state"),
                    meta_updated_at=(
                        _signature_time(before["meta"])
                        if "meta" in payloads else None),
                    state_updated_at=(
                        _signature_time(before["state"])
                        if "state" in payloads else None),
                )
                if committed:
                    for source in payloads:
                        acknowledged[source] = before[source]
                    retry_sources.difference_update(payloads)
                    changed = True
                else:
                    retry_sources.update(candidates)
                    health.reject(candidates[0], "transaction_rejected",
                                  before[candidates[0]])
                for source, reason, signature in unreadable:
                    retry_sources.add(source)
                    health.reject(source, reason, signature)
            elif unreadable:
                for source, reason, signature in unreadable:
                    retry_sources.add(source)
                    health.reject(source, reason, signature)

            now = time.time()
            # Run this in the same iteration as ingestion so an old active file
            # is never published as live during process startup.
            if store.tick_staleness(now, interval):
                changed = True
            if changed:
                await broadcast(app)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            health.record_error(error)
            raise
        await asyncio.sleep(interval)


async def status_ticker(app, interval):
    """Age live data into `stale` even while nothing new arrives on disk.

    A collector that stops writing produces no file event, so without this the
    last good frame would keep claiming to be live forever.
    """
    store = app["store"]
    while True:
        await asyncio.sleep(interval)
        if store.refresh_status():
            await broadcast(app)


async def demo_generator(app, interval):
    store = app[STORE_KEY]
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
    store.apply(time.time(), meta={
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
        store.apply(time.time(), state={
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
    allowed_origins = request.app[CONFIG_KEY].get("allowed_origins", set())
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
                request.app[CONFIG_KEY].get("allowed_origins", set())):
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
    path = os.path.join(request.app[STATIC_DIR_KEY], "overlay.html")
    if not os.path.isfile(path):
        return jr({"error": "overlay.html not found", "path": path}, 404)
    return web.FileResponse(path)


def _bounded_health_error(message):
    try:
        text = str(message)
    except BaseException:
        text = "unprintable health error"
    text = "".join(char if char.isprintable() else " " for char in text)
    return text[:HEALTH_ERROR_MAX_CHARS]


def _record_background_failure(app, message):
    error = _bounded_health_error(message)
    _ingestion_health(app).record_error(error)
    return False, error


def background_task_health(app):
    """Return health and a bounded diagnostic for the producer task."""
    task = app.get(BACKGROUND_TASK_KEY)
    if task is None:
        return _record_background_failure(app, "background task not started")
    try:
        if not task.done():
            return True, None
        if task.cancelled():
            return _record_background_failure(
                app, "background task cancelled")
        exc = task.exception()
    except asyncio.CancelledError:
        return _record_background_failure(app, "background task cancelled")
    except BaseException as error:
        return _record_background_failure(
            app, "%s: %s" % (type(error).__name__, error))
    if exc is None:
        return _record_background_failure(app, "background task exited")
    return _record_background_failure(
        app, "%s: %s" % (type(exc).__name__, exc))


async def h_healthz(request):
    store = request.app[STORE_KEY]
    ok, background_error = background_task_health(request.app)
    body = {
        "ok": ok,
        "backgroundError": background_error,
        "serviceId": SERVICE_ID,
        "apiVersion": API_VERSION,
        "instanceId": store.instance_id,
        "seq": store.seq,
        "battleId": store.battle_id,
        "status": store.status,
        "battleActive": (store.state or {}).get("active", False),
        "ageSeconds": (round(max(0.0, time.time() - store.last_state_update), 2)
                       if store.last_state_update is not None else None),
        "version": store.version,
        "wsClients": len(store.ws_clients),
        "hasMeta": bool(store.meta),
        "hasState": bool(store.state),
    }
    return jr(body, status=200 if ok else 503)


async def h_all(request):
    # Serve the exact cached bytes for the current cursor so /all and /ws are
    # byte-identical for a given (instanceId, seq).
    _, js = request.app[STORE_KEY].snapshot()
    return web.Response(text=js, content_type="application/json")


async def h_map_obj(request):
    s = request.app[STORE_KEY]
    objects, _ = build_map_objects(s.meta, s.state)
    return jr(objects)


async def h_map_info(request):
    s = request.app[STORE_KEY]
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
    s = request.app[STORE_KEY]
    return jr((s.state or {}).get("self") or {})


async def h_indicators(request):
    self_ = (request.app[STORE_KEY].state or {}).get("self") or {}
    return jr({
        "yaw": self_.get("yaw"), "speed": self_.get("speed"),
        "health": self_.get("health"), "maxHealth": self_.get("maxHealth"),
        "position": self_.get("position"), "isObserver": self_.get("isObserver"),
    })


async def h_roster(request):
    return jr((request.app[STORE_KEY].meta or {}).get("roster", []))


async def h_damage(request):
    return jr((request.app[STORE_KEY].state or {}).get("damage", {}))


async def h_ballistics(request):
    return jr(_normalized_ballistics(
        (request.app[STORE_KEY].state or {}).get("ballistics")))


async def h_debug(request):
    s = request.app[STORE_KEY]
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
            origin, request.app[CONFIG_KEY].get("allowed_origins", set())):
        raise web.HTTPForbidden(text="WebSocket Origin not allowed")
    store = request.app[STORE_KEY]
    ws = web.WebSocketResponse(heartbeat=20.0)
    await ws.prepare(request)
    store.ws_clients.add(ws)
    try:
        _, payload = store.snapshot()
        await ws.send_str(payload)
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
    app[STORE_KEY] = Store(
        source_kind="demo" if config.get("demo") else "file")
    app[INGESTION_HEALTH_KEY] = IngestionHealth()
    app[CONFIG_KEY] = config
    app[STATIC_DIR_KEY] = config["static_dir"]

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
        cfg = app[CONFIG_KEY]
        if cfg["demo"]:
            app[BACKGROUND_TASK_KEY] = asyncio.create_task(
                demo_generator(app, cfg["interval"]))
        else:
            app[BACKGROUND_TASK_KEY] = asyncio.create_task(
                file_watcher(app, cfg["state_file"], cfg["meta_file"], cfg["interval"]))

    async def on_cleanup(app):
        health = _ingestion_health(app)
        errors = []
        cleanup_cancel = None

        def externally_cancelled():
            current = asyncio.current_task()
            return current is not None and current.cancelling() > 0

        def remember(error):
            if isinstance(error, BaseException):
                message = "%s: %s" % (type(error).__name__, error)
            else:
                message = str(error)
            message = _bounded_health_error(message)
            if message and message not in errors:
                errors.append(message)

        def consume_when_done(done_task):
            try:
                if not done_task.cancelled():
                    error = done_task.exception()
                    if error is not None:
                        health.record_error(error)
            except asyncio.CancelledError:
                pass
            except BaseException as error:
                health.record_error(error)

        task = app.get(BACKGROUND_TASK_KEY)
        if task is not None:
            try:
                done = task.done()
            except BaseException as error:
                remember(error)
                done = False

            cancel_failed = False
            if not done:
                try:
                    task.cancel()
                except BaseException as error:
                    remember(error)
                    cancel_failed = True

            if not cancel_failed or done:
                try:
                    await task
                except asyncio.CancelledError as error:
                    if externally_cancelled():
                        cleanup_cancel = error
                except BaseException as error:
                    remember(error)
            else:
                # A non-standard task may reject cancellation. Never block
                # cleanup on it; arrange to consume its eventual exception.
                try:
                    if task.done():
                        error = task.exception()
                        if error is not None:
                            remember(error)
                    else:
                        task.add_done_callback(consume_when_done)
                except asyncio.CancelledError:
                    pass
                except BaseException as error:
                    remember(error)

            # Calling exception() on a completed task marks the exception as
            # retrieved even if awaiting/cancelling followed an unusual path.
            try:
                if task.done() and not task.cancelled():
                    error = task.exception()
                    if error is not None:
                        remember(error)
            except asyncio.CancelledError:
                pass
            except BaseException as error:
                remember(error)

        for ws in list(app[STORE_KEY].ws_clients):
            try:
                await ws.close()
            except asyncio.CancelledError as error:
                if externally_cancelled():
                    cleanup_cancel = cleanup_cancel or error
                else:
                    remember(error)
            except BaseException as error:
                remember(error)
            else:
                app[STORE_KEY].ws_clients.discard(ws)

        if errors:
            health.record_error("; ".join(errors))
        if cleanup_cancel is not None:
            raise cleanup_cancel

    app.on_startup.append(on_startup)
    # aiohttp installs its cleanup-context signal first. Keep the service
    # cleanup first as well so direct embedders/tests invoking handler 0 get the
    # same resource shutdown behavior as Application.cleanup().
    app.on_cleanup.insert(0, on_cleanup)
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


class ConfigurationError(ValueError):
    pass


def resolve_runtime_config(args, repo_root=None):
    """Resolve CLI/config-file precedence without starting the HTTP server."""
    if repo_root is None:
        repo_root = os.path.dirname(HERE)

    # Load config.ini (repo root by default). Precedence: CLI flag > config.ini > built-in default.
    config_path = args.config or os.path.join(repo_root, "config.ini")
    file_cfg = load_config_file(config_path)
    cfg_loaded = bool(file_cfg)
    config_warnings = []

    def pick(cli_val, key, default, cast=str):
        if cli_val is not None:
            return cli_val
        raw = file_cfg.get(key)
        if raw is None or raw == "":
            return default
        try:
            return cast(raw)
        except (TypeError, ValueError):
            config_warnings.append(
                "config.ini: bad value for %s=%r, using %r"
                % (key, raw, default))
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
        raise ConfigurationError(
            "refusing non-loopback host %r; use --allow-remote or set "
            "allow_remote=true" % host)

    if args.demo:
        source_desc = "DEMO (synthetic data)"
        config = {"demo": True, "interval": max(poll_interval, 0.05),
                  "static_dir": static_dir, "state_file": None, "meta_file": None,
                  "allowed_origins": allowed_origins}
    else:
        if not state_file and game_dir:
            state_file = find_state_file(game_dir)
            if not state_file:
                config_warnings.append(
                    "could not auto-find state.json under "
                    "%s/bin/*/res_mods/..." % game_dir)
        if not state_file:
            raise ConfigurationError(
                "set game_dir in %s, or pass --game-dir/--state-file, "
                "or use --demo" % config_path)
        if not meta_file:
            meta_file = os.path.join(os.path.dirname(state_file), "meta.json")
        source_desc = "state=%s" % state_file
        config = {"demo": False, "interval": max(poll_interval, 0.02),
                  "static_dir": static_dir, "state_file": state_file, "meta_file": meta_file,
                  "allowed_origins": allowed_origins}

    return {
        "app_config": config,
        "host": host,
        "port": port,
        "config_path": config_path,
        "config_loaded": cfg_loaded,
        "source_desc": source_desc,
        "allowed_origins": allowed_origins,
        "warnings": tuple(config_warnings),
    }


def main(argv=None):
    args = parse_args(argv if argv is not None else sys.argv[1:])
    try:
        resolved = resolve_runtime_config(args)
    except ConfigurationError as error:
        print("ERROR: %s" % error)
        return 2

    for warning in resolved["warnings"]:
        print("[warn] %s" % warning)

    host = resolved["host"]
    port = resolved["port"]
    config_path = resolved["config_path"]
    cfg_loaded = resolved["config_loaded"]
    source_desc = resolved["source_desc"]
    allowed_origins = resolved["allowed_origins"]
    config = resolved["app_config"]

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
