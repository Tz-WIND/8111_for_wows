# MIT License
# Copyright (c) 2026 Tz-WIND/Lex_q
#
# SPDX-License-Identifier: MIT

API_VERSION = 'API_v1.0'
MOD_NAME = 'WowsExtractor'
MOD_VERSION = '0.1.0'

# ---------------------------------------------------------------------------
# WowsExtractor -- in-game data collector for the "8111 for WoWS" project.
#
# It collects *legal* (player-visible) battle data through the official
# ModsAPI and writes it to JSON files on disk. A separate local process
# (server/server.py) reads those files and exposes them over HTTP + WebSocket,
# similar to War Thunder's localhost:8111 telemetry.
#
#   meta.json   -- written once per battle: roster, map, consumable ranges.
#   state.json  -- written ~10x/sec: live ship positions, hp, damage, etc.
#
# This mod creates NO UI elements; it only reads game state and writes files.
# Everything is wrapped defensively: a missing/renamed API on a given client
# build degrades gracefully (the field is omitted) instead of crashing.
# ---------------------------------------------------------------------------

try:
    import events, ui, utils, dataHub, constants, battle, callbacks
except:
    pass

# `os` may not be directly importable inside the sandbox -- borrow it from a
# stdlib module that already imported it (same trick as ModsInstaller).
try:
    import os
except:
    try:
        import xml.sax.saxutils
        os = xml.sax.saxutils.os
    except:
        os = None

# The standard `json` module is *much* faster than our pure-python fallback.
# It may be blocked by the sandbox importer (like `os`), so this is best-effort.
try:
    import json as _json_module
except:
    _json_module = None

try:
    from .battle_identity import BattleIdentity, create_session_nonce
except:
    from battle_identity import BattleIdentity, create_session_nonce

try:
    from .emit_guard import should_write_state
except:
    from emit_guard import should_write_state

# One nonce per game/mod load prevents fallback IDs repeating after a restart.
SESSION_NONCE = create_session_nonce()


def _get_threading():
    """Best-effort handle to the `threading` module.

    The collector's only heavy per-frame cost (JSON encoding + disk I/O) is moved
    off the game thread onto a background writer when threading is available. The
    sandbox may block `import threading`; if so we try to borrow an already-loaded
    copy (same idea as the `os` trick) and otherwise fall back to synchronous
    writes -- correctness is identical either way, only smoothness differs.
    """
    try:
        import threading as _t
        if hasattr(_t, 'Thread'):
            return _t
    except:
        pass
    try:
        import sys as _sys
        for _m in list(_sys.modules.values()):
            try:
                _t = getattr(_m, 'threading', None)
                if _t is not None and hasattr(_t, 'Thread') and hasattr(_t, 'Lock'):
                    return _t
            except:
                pass
    except:
        pass
    return None


_threading = _get_threading()

# sentinel used to memoize values that may legitimately be None
_UNSET = object()

try:
    CC = constants.UiComponents
except:
    CC = None

STATE_INTERVAL = 0.1          # default seconds between state.json writes (~10 Hz); see config.ini
LAST_SEEN_TTL = 60.0          # default: keep reporting a dark ship's last-known spot this long; see config.ini
META_RETRY_INTERVAL = 0.5     # battleInfo/map can be late in very small battles
META_RETRY_SECONDS = 12.0
SCHEMA_VERSION = 1
# Version of the JSON contract written to disk. Distinct from API_VERSION above,
# which declares the ModsAPI level to the PnFMods loader.
WIRE_API_VERSION = '1.0'
INVALID = -1


# ===========================================================================
# small utilities
# ===========================================================================
def logInfo(*args):
    try:
        utils.logInfo('[{}] {}'.format(MOD_NAME, ' '.join([_str(a) for a in args])))
    except:
        pass


def logError(*args):
    try:
        utils.logError('[{}] {}'.format(MOD_NAME, ' '.join([_str(a) for a in args])))
    except:
        pass


def _str(v):
    try:
        return str(v)
    except:
        try:
            return repr(v)
        except:
            return '?'


def _try(fn, default=None):
    """Call a zero-arg callable, swallowing any exception."""
    try:
        return fn()
    except:
        return default


def _get(obj, name, default=None):
    """getattr() that swallows exceptions -- like `_try(lambda: obj.name)` but
    without allocating a lambda on every call (this runs in the hot ship loop)."""
    try:
        return getattr(obj, name)
    except:
        return default


# ---------------------------------------------------------------------------
# config file (config.ini next to this mod). Uses a trivial `key = value` format
# parsed with plain string ops, so it works even if json/configparser are blocked
# by the sandbox. A missing/garbled file just means built-in defaults.
# ---------------------------------------------------------------------------
def _parse_kv(text):
    cfg = {}
    try:
        text = text.replace('\r\n', '\n').replace('\r', '\n')
        for line in text.split('\n'):
            line = line.strip()
            if not line or line[0] in ('#', ';') or '=' not in line:
                continue
            i = line.find('=')
            key = line[:i].strip().lower()
            val = line[i + 1:].strip()
            if key:
                cfg[key] = val
    except:
        pass
    return cfg


def _cfg_float(cfg, key, default):
    try:
        v = cfg.get(key)
        if v is None or v == '':
            return default
        return float(v)
    except:
        return default


def _load_mod_config():
    base = _try(lambda: utils.getModDir())
    if not base:
        return {}
    try:
        f = open(base + '/config.ini', 'rb')
        try:
            raw = f.read()
        finally:
            f.close()
        return _parse_kv(raw)
    except:
        return {}


def _now():
    return _try(lambda: utils.getTimeFromGameStart(), 0.0) or 0.0


# ---------------------------------------------------------------------------
# minimal JSON encoder (does not depend on the `json` module being importable)
# ---------------------------------------------------------------------------
try:
    _STRING_TYPES = (str, unicode)
    _INT_TYPES = (int, long)
except NameError:  # pragma: no cover - python3 safety, not used in game
    _STRING_TYPES = (str,)
    _INT_TYPES = (int,)

_ESCAPE = {
    '"': '\\"', '\\': '\\\\', '\b': '\\b', '\f': '\\f',
    '\n': '\\n', '\r': '\\r', '\t': '\\t',
}


def _json_str(s):
    try:
        if isinstance(s, unicode):
            s = s.encode('utf-8')
    except NameError:
        pass
    # Fast path: the vast majority of our strings (keys, names, enum values) need
    # no escaping. `min(s)`, `in` are C-level scans -- far cheaper than iterating
    # every character in pure Python, which is what dominated the old hot path.
    try:
        if not s or (min(s) >= ' ' and '"' not in s and '\\' not in s):
            return '"' + s + '"'
    except:
        pass
    out = ['"']
    for ch in s:
        if ch in _ESCAPE:
            out.append(_ESCAPE[ch])
        elif ord(ch) < 0x20:
            out.append('\\u%04x' % ord(ch))
        else:
            out.append(ch)
    out.append('"')
    return ''.join(out)


def _json(obj):
    if obj is None:
        return 'null'
    if obj is True:
        return 'true'
    if obj is False:
        return 'false'
    if isinstance(obj, _INT_TYPES):
        return str(obj)
    if isinstance(obj, float):
        if obj != obj or obj in (float('inf'), float('-inf')):  # NaN/inf -> null
            return 'null'
        return repr(obj)
    if isinstance(obj, _STRING_TYPES):
        return _json_str(obj)
    if isinstance(obj, dict):
        parts = []
        for k, v in obj.items():
            try:
                key = k if isinstance(k, _STRING_TYPES) else str(k)
            except:
                key = '?'
            parts.append(_json_str(key) + ':' + _json(v))
        return '{' + ','.join(parts) + '}'
    if isinstance(obj, (list, tuple)):
        return '[' + ','.join([_json(v) for v in obj]) + ']'
    # last resort
    return _json_str(_str(obj))


def json_dumps(obj):
    # Prefer the engine helper if present, then stdlib json, then our encoder.
    enc = getattr(utils, 'jsonEncode', None)
    if enc is not None:
        try:
            return enc(obj)
        except:
            pass
    if _json_module is not None:
        try:
            return _json_module.dumps(obj)
        except:
            pass
    return _json(obj)


def _serialize_safe(obj):
    """Thread-safe serializer for the background writer.

    Deliberately avoids `utils.jsonEncode` -- engine helpers are only safe to call
    on the game thread. Plain `json` / our pure-python encoder are fine off-thread,
    and since this no longer runs on the render thread its speed barely matters."""
    if _json_module is not None:
        try:
            return _json_module.dumps(obj)
        except:
            pass
    return _json(obj)


def _atomic_write(path, text):
    """Best-effort atomic write. Falls back to a plain write if os is missing.
    The server tolerates partial reads anyway (keeps last good snapshot)."""
    try:
        data = text
        try:
            if isinstance(text, unicode):
                data = text.encode('utf-8')
        except NameError:
            pass
        if os is None:
            f = open(path, 'wb')
            try:
                f.write(data)
            finally:
                f.close()
            return True
        tmp = path + '.tmp'
        f = open(tmp, 'wb')
        try:
            f.write(data)
        finally:
            f.close()
        try:
            if os.path.exists(path):
                os.remove(path)
        except:
            pass
        os.rename(tmp, path)
        return True
    except Exception, e:
        logError('write failed for {}: {}'.format(path, _str(e)))
        return False


def _write_file(path, text):
    """Plain in-place overwrite (no temp+rename churn).

    Used for the high-frequency state.json. A reader that catches a half-written
    file just parses to None and keeps its last good snapshot (the server does
    exactly this), and the next write ~0.1s later heals it -- so we trade
    strict atomicity for far fewer syscalls and no per-tick "new file" that
    on-access antivirus would re-scan."""
    try:
        data = text
        try:
            if isinstance(text, unicode):
                data = text.encode('utf-8')
        except NameError:
            pass
        f = open(path, 'wb')
        try:
            f.write(data)
        finally:
            f.close()
        return True
    except Exception, e:
        logError('write failed for {}: {}'.format(path, _str(e)))
        return False


# ===========================================================================
# background writer -- keeps JSON encoding + disk I/O off the game thread
# ===========================================================================
class _AsyncWriter(object):
    """Single daemon thread that serializes plain-data snapshots and writes them
    to disk. The game thread only builds the (pure python) snapshot dict and drops
    it in a latest-wins slot per path; if the disk stalls we simply skip stale
    frames instead of backing up a queue or blocking rendering."""

    def __init__(self, threading_mod):
        self._lock = threading_mod.Lock()
        self._event = threading_mod.Event()
        self._pending = {}        # path -> latest snapshot dict (latest wins)
        self._alive = True
        th = threading_mod.Thread(target=self._run)
        try:
            th.setName('WowsExtractorWriter')
        except:
            pass
        try:
            th.setDaemon(True)
        except:
            try:
                th.daemon = True
            except:
                pass
        self._thread = th
        th.start()

    def submit(self, path, obj):
        self._lock.acquire()
        try:
            self._pending[path] = obj
        finally:
            self._lock.release()
        self._event.set()

    def _drain(self):
        self._lock.acquire()
        try:
            batch = self._pending
            self._pending = {}
        finally:
            self._lock.release()
        return batch

    def _run(self):
        while self._alive:
            try:
                self._event.wait(1.0)
                self._event.clear()
                batch = self._drain()
                for path, obj in batch.items():
                    try:
                        _write_file(path, _serialize_safe(obj))
                    except:
                        pass
            except:
                # the writer thread must never die on a transient error
                try:
                    self._event.clear()
                except:
                    pass


def _coerce(val):
    """Keep only JSON-friendly values; convert Vector3-likes to [x, y, z].
    Returns None for anything we don't want serialized (engine objects etc.)."""
    if val is None:
        return None
    if isinstance(val, bool):
        return val
    if isinstance(val, (int, long, float)):
        return val
    if isinstance(val, (str, unicode)):
        return val
    v = _vec(val)
    if v is not None:
        return v
    if isinstance(val, (list, tuple)):
        out = []
        for x in val:
            c = _coerce(x)
            if c is not None:
                out.append(c)
        return out
    return None


def _vec(pos):
    """Extract (x, y, z) from a Vector3 / tuple / object, defensively."""
    if pos is None:
        return None
    for attrs in (('x', 'y', 'z'),):
        try:
            return [float(getattr(pos, attrs[0])), float(getattr(pos, attrs[1])),
                    float(getattr(pos, attrs[2]))]
        except:
            pass
    try:
        return [float(pos[0]), float(pos[1]), float(pos[2])]
    except:
        return None


def _component_pos(comp):
    """Extract [x, y, z] from a dataHub position component (e.g. worldPosition).

    The component may itself behave like a Vector3, or wrap the coordinates under
    a sub-attribute, so probe both shapes defensively."""
    if comp is None:
        return None
    pos = _vec(comp)
    if pos:
        return pos
    for attr in ('position', 'worldPosition', 'point', 'value', 'pos', 'coords'):
        pos = _vec(_get(comp, attr))
        if pos:
            return pos
    return None


def _point_xy(pt):
    """Extract [x, y] from a 2D Point-like object (mapPosition.position)."""
    if pt is None:
        return None
    try:
        return [float(pt.x), float(pt.y)]
    except:
        pass
    try:
        return [float(pt[0]), float(pt[1])]
    except:
        return None


def _map_norm_xy(x, y):
    """Return trusted minimap-normalized [x, y], or None if the source is bogus."""
    try:
        nx = float(x)
        ny = float(y)
    except:
        return None
    if nx != nx or ny != ny:
        return None
    if nx < -0.01 or nx > 1.01 or ny < -0.01 or ny > 1.01:
        return None
    if nx < 0.0:
        nx = 0.0
    elif nx > 1.0:
        nx = 1.0
    if ny < 0.0:
        ny = 0.0
    elif ny > 1.0:
        ny = 1.0
    return [nx, ny]


def _looks_like_space(v):
    """True if `v` looks like an internal WoWS space id.

    Examples: "spaces/13_OC_new_dawn", "13_OC_new_dawn", "s02_Naval_Defense".
    The leading token is digits (13) or a letter + digits (s02). This lets the
    collector pick the recognition key out of a grab-bag of arena attributes,
    even when one of them is a localized display name instead.
    """
    if not isinstance(v, (str, unicode)):
        return False
    tail = v.replace('\\', '/').strip().strip('/').rsplit('/', 1)[-1]
    if '_' not in tail:
        return False
    head = tail.split('_', 1)[0]
    if head.isdigit():
        return True
    return len(head) >= 2 and head[0].isalpha() and head[1:].isdigit()


# ===========================================================================
# damage accumulation (mirrors DamageMonitor approach)
# ===========================================================================
class DamageTracker(object):
    def __init__(self):
        self.clear()

    def clear(self):
        self._inflicted = {}   # attackerPlayerId -> {'total':x, 'byVictim':{vid:dmg}}
        self._received = {}    # victimPlayerId   -> {'total':x, 'byAttacker':{aid:dmg}}
        self._teamTotal = {}   # teamId -> total

    def on_damages(self, victimVehicleId, damages):
        victim = _try(lambda: battle.getPlayerByVehicleId(victimVehicleId))
        if victim is None:
            return
        victimId = _try(lambda: victim.id)
        if victimId is None:
            return
        for d in (damages or []):
            dmg = _try(lambda: d['damage'], 0) or 0
            if dmg <= 0:
                continue
            attacker = _try(lambda: battle.getPlayerByVehicleId(d['vehicleID']))
            if attacker is None:
                continue
            attackerId = _try(lambda: attacker.id)
            teamId = _try(lambda: attacker.teamId)
            if attackerId is None:
                continue
            inf = self._inflicted.setdefault(attackerId, {'total': 0, 'byVictim': {}})
            inf['total'] += dmg
            inf['byVictim'][victimId] = inf['byVictim'].get(victimId, 0) + dmg
            rec = self._received.setdefault(victimId, {'total': 0, 'byAttacker': {}})
            rec['total'] += dmg
            rec['byAttacker'][attackerId] = rec['byAttacker'].get(attackerId, 0) + dmg
            if teamId is not None:
                self._teamTotal[teamId] = self._teamTotal.get(teamId, 0) + dmg

    def snapshot(self):
        # stringify int keys so they survive JSON object keys cleanly
        def keymap(d):
            return dict((str(k), v) for k, v in d.items())
        inflicted = {}
        for aid, info in self._inflicted.items():
            inflicted[str(aid)] = {
                'total': info['total'],
                'byVictim': keymap(info['byVictim']),
            }
        received = {}
        for vid, info in self._received.items():
            received[str(vid)] = {
                'total': info['total'],
                'byAttacker': keymap(info['byAttacker']),
            }
        return {
            'inflicted': inflicted,
            'received': received,
            'teamTotal': keymap(self._teamTotal),
        }


# ===========================================================================
# ballistics tracker (best-effort; mirrors PenetrationCalculator)
# ===========================================================================
class BallisticsTracker(object):
    def __init__(self):
        self._ammo = None
        self._modifiers = None
        self._squadron = False

    def start(self):
        self._modifiers = _try(lambda: battle.getAmmoModifiers())
        self._squadron = False
        self._refresh_selected()

    def stop(self):
        self._ammo = None
        self._modifiers = None
        self._squadron = False

    def _refresh_selected(self):
        wt = _try(lambda: constants.WeaponType.ARTILLERY)
        ammoId = _try(lambda: battle.getSelectedAmmoId(wt), INVALID)
        self.on_ammo_changed(ammoId)

    def on_ammo_changed(self, ammoId):
        if ammoId is None or ammoId == INVALID:
            self._ammo = None
            return
        if self._ammo is None or _try(lambda: self._ammo.id) != ammoId:
            self._ammo = _try(lambda: battle.getAmmoParams(ammoId))

    def on_weapon_changed(self, weaponType):
        wt = _try(lambda: constants.WeaponType.ARTILLERY)
        if weaponType == wt:
            self.on_ammo_changed(_try(lambda: battle.getSelectedAmmoId(weaponType), INVALID))
        else:
            self._ammo = None

    def on_squadron(self, bombParamsId):
        self._squadron = True
        self.on_ammo_changed(bombParamsId)

    def on_squadron_off(self, *a):
        self._squadron = False
        self.on_ammo_changed(INVALID)

    def snapshot(self):
        ammo = self._ammo
        if ammo is None:
            return {'available': False}
        data = {'available': True, 'squadronMode': bool(self._squadron)}
        data['ammoType'] = _try(lambda: ammo.ammoType)
        data['ammoId'] = _try(lambda: ammo.id)
        hoop = _try(lambda: battle.getSelfHoopRanging())
        impactSpeed = self._impact_speed(ammo, hoop)
        data['penetration'] = self._penetration(ammo, impactSpeed)
        data['impactSpeed'] = impactSpeed
        data['impactAngle'] = _try(lambda: __import__('math').degrees(hoop.pitch)) if hoop is not None else None
        isAP = data['ammoType'] == 'AP'
        isCS = data['ammoType'] == 'CS'
        if isAP or isCS:
            data['startRicochet'] = _try(lambda: ammo.bulletRicochetAt)
            data['alwaysRicochet'] = _try(lambda: ammo.bulletAlwaysRicochetAt)
            data['overmatch'] = _try(lambda: __import__('math').floor(ammo.bulletDiametr * 1000 / 14.3))
        if isAP:
            data['detonatorDelay'] = _try(lambda: ammo.bulletDetonator)
            data['detonatorThreshold'] = _try(lambda: ammo.bulletDetonatorThreshold)
        return data

    def _impact_speed(self, ammo, hoop):
        if self._squadron:
            return _try(lambda: ammo.bulletSpeed) if _try(lambda: ammo.ammoType) == 'AP' else None
        if hoop is None:
            return None
        gunPos = _try(lambda: hoop.gunPos)
        gunDir = _try(lambda: hoop.gunDir)
        if gunPos is None or gunDir is None:
            return None
        if _try(lambda: gunPos.y, 0) >= 0.001:
            return _try(lambda: battle.getAmmoImpactSpeed(ammo, gunPos, gunDir))
        return None

    def _penetration(self, ammo, impactSpeed):
        atype = _try(lambda: ammo.ammoType)
        if atype == 'HE':
            return _try(lambda: int(ammo.alphaPiercingHE))
        if atype == 'CS':
            return _try(lambda: int(ammo.alphaPiercingCS))
        if atype == 'AP' and impactSpeed:
            def calc():
                import math
                krupp = battle.getBulletKrupp(ammo, self._modifiers)
                raw = (krupp * (ammo.bulletMass * impactSpeed * impactSpeed) ** 0.69
                       * ammo.bulletDiametr ** (-1.07) * 0.0000001)
                if self._squadron:
                    return int(raw)
                normalize = math.radians(ammo.bulletCapNormalizeMaxAngle)
                hoop = battle.getSelfHoopRanging()
                angle = max(0, abs(hoop.pitch) - normalize)
                return int(raw * math.cos(angle))
            return _try(calc)
        return None


# ===========================================================================
# main collector
# ===========================================================================
class Collector(object):
    def __init__(self):
        self._stateFile = None
        self._metaFile = None
        self._tickHandle = None
        self._lastWrite = 0.0
        self._active = False
        self._selfPlayerId = None
        self._battleIdentity = BattleIdentity(SESSION_NONCE)
        self._battleId = None
        self._selfTeam = _UNSET       # memoized once per snapshot build
        self._stateInterval = STATE_INTERVAL
        self._lastSeenTtl = LAST_SEEN_TTL
        self._lastMetaWrite = 0.0
        self._metaRetryUntil = 0.0
        self._metaReady = False
        self._load_config()
        self._damage = DamageTracker()
        self._ballistics = BallisticsTracker()
        # key -> {identity{...}, pos, yaw, health, ts}; last spot of each ship we
        # have seen, so enemies that lit up then went dark keep a "ghost" marker.
        self._lastSeen = {}
        # Ships observed dead this battle. Prevents a sunk enemy that drops out of
        # getAllShips() from being resurrected as a last-seen ("灭点") ghost.
        self._deadKeys = set()
        # cached map->world affine (per battle/map); derived at runtime from ships
        # that expose BOTH getPosition() (world) and the mapPosition component.
        self._mapTransform = None

        # Move JSON encoding + disk writes off the game thread when possible.
        self._writer = None
        if _threading is not None:
            try:
                self._writer = _AsyncWriter(_threading)
                logInfo('async writer enabled (encoding + disk I/O off game thread)')
            except Exception, e:
                self._writer = None
                logError('async writer unavailable, using sync writes: {}'.format(_str(e)))

        events.onBattleStart(self._on_battle_start)
        events.onBattleShown(self._on_battle_shown)
        events.onBattleQuit(self._on_battle_quit)
        events.onReceiveDamagesOnShip(self._on_damages)
        # ballistics (best effort; some builds may not expose all of these)
        _try(lambda: events.onArtilleryAmmoChanged(self._ballistics.on_ammo_changed))
        _try(lambda: events.onWeaponTypeChanged(self._ballistics.on_weapon_changed))
        _try(lambda: events.onSquadronActivated(self._ballistics.on_squadron))
        _try(lambda: events.onSquadronDeactivated(self._ballistics.on_squadron_off))
        logInfo('loaded v{} (state interval {}s)'.format(MOD_VERSION, self._stateInterval))

    # -- config -------------------------------------------------------------
    def _load_config(self):
        """(Re)read config.ini from the mod folder. Called at load and again on
        each battle start, so edits are picked up without restarting the game."""
        cfg = _load_mod_config()
        # clamp the interval so a typo (0 / negative) can't hammer the disk
        self._stateInterval = max(_cfg_float(cfg, 'state_interval', STATE_INTERVAL), 0.02)
        self._lastSeenTtl = _cfg_float(cfg, 'last_seen_ttl', LAST_SEEN_TTL)

    # -- paths --------------------------------------------------------------
    def _resolve_paths(self):
        base = _try(lambda: utils.getModDir())
        if not base:
            base = '.'
        self._stateFile = base + '/state.json'
        self._metaFile = base + '/meta.json'
        try:
            abspath = os.path.abspath(self._stateFile) if os else self._stateFile
        except:
            abspath = self._stateFile
        logInfo('writing telemetry to: {}'.format(abspath))

    # -- lifecycle ----------------------------------------------------------
    def _on_battle_start(self, *args):
        self._load_config()
        self._resolve_paths()
        self._damage.clear()
        self._lastSeen = {}
        self._deadKeys = set()
        self._mapTransform = None
        self._lastMetaWrite = 0.0
        self._metaRetryUntil = 0.0
        self._metaReady = False
        self._selfPlayerId = _try(lambda: battle.getSelfPlayerInfo().id)
        self._battleId = self._battleIdentity.start(
            self._selfPlayerId, self._find_arena_id())

    def _find_arena_id(self):
        """Return the raw arena id when the client has exposed it yet."""
        for getter in (
                lambda: battle.getArenaInfo(),
                lambda: self._battle_info_component()):
            info = _try(getter)
            if info is None:
                continue
            for key in ('arenaUniqueId', 'arenaUniqueID', 'uniqueId',
                        'uniqueID', 'arenaId', 'arenaID', 'battleId'):
                val = _coerce(_get(info, key))
                if val not in (None, '', 0):
                    return val
        return None

    def _promote_battle_id(self):
        """Promote a fallback id when the arena id arrives after battle start."""
        before = self._battleId
        self._battleId = self._battleIdentity.promote(self._find_arena_id())
        return self._battleId != before

    def _on_battle_shown(self, *args):
        self._active = True
        self._ballistics.start()
        self._metaRetryUntil = _now() + META_RETRY_SECONDS
        self._refresh_meta(force=True)
        # start the high-frequency state loop
        self._stop_tick()
        self._tickHandle = _try(lambda: callbacks.perTick(self._tick))
        self._lastWrite = 0.0
        self._tick()  # write an immediate first frame

    def _on_battle_quit(self, *args):
        self._active = False
        self._stop_tick()
        self._ballistics.stop()
        self._lastSeen = {}
        self._deadKeys = set()
        self._mapTransform = None
        # write one final snapshot marking the battle inactive (through the same
        # single writer, so it can't race a still-queued "active" frame)
        try:
            snap = {'schema': SCHEMA_VERSION, 'apiVersion': WIRE_API_VERSION,
                    'battleId': self._battleId, 'active': False, 'ts': _now(),
                    'ships': [], 'self': None}
            self._write_state(snap)
        except:
            pass

    def _stop_tick(self):
        if self._tickHandle is not None:
            _try(lambda: callbacks.cancel(self._tickHandle))
            self._tickHandle = None

    def _on_damages(self, victimId, damages):
        _try(lambda: self._damage.on_damages(victimId, damages))

    # -- state output -------------------------------------------------------
    def _write_state(self, snap):
        """Emit a state snapshot. With the background writer, the game thread does
        no encoding or disk I/O at all -- it just hands off the plain dict. Without
        it, fall back to an in-place synchronous write."""
        path = self._stateFile
        if not path:
            return
        # Drop a late live frame that would overwrite the quit-written inactive
        # snapshot (tick can finish building after `_on_battle_quit`).
        if not should_write_state(self._active, snap):
            return
        w = self._writer
        if w is not None:
            try:
                w.submit(path, snap)
                return
            except:
                pass
        _write_file(path, json_dumps(snap))

    # -- per-frame (throttled) ---------------------------------------------
    def _tick(self, *args):
        if not self._active:
            return
        now = _now()
        self._refresh_meta(now)
        if (now - self._lastWrite) < self._stateInterval:
            return
        self._lastWrite = now
        try:
            snap = self._build_state(now)
            # Quit can land while we were building; never overwrite the terminal
            # inactive frame with a stale live snapshot.
            if not self._active:
                return
            self._write_state(snap)
        except Exception, e:
            logError('tick failed: {}'.format(_str(e)))

    # -- relation helper ----------------------------------------------------
    def _self_team(self):
        """Self team id, memoized for the duration of one snapshot build so we
        don't hit the engine ~24x per frame (once per ship)."""
        if self._selfTeam is _UNSET:
            team = None
            try:
                team = battle.getSelfPlayer().teamId
            except:
                team = _try(lambda: battle.getSelfPlayerInfo().teamId)
            self._selfTeam = team
        return self._selfTeam

    def _relation(self, teamId):
        # 0 = self team in observer? We use: 0=self,1=ally,2=enemy
        selfTeam = self._self_team()
        if selfTeam is None or teamId is None:
            return INVALID
        return 1 if teamId == selfTeam else 2

    # -- meta (static per battle) ------------------------------------------
    def _battle_info_component(self):
        """Return the BattleInfo component from whichever dataHub path is ready."""
        for getter in (
                lambda: dataHub.getSingleEntity('battleInfo')[CC.battleInfo],
                lambda: dataHub.getSingleComponent(CC.battleInfo),
                lambda: dataHub.getSingleEntity('battleData')[CC.battleInfo]):
            comp = _try(getter)
            if comp is not None:
                return comp
        for entity in _try(lambda: dataHub.getEntityCollections('battleData'), []) or []:
            comp = _try(lambda e=entity: e[CC.battleInfo])
            if comp is not None:
                return comp
        return None

    def _meta_has_precise_map(self, info):
        if not info:
            return False
        for key in ('id', 'geometryName', 'geometry', 'spaceName', 'spaceId',
                    'mapPath', 'mapName', 'name', 'arenaName'):
            if _looks_like_space(info.get(key)):
                return True
        for keys in (('minX', 'maxX', 'minZ', 'maxZ'),
                     ('minX', 'maxX', 'minY', 'maxY')):
            ok = True
            for key in keys:
                if not isinstance(info.get(key), (int, long, float)):
                    ok = False
                    break
            if ok:
                return True
        return False

    def _meta_ready_for_clients(self, meta):
        mode = meta.get('battleType') or meta.get('gameMode')
        return bool(mode) and self._meta_has_precise_map(meta.get('map') or {})

    def _refresh_meta(self, now=None, force=False):
        if now is None:
            now = _now()
        within_retry = (not self._metaRetryUntil
                        or now <= self._metaRetryUntil)
        promoted = False
        if force or (within_retry and not self._battleIdentity.is_arena):
            promoted = self._promote_battle_id()
        if self._metaReady and not force and not promoted:
            return
        if (not force) and not within_retry and not promoted:
            return
        if (not force) and not promoted \
                and (now - self._lastMetaWrite) < META_RETRY_INTERVAL:
            return
        meta = self._build_meta()
        _atomic_write(self._metaFile, json_dumps(meta))
        self._lastMetaWrite = now
        self._metaReady = self._meta_ready_for_clients(meta)
        if force or self._metaReady:
            logInfo('meta.json written ({} players, ready={})'.format(
                len(meta.get('roster', [])), self._metaReady))

    def _build_meta(self):
        self._selfTeam = _UNSET
        battleInfo = self._battle_info_component()
        battleType = _coerce(_get(battleInfo, 'battleType'))
        gameMode = _coerce(_get(battleInfo, 'gameMode'))
        meta = {
            'schema': SCHEMA_VERSION,
            'apiVersion': WIRE_API_VERSION,
            'battleId': self._battleId,
            'mod': {'name': MOD_NAME, 'version': MOD_VERSION},
            'ts': _now(),
            'battleType': battleType or gameMode,
            'gameMode': gameMode,
            'selfPlayerId': self._selfPlayerId,
            'map': self._build_map_info(battleInfo),
            'roster': self._build_roster(),
        }
        return meta

    def _build_map_info(self, battleInfo=None):
        """Collect the map's identity (for server-side recognition) and, when
        the client exposes them, raw world bounds.

        The server only needs a recognizable space id (e.g. ``13_OC_new_dawn``)
        to look up exact, origin-centred bounds; numeric bounds here are an
        optional bonus that takes priority when present. We probe several
        sources because attribute names vary across client builds -- whichever
        one carries the space id, the server resolver will match it.
        """
        info = {}
        # String-ish identifiers first, then any numeric bounds we can find.
        name_keys = ('geometryName', 'geometry', 'mapName', 'spaceName',
                     'spaceId', 'mapPath', 'name', 'mapId', 'arenaName')
        num_keys = ('width', 'height', 'size', 'worldSize', 'spaceBounds',
                    'bounds', 'minX', 'maxX', 'minY', 'maxY', 'minZ', 'maxZ')

        def scan(obj):
            if obj is None:
                return
            for key in name_keys + num_keys:
                if info.get(key) is not None:
                    continue
                v = _coerce(_get(obj, key))
                if v is not None:
                    info[key] = v

        # 1) arena info method on `battle`
        scan(_try(lambda: battle.getArenaInfo()))
        # 2) the battleInfo component (same entity used for battleType)
        scan(battleInfo if battleInfo is not None else self._battle_info_component())
        # 3) mapInfo and dataHub arena/space entities
        mapId = info.get('mapId')
        mapEnt = _try(lambda: dataHub.getPrimaryEntity(CC.mapInfo, mapId))
        scan(_try(lambda: mapEnt[CC.mapInfo]))
        for ename in ('arena', 'arenaInfo', 'minimap', 'space', 'spaceInfo'):
            ent = _try(lambda e=ename: dataHub.getSingleEntity(e))
            if ent is None:
                continue
            scan(_try(lambda e=ename, x=ent: x[getattr(CC, e)]))

        # Promote the first space-looking value to `id`: the recognition key
        # the server uses to resolve the friendly name and exact bounds.
        for key in ('geometryName', 'geometry', 'mapName', 'spaceName',
                    'spaceId', 'mapPath', 'name', 'mapId'):
            if _looks_like_space(info.get(key)):
                info['id'] = info[key]
                break
        return info

    def _build_roster(self):
        roster = {}
        # avatars: team / ship class / ship name / tier
        for entity in _try(lambda: dataHub.getEntityCollections('avatar'), []) or []:
            a = _try(lambda: entity[CC.avatar])
            if a is None:
                continue
            pid = _try(lambda: a.playerId)
            if pid is None:
                pid = _try(lambda: a.id)
            if pid is None:
                continue
            rec = roster.setdefault(pid, {'playerId': pid})
            rec['teamId'] = _try(lambda: a.teamId)
            rec['relation'] = self._relation(rec['teamId'])
            rec['name'] = _try(lambda: a.name)
            rec['shipType'] = _try(lambda: a.shipType)
            rec['shipName'] = _try(lambda: a.ship.ref.ship.fullName)
            rec['shipTier'] = _try(lambda: a.ship.ref.ship.level)
        # shipBattleInfo: consumables (ranges & cooldowns)
        for entity in _try(lambda: dataHub.getEntityCollections('shipBattleInfo'), []) or []:
            s = _try(lambda: entity[CC.shipBattleInfo])
            if s is None:
                continue
            pid = _try(lambda: s.playerId)
            if pid is None:
                continue
            rec = roster.setdefault(pid, {'playerId': pid})
            cons = self._extract_consumables(s)
            if cons:
                rec['consumables'] = cons
        return list(roster.values())

    def _extract_consumables(self, ship):
        out = []
        main = _try(lambda: list(ship.mainConsumables), []) or []
        alt = _try(lambda: [c for lst in ship.altConsumables for c in lst], []) or []
        for cons in (main + alt):
            title = _try(lambda: cons.title)
            if title is None:
                continue
            item = {'title': title, 'abilityId': _try(lambda: cons.abilityId)}
            params = {}
            for cat in ('activeAttributes', 'usageAttributes'):
                catObj = _try(lambda: getattr(cons, cat))
                if catObj is None:
                    continue
                for typ in ('neutral', 'positive', 'negative'):
                    lst = _try(lambda: getattr(catObj, typ))
                    if not lst:
                        continue
                    for attr in lst:
                        name = _try(lambda: attr.paramName)
                        if name is None:
                            continue
                        val = _try(lambda: attr.measuredValue)
                        if val is None:
                            val = _try(lambda: attr.numericValue)
                        params[name] = val
            if params:
                item['params'] = params
            out.append(item)
        return out

    # -- state (per frame) --------------------------------------------------
    def _build_state(self, now):
        self._selfTeam = _UNSET   # recompute self team once for this frame
        ships, diag = self._build_ships(now)
        return {
            'schema': SCHEMA_VERSION,
            'apiVersion': WIRE_API_VERSION,
            'battleId': self._battleId,
            'active': bool(self._active),
            'ts': now,
            'self': self._build_self(),
            'ships': ships,
            'damage': self._damage.snapshot(),
            'ballistics': _try(lambda: self._ballistics.snapshot(), {'available': False}),
            'diag': diag,
        }

    def _build_self(self):
        info = _try(lambda: battle.getSelfPlayerInfo())
        data = {}
        if info is not None:
            data['playerId'] = _try(lambda: info.id)
            data['teamId'] = _try(lambda: info.teamId)
            gd = _try(lambda: info.shipGameData)
            if gd is not None:
                for k in ('health', 'maxHealth', 'yaw', 'speed', 'isVisible', 'isShipVisible'):
                    v = _try(lambda: gd[k]) if _is_mapping(gd) else _try(lambda: getattr(gd, k))
                    if v is not None:
                        data[k] = v
        ship = _try(lambda: battle.getSelfPlayerShip())
        if ship is None:
            ship = _try(lambda: battle.getObserverShip())
        if ship is not None:
            pos = _vec(_try(lambda: ship.getPosition()))
            if pos:
                data['position'] = pos
            for k in ('health', 'maxHealth', 'yaw'):
                if k not in data:
                    v = _try(lambda: getattr(ship, k))
                    if v is not None:
                        data[k] = v
        data['isObserver'] = _try(lambda: battle.isObserverMode(), False)
        return data if data else None

    def _ship_key(self, entry):
        """Stable per-battle key for last-seen tracking (prefixed to avoid
        collisions between different id namespaces)."""
        keys = self._ship_keys(entry)
        return keys[0] if keys else None

    def _ship_keys(self, entry):
        """All id aliases for a ship row / identity dict."""
        keys = []
        if not entry:
            return keys
        for k in ('vehicleId', 'uiId', 'playerId'):
            v = entry.get(k)
            if v is not None:
                keys.append(k[0] + str(v))
        return keys

    def _entry_is_dead(self, entry):
        """True when the ship should never keep a last-seen ghost.

        Use == so integer 0 from some client builds counts as dead; `is False`
        would miss that and remember the wreck as a 灭点.
        """
        if not entry:
            return False
        alive = entry.get('alive')
        if alive == False or alive == 0:
            return True
        health = entry.get('health')
        try:
            if health is not None and float(health) <= 0:
                return True
        except:
            pass
        return False

    def _forget_ship(self, entry):
        """Drop last-seen memory for every id alias of this ship and mark dead."""
        keys = set(self._ship_keys(entry))
        for key in list(self._lastSeen.keys()):
            ident = self._lastSeen[key].get('identity') or {}
            ident_keys = self._ship_keys(ident)
            if key in keys or keys.intersection(ident_keys):
                keys.update(ident_keys)
                keys.add(key)
        for key in keys:
            self._deadKeys.add(key)
            if key in self._lastSeen:
                del self._lastSeen[key]

    def _remember(self, key, entry, pos, now):
        self._lastSeen[key] = {
            'identity': {
                'uiId': entry.get('uiId'),
                'vehicleId': entry.get('vehicleId'),
                'playerId': entry.get('playerId'),
                'teamId': entry.get('teamId'),
                'relation': entry.get('relation'),
                'name': entry.get('name'),
                'shipType': entry.get('shipType'),
            },
            'pos': pos,
            'nx': entry.get('nx'),
            'ny': entry.get('ny'),
            'yaw': entry.get('yaw'),
            'health': entry.get('health'),
            'ts': now,
        }

    def _apply_ghost(self, target, seen, now):
        """Attach lastPosition/staleSeconds fields onto target from a cache entry."""
        if seen.get('pos') is not None:
            target['lastPosition'] = seen['pos']
        if seen.get('nx') is not None and seen.get('ny') is not None:
            target['lastNx'] = seen['nx']
            target['lastNy'] = seen['ny']
        if seen.get('yaw') is not None:
            target['lastYaw'] = seen['yaw']
        if seen.get('health') is not None:
            target['lastHealth'] = seen['health']
        target['lastSeenTs'] = seen['ts']
        target['staleSeconds'] = now - seen['ts']

    def _position_from_obj(self, obj):
        """Extract world position from a ship-like object or mapping."""
        if obj is None:
            return None
        for getter in (lambda: obj.getPosition() if hasattr(obj, 'getPosition') else None,
                       lambda: obj.position,
                       lambda: obj.worldPosition,
                       lambda: obj.positionHull):
            pos = _vec(_try(getter))
            if pos:
                return pos
        gd = _get(obj, 'shipGameData')
        if gd is not None:
            for key in ('position', 'worldPosition'):
                if _is_mapping(gd):
                    pos = _vec(_try(lambda k=key: gd[k]))
                else:
                    pos = _vec(_try(lambda k=key: getattr(gd, k)))
                if pos:
                    return pos
            if _is_mapping(gd):
                try:
                    if gd.get('x') is not None and gd.get('z') is not None:
                        y = gd.get('y', 0.0)
                        return [float(gd['x']), float(y), float(gd['z'])]
                except:
                    pass
        if _is_mapping(obj):
            for key in ('position', 'worldPosition'):
                pos = _vec(_try(lambda k=key: obj[k]))
                if pos:
                    return pos
        return None

    def _get_ship_position(self, ship, playerId=None, vehId=None):
        """Try multiple sources to get a ship's world position.
        Different WoWS builds expose position via different APIs; spotted enemies
        in particular may only be reachable through getPlayerShipInfo()."""
        pos = self._position_from_obj(ship)
        if pos:
            return pos
        if playerId is not None:
            for getter in (lambda: battle.getPlayerShipInfo(playerId),
                           lambda: battle.getPlayerInfo(playerId)):
                pos = self._position_from_obj(_try(getter))
                if pos:
                    return pos
        if vehId is not None:
            player = _try(lambda: battle.getPlayerByVehicleId(vehId))
            if player is not None:
                pos = self._position_from_obj(player)
                if pos:
                    return pos
                pid = _get(player, 'id')
                if pid is not None and pid != playerId:
                    for getter in (lambda: battle.getPlayerShipInfo(pid),
                                   lambda: battle.getPlayerInfo(pid)):
                        pos = self._position_from_obj(_try(getter))
                        if pos:
                            return pos
        return None

    def _collect_map_positions(self):
        """Map playerId -> (mapX, mapY, mapYaw) from each ship entity's
        mapPosition component.

        ship.getPosition() and the worldPosition component are only populated for
        ships rendered in our own 3D bubble. The mapPosition component, however, is
        populated for EVERY ship drawn on the minimap -- including enemies spotted
        only by a teammate. Its `position` is a 2D Point in minimap space, which we
        convert to world coordinates via a per-battle calibration (see
        _fit_map_transform)."""
        out = {}
        if CC is None:
            return out
        mpKey = _get(CC, 'mapPosition')
        avKey = _get(CC, 'avatar')
        visKey = _get(CC, 'visibility')
        if mpKey is None or avKey is None:
            return out
        for entity in _try(lambda: dataHub.getEntityCollections('avatar'), []) or []:
            a = _try(lambda e=entity: e[avKey])
            if a is None:
                continue
            pid = _get(a, 'playerId')
            if pid is None:
                pid = _get(a, 'id')
            if pid is None:
                continue
            if visKey is not None:
                vis = _try(lambda e=entity: e[visKey])
                # Last-visible markers can keep a stale mapPosition; live state
                # should only use positions the game currently marks on-map.
                if vis is not None and _get(vis, 'mapVisible') is False:
                    continue
            comp = _try(lambda e=entity: e[mpKey])
            if comp is None:
                continue
            xy = _point_xy(_get(comp, 'position'))
            if xy is None:
                continue
            out[pid] = (xy[0], xy[1], _get(comp, 'yaw'))
        return out

    @staticmethod
    def _fit_axis(ms, ws):
        """Least-squares fit w = a*m + b; returns (a, b, residual) or None."""
        n = len(ms)
        if n < 2:
            return None
        mean_m = sum(ms) / n
        mean_w = sum(ws) / n
        var_m = sum((m - mean_m) ** 2 for m in ms)
        if var_m <= 1e-6:
            return None
        cov = sum((ms[i] - mean_m) * (ws[i] - mean_w) for i in range(n))
        a = cov / var_m
        b = mean_w - a * mean_m
        res = sum((ws[i] - (a * ms[i] + b)) ** 2 for i in range(n))
        return (a, b, res)

    def _fit_map_transform(self, samples):
        """Derive the affine mapping minimap-space -> world from ships that expose
        both. `samples` = [(mapX, mapY, worldX, worldZ), ...].

        Returns ('A'|'B', ax, bx, az, bz) where 'A' means worldX<-mapX, worldZ<-mapY
        and 'B' means the axes are swapped. We try both and keep the better fit, so
        we don't have to assume the minimap's axis orientation up front.

        Needs >=3 samples: with only 2 points both orientations fit exactly, so a
        later frame that drops one axis (ships aligned) can flip A<->B and make
        map-only enemies jump on the overlay."""
        if len(samples) < 3:
            return None
        mxs = [s[0] for s in samples]
        mys = [s[1] for s in samples]
        wxs = [s[2] for s in samples]
        wzs = [s[3] for s in samples]
        a_x = self._fit_axis(mxs, wxs)
        a_z = self._fit_axis(mys, wzs)
        b_x = self._fit_axis(mys, wxs)
        b_z = self._fit_axis(mxs, wzs)
        cands = []
        if a_x and a_z:
            cands.append(('A', a_x[2] + a_z[2], a_x, a_z))
        if b_x and b_z:
            cands.append(('B', b_x[2] + b_z[2], b_x, b_z))
        if not cands:
            return None
        cands.sort(key=lambda c: c[1])
        kind, _res, fx, fz = cands[0]
        return (kind, fx[0], fx[1], fz[0], fz[1])

    def _map_to_world(self, mx, my):
        t = self._mapTransform
        if t is None:
            return None
        kind, ax, bx, az, bz = t
        if kind == 'A':
            return [ax * mx + bx, 0.0, az * my + bz]
        return [ax * my + bx, 0.0, az * mx + bz]

    def _build_ships(self, now):
        ships = _try(lambda: battle.getAllShips(), []) or []
        mapIndex = self._collect_map_positions()   # playerId -> (mapX, mapY, mapYaw)

        # -- pass 1: resolve ids + world positions; calibrate map->world ---------
        infos = []
        calib = []
        for ship in ships:
            vehId = None
            for _name in ('_Ship__id', 'id', 'vehicleId'):
                vehId = _get(ship, _name)
                if vehId is not None:
                    break
            player = _try(lambda: battle.getPlayerByVehicleId(vehId)) if vehId is not None else None
            pid = _get(player, 'id') if player is not None else None
            worldPos = self._get_ship_position(ship, pid, vehId)
            infos.append((ship, vehId, player, pid, worldPos))
            if worldPos and pid is not None:
                mp = mapIndex.get(pid)
                if mp is not None:
                    calib.append((mp[0], mp[1], worldPos[0], worldPos[2]))
        # Lock the first good fit for the whole battle. Re-fitting every frame
        # can flip A<->B when calibration ships line up on one axis, which makes
        # spotted-only enemies (mapPosition -> world) jump to the wrong place.
        if self._mapTransform is None:
            t = self._fit_map_transform(calib)
            if t is not None:
                self._mapTransform = t

        # -- pass 2: build per-ship entries --------------------------------------
        out = []
        seen_keys = set()
        nAlly = 0
        nAllyVis = 0
        nEnemy = 0
        nEnemyVis = 0
        for ship, vehId, player, pid, worldPos in infos:
            entry = {}
            entry['uiId'] = _get(ship, 'uiId')
            if vehId is not None:
                entry['vehicleId'] = vehId
            teamId = _get(ship, 'teamId')
            entry['teamId'] = teamId
            entry['relation'] = self._relation(teamId)
            entry['alive'] = _try(lambda: ship.isAlive())
            if player is not None:
                entry['playerId'] = pid
                if entry.get('teamId') is None:
                    entry['teamId'] = _get(player, 'teamId')
                    entry['relation'] = self._relation(entry['teamId'])

            pos = worldPos
            mp = mapIndex.get(pid) if pid is not None else None
            norm = _map_norm_xy(mp[0], mp[1]) if mp is not None else None
            if norm is not None:
                # mapPosition is already minimap-normalized; this keeps 1v1
                # enemies drawable even when only self is available to calibrate.
                entry['nx'] = norm[0]
                entry['ny'] = norm[1]
            if not pos and mp is not None:
                # spotted-but-not-rendered ship (e.g. enemy lit only by a teammate):
                # getPosition() is None, but mapPosition is populated -- convert it
                # to world coordinates using the calibrated transform.
                pos = self._map_to_world(mp[0], mp[1])
            if pos:
                entry['position'] = pos
                entry['visible'] = True
            elif norm is not None:
                entry['visible'] = True
            else:
                entry['visible'] = False
            for k in ('yaw', 'health', 'maxHealth', 'name'):
                v = _get(ship, k)
                if v is not None:
                    entry[k] = v
            # spotted-only enemies have no ship.yaw; fall back to mapPosition yaw
            if entry.get('yaw') is None and mp is not None and mp[2] is not None:
                entry['yaw'] = mp[2]
            # ship class lives on `subtype` on this build (`shipType` is None)
            shipType = _get(ship, 'shipType')
            if shipType is None:
                shipType = _get(ship, 'subtype')
            if shipType is not None:
                entry['shipType'] = shipType
            # track ally/enemy visibility counts for diagnostics
            rel = entry.get('relation')
            if rel == 2:
                nEnemy += 1
                if pos or norm is not None:
                    nEnemyVis += 1
            elif rel == 1:
                nAlly += 1
                if pos or norm is not None:
                    nAllyVis += 1

            keys = self._ship_keys(entry)
            for key in keys:
                seen_keys.add(key)
            key = keys[0] if keys else None
            if self._entry_is_dead(entry):
                # sunk ship: drop any ghost memory, no last-seen marker
                entry['alive'] = False
                self._forget_ship(entry)
            elif any(k in self._deadKeys for k in keys):
                # previously observed dead under another alias; never revive
                entry['alive'] = False
                self._forget_ship(entry)
            elif pos or norm is not None:
                if key is not None:
                    self._remember(key, entry, pos, now)
            elif key is not None:
                # alive but no position this frame -> went dark; reuse last spot
                seen = self._lastSeen.get(key)
                if seen is None:
                    for alt in keys[1:]:
                        seen = self._lastSeen.get(alt)
                        if seen is not None:
                            break
                if seen is not None:
                    health = seen.get('health')
                    try:
                        dead_hp = health is not None and float(health) <= 0
                    except:
                        dead_hp = False
                    if dead_hp:
                        entry['alive'] = False
                        self._forget_ship(entry)
                    elif (now - seen['ts']) <= self._lastSeenTtl:
                        self._apply_ghost(entry, seen, now)
                    else:
                        self._forget_ship(seen.get('identity') or entry)
            out.append(entry)

        # ships no longer returned by getAllShips() at all: emit ghost-only rows
        nGhost = 0
        for key in list(self._lastSeen.keys()):
            if key in seen_keys or key in self._deadKeys:
                if key in self._deadKeys and key in self._lastSeen:
                    del self._lastSeen[key]
                continue
            seen = self._lastSeen[key]
            ident = seen.get('identity') or {}
            if any(k in self._deadKeys for k in self._ship_keys(ident)):
                self._forget_ship(ident)
                continue
            health = seen.get('health')
            try:
                dead_hp = health is not None and float(health) <= 0
            except:
                dead_hp = False
            if dead_hp:
                self._forget_ship(ident)
                continue
            if (now - seen['ts']) > self._lastSeenTtl:
                del self._lastSeen[key]
                continue
            ghost = dict(ident)
            ghost['alive'] = True
            ghost['visible'] = False
            self._apply_ghost(ghost, seen, now)
            out.append(ghost)
            nGhost += 1
        return out, {
            'totalShips': len(ships),
            'allies': nAlly,
            'alliesVisible': nAllyVis,
            'enemies': nEnemy,
            'enemiesVisible': nEnemyVis,
            'ghosts': nGhost,
            'mapCalib': (self._mapTransform[0] if self._mapTransform else None),
            'mapSamples': len(calib),
        }


def _is_mapping(o):
    try:
        return hasattr(o, 'keys') and hasattr(o, '__getitem__')
    except:
        return False


gCollector = Collector()
