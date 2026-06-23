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

try:
    CC = constants.UiComponents
except:
    CC = None

STATE_INTERVAL = 0.1          # seconds between state.json writes (~10 Hz)
SCHEMA_VERSION = 1
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
    try:
        import json
        return json.dumps(obj)
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
        self._damage = DamageTracker()
        self._ballistics = BallisticsTracker()
        self._playerCache = {}   # playerId -> {name, teamId, relation, ship{...}}

        events.onBattleStart(self._on_battle_start)
        events.onBattleShown(self._on_battle_shown)
        events.onBattleQuit(self._on_battle_quit)
        events.onReceiveDamagesOnShip(self._on_damages)
        # ballistics (best effort; some builds may not expose all of these)
        _try(lambda: events.onArtilleryAmmoChanged(self._ballistics.on_ammo_changed))
        _try(lambda: events.onWeaponTypeChanged(self._ballistics.on_weapon_changed))
        _try(lambda: events.onSquadronActivated(self._ballistics.on_squadron))
        _try(lambda: events.onSquadronDeactivated(self._ballistics.on_squadron_off))
        logInfo('loaded v{} (state interval {}s)'.format(MOD_VERSION, STATE_INTERVAL))

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
        self._resolve_paths()
        self._damage.clear()
        self._playerCache = {}
        self._selfPlayerId = _try(lambda: battle.getSelfPlayerInfo().id)

    def _on_battle_shown(self, *args):
        self._active = True
        self._ballistics.start()
        # write meta now that the roster/consumables are available
        meta = self._build_meta()
        _atomic_write(self._metaFile, json_dumps(meta))
        logInfo('meta.json written ({} players)'.format(len(meta.get('roster', []))))
        # start the high-frequency state loop
        self._stop_tick()
        self._tickHandle = _try(lambda: callbacks.perTick(self._tick))
        self._lastWrite = 0.0
        self._tick()  # write an immediate first frame

    def _on_battle_quit(self, *args):
        self._active = False
        self._stop_tick()
        self._ballistics.stop()
        # write one final snapshot marking the battle inactive
        try:
            snap = {'schema': SCHEMA_VERSION, 'active': False, 'ts': _now(),
                    'ships': [], 'self': None}
            if self._stateFile:
                _atomic_write(self._stateFile, json_dumps(snap))
        except:
            pass

    def _stop_tick(self):
        if self._tickHandle is not None:
            _try(lambda: callbacks.cancel(self._tickHandle))
            self._tickHandle = None

    def _on_damages(self, victimId, damages):
        _try(lambda: self._damage.on_damages(victimId, damages))

    # -- per-frame (throttled) ---------------------------------------------
    def _tick(self, *args):
        now = _now()
        if (now - self._lastWrite) < STATE_INTERVAL:
            return
        self._lastWrite = now
        try:
            snap = self._build_state(now)
            _atomic_write(self._stateFile, json_dumps(snap))
        except Exception, e:
            logError('tick failed: {}'.format(_str(e)))

    # -- relation helper ----------------------------------------------------
    def _relation(self, teamId):
        # 0 = self team in observer? We use: 0=self,1=ally,2=enemy
        try:
            selfTeam = battle.getSelfPlayer().teamId
        except:
            selfTeam = _try(lambda: battle.getSelfPlayerInfo().teamId)
        if selfTeam is None or teamId is None:
            return INVALID
        return 1 if teamId == selfTeam else 2

    # -- meta (static per battle) ------------------------------------------
    def _build_meta(self):
        meta = {
            'schema': SCHEMA_VERSION,
            'mod': {'name': MOD_NAME, 'version': MOD_VERSION},
            'ts': _now(),
            'battleType': _try(lambda: dataHub.getSingleEntity('battleInfo')[CC.battleInfo].battleType),
            'selfPlayerId': self._selfPlayerId,
            'map': self._build_map_info(),
            'roster': self._build_roster(),
        }
        return meta

    def _build_map_info(self):
        info = {}
        # Try a few likely sources for arena/space bounds. The probe mod helps
        # confirm which one exists on a given build; all are optional here.
        arena = _try(lambda: battle.getArenaInfo())
        if arena is not None:
            for key in ('name', 'mapName', 'spaceId', 'width', 'height',
                        'minX', 'maxX', 'minY', 'maxY', 'minZ', 'maxZ'):
                v = _coerce(_try(lambda: getattr(arena, key)))
                if v is not None:
                    info[key] = v
        # dataHub fallback
        for ename in ('arena', 'arenaInfo', 'minimap', 'space', 'spaceInfo'):
            ent = _try(lambda: dataHub.getSingleEntity(ename))
            if ent is None:
                continue
            comp = _try(lambda: ent[getattr(CC, ename)])
            if comp is None:
                continue
            for key in ('spaceBounds', 'bounds', 'size', 'worldSize',
                        'minX', 'maxX', 'minY', 'maxY', 'minZ', 'maxZ', 'name'):
                if key in info:
                    continue
                v = _coerce(_try(lambda: getattr(comp, key)))
                if v is not None:
                    info[key] = v
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
        return {
            'schema': SCHEMA_VERSION,
            'active': True,
            'ts': now,
            'self': self._build_self(),
            'ships': self._build_ships(),
            'damage': self._damage.snapshot(),
            'ballistics': _try(lambda: self._ballistics.snapshot(), {'available': False}),
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

    def _build_ships(self):
        ships = _try(lambda: battle.getAllShips(), []) or []
        out = []
        for ship in ships:
            entry = {}
            entry['uiId'] = _try(lambda: ship.uiId)
            # the entity id used by getPlayerByVehicleId varies by build; try a few
            vehId = None
            for getter in (lambda: ship._Ship__id, lambda: ship.id, lambda: ship.vehicleId):
                vehId = _try(getter)
                if vehId is not None:
                    break
            if vehId is not None:
                entry['vehicleId'] = vehId
            teamId = _try(lambda: ship.teamId)
            entry['teamId'] = teamId
            entry['relation'] = self._relation(teamId)
            entry['alive'] = _try(lambda: ship.isAlive())
            pos = _vec(_try(lambda: ship.getPosition()))
            if pos:
                entry['position'] = pos
                entry['visible'] = True
            else:
                entry['visible'] = False
            for k in ('yaw', 'health', 'maxHealth', 'name', 'shipType'):
                v = _try(lambda: getattr(ship, k))
                if v is not None:
                    entry[k] = v
            # link to player roster (name/type) by vehicle id
            player = _try(lambda: battle.getPlayerByVehicleId(vehId)) if vehId is not None else None
            if player is not None:
                entry['playerId'] = _try(lambda: player.id)
                if entry.get('teamId') is None:
                    entry['teamId'] = _try(lambda: player.teamId)
                    entry['relation'] = self._relation(entry['teamId'])
            out.append(entry)
        return out


def _is_mapping(o):
    try:
        return hasattr(o, 'keys') and hasattr(o, '__getitem__')
    except:
        return False


gCollector = Collector()
