# MIT License
# Copyright (c) 2026 Tz-WIND/Lex_q
#
# SPDX-License-Identifier: MIT

API_VERSION = 'API_v1.0'
MOD_NAME = 'WowsProbe'

# ---------------------------------------------------------------------------
# WowsProbe -- discovery mod for the "8111 for WoWS" extractor.
#
# Purpose: this mod does NOT collect data for the HTTP server. It is a one-off
# diagnostic that, on every battle start, dumps the *actual* ModsAPI surface
# available on YOUR World of Warships client build (method names, dataHub
# entity/component names, ship attributes, self-ship fields, map/minimap APIs)
# both to python.log and to a text file. Use its output to confirm/adjust the
# field names used by the real collector (PnFMods/WowsExtractor/Main.py).
#
# It also verifies that writing files from inside the game sandbox works and
# prints the absolute path it writes to (so you know where state.json will go).
# ---------------------------------------------------------------------------

try:
    import events, ui, utils, dataHub, constants, battle, callbacks
except:
    pass

# `os` is not always importable directly inside the sandbox; the well-known
# trick is to borrow it from a stdlib module that already imported it.
try:
    import os
except:
    try:
        import xml.sax.saxutils
        os = xml.sax.saxutils.os
    except:
        os = None


def logInfo(*args):
    try:
        utils.logInfo('[{}] {}'.format(MOD_NAME, ' '.join([_s(a) for a in args])))
    except:
        pass


def _s(v):
    try:
        return str(v)
    except:
        try:
            return repr(v)
        except:
            return '<unprintable>'


def _try(fn, default=None):
    try:
        return fn()
    except:
        return default


_UNSET_PROBE = object()


def _get(obj, name, default=None):
    try:
        return getattr(obj, name)
    except:
        return default


def _safe_call(fn):
    try:
        return repr(fn())
    except Exception, e:
        return '<error: {}>'.format(_s(e))


def _vec_probe(pos):
    if pos is None:
        return None
    try:
        return [float(pos.x), float(pos.y), float(pos.z)]
    except:
        pass
    try:
        return [float(pos[0]), float(pos[1]), float(pos[2])]
    except:
        return None


def _component_pos_probe(comp):
    """Mirror of the extractor's _component_pos: pull [x,y,z] from a position
    component, whether it is itself vector-like or wraps one under a sub-attr."""
    if comp is None:
        return None
    v = _vec_probe(comp)
    if v:
        return v
    for attr in ('position', 'worldPosition', 'point', 'value', 'pos', 'coords'):
        v = _vec_probe(_get(comp, attr))
        if v:
            return v
    return None


def _safe_dir(obj):
    try:
        return [a for a in dir(obj) if not a.startswith('__')]
    except:
        return []


def _describe(obj, prefix='  '):
    """Return a list of "name = value (type)" / "name : <callable>" lines."""
    lines = []
    for a in _safe_dir(obj):
        try:
            v = getattr(obj, a)
        except Exception, e:
            lines.append('{}{} : <getattr error: {}>'.format(prefix, a, _s(e)))
            continue
        try:
            if callable(v):
                lines.append('{}{} : <callable {}>'.format(prefix, a, type(v).__name__))
            else:
                s = repr(v)
                if len(s) > 240:
                    s = s[:240] + '...'
                lines.append('{}{} = {} ({})'.format(prefix, a, s, type(v).__name__))
        except Exception, e:
            lines.append('{}{} : <describe error: {}>'.format(prefix, a, _s(e)))
    return lines


# Known + guessed dataHub entity collection names to probe.
CANDIDATE_COLLECTIONS = [
    'shipBattleInfo', 'avatar', 'battleInfo', 'battleChatAndLog',
    'battleChatAndLogMessage', 'nearestEnemyIndication', 'alertIndication',
    'userPrefs', 'vehicle', 'ship', 'ownShip', 'player', 'players',
    'minimap', 'miniMap', 'arena', 'arenaInfo', 'space', 'spaceInfo',
    'score', 'battleScore', 'teamScore', 'progress', 'battleProgress',
    'timer', 'battleTimer', 'ribbon', 'frags', 'consumables', 'camera',
    'crosshair', 'aim', 'artilleryState', 'weaponState',
]

CANDIDATE_SINGLE = [
    'battleInfo', 'arena', 'arenaInfo', 'minimap', 'miniMap', 'space',
    'spaceInfo', 'score', 'battleScore', 'battleProgress', 'userPrefs',
    'nearestEnemyIndication', 'alertIndication', 'battleChatAndLog',
]


class Probe(object):
    def __init__(self):
        self._lines = []
        # spotted-enemy hunter state (find how to read minimap positions)
        self._captured = False
        self._dumpedKinds = set()
        self._huntTicks = 0
        self._lastHunt = 0.0
        self._huntHandle = None
        self._selfTeam = None
        events.onBattleStart(self._on_start)
        events.onBattleShown(self._on_shown)
        events.onBattleQuit(self._on_quit)
        logInfo('WowsProbe loaded. Will dump API surface on battle start.')

    # -- output helpers -----------------------------------------------------
    def _emit(self, line=''):
        self._lines.append(line)
        logInfo(line)

    def _flush(self):
        # `os` may be missing in the sandbox, but plain open() still works, so we
        # no longer bail on os is None (the previous behaviour suppressed the file).
        mod_dir = _try(lambda: utils.getModDir(), '.') or '.'
        path = mod_dir + '/probe_dump.txt'
        try:
            f = open(path, 'w')
            try:
                f.write('\n'.join([_s(l) for l in self._lines]))
            finally:
                f.close()
            ap = path
            if os is not None:
                ap = _try(lambda: os.path.abspath(path), path)
            logInfo('[probe] WROTE DUMP -> {}'.format(ap))
        except Exception, e:
            logInfo('[probe] FAILED to write dump file at {}: {}'.format(path, _s(e)))

    # -- sections -----------------------------------------------------------
    def _dump_modules(self):
        self._emit('==================== MODULE SURFACE ====================')
        for name, mod in (('battle', battle), ('ui', ui), ('utils', utils),
                          ('dataHub', dataHub), ('constants', constants),
                          ('events', events), ('callbacks', callbacks)):
            self._emit('---- dir({}) ----'.format(name))
            try:
                self._emit('  ' + ', '.join(_safe_dir(mod)))
            except Exception, e:
                self._emit('  <error: {}>'.format(_s(e)))

    def _dump_ui_components(self):
        self._emit('==================== constants.UiComponents ====================')
        try:
            cc = constants.UiComponents
            for a in _safe_dir(cc):
                try:
                    self._emit('  UiComponents.{} = {}'.format(a, _s(getattr(cc, a))))
                except Exception, e:
                    self._emit('  UiComponents.{} : <error {}>'.format(a, _s(e)))
        except Exception, e:
            self._emit('  <error: {}>'.format(_s(e)))

    def _dump_collections(self):
        self._emit('==================== dataHub collections ====================')
        cc = getattr(constants, 'UiComponents', None)
        for name in CANDIDATE_COLLECTIONS:
            try:
                coll = dataHub.getEntityCollections(name)
            except Exception, e:
                continue
            try:
                items = list(coll)
            except Exception:
                items = coll
            try:
                n = len(items)
            except Exception:
                n = '?'
            self._emit('[collection] {} -> {} entities'.format(name, n))
            # Dump the component of the first entity, if we can resolve it.
            try:
                comp_key = getattr(cc, name, None) if cc is not None else None
                if items and comp_key is not None:
                    comp = items[0][comp_key]
                    self._emit('  first {}[{}] attrs:'.format(name, name))
                    for line in _describe(comp, '    '):
                        self._emit(line)
            except Exception, e:
                self._emit('  <component dump error: {}>'.format(_s(e)))

    def _dump_singles(self):
        self._emit('==================== dataHub single entities ====================')
        cc = getattr(constants, 'UiComponents', None)
        for name in CANDIDATE_SINGLE:
            try:
                ent = dataHub.getSingleEntity(name)
            except Exception:
                continue
            if ent is None:
                continue
            self._emit('[single] {} present'.format(name))
            try:
                comp_key = getattr(cc, name, None) if cc is not None else None
                if comp_key is not None:
                    comp = ent[comp_key]
                    for line in _describe(comp, '    '):
                        self._emit(line)
            except Exception, e:
                self._emit('  <component dump error: {}>'.format(_s(e)))

    def _dump_self(self):
        self._emit('==================== self / player ====================')
        for label, fn in (
            ('battle.getSelfPlayerInfo()', lambda: battle.getSelfPlayerInfo()),
            ('battle.getSelfPlayer()', lambda: battle.getSelfPlayer()),
            ('battle.getSelfPlayerShip()', lambda: battle.getSelfPlayerShip()),
            ('battle.getObserverShip()', lambda: battle.getObserverShip()),
            ('battle.isObserverMode()', lambda: battle.isObserverMode()),
        ):
            try:
                v = fn()
            except Exception, e:
                self._emit('{} -> <error: {}>'.format(label, _s(e)))
                continue
            self._emit('{} -> {} ({})'.format(label, _s(v), type(v).__name__))
            if v is not None and not isinstance(v, (int, long, float, bool, str, unicode)):
                for line in _describe(v, '    '):
                    self._emit(line)

    def _probe_ship(self, ship, title):
        self._emit('-- {} --'.format(title))
        for line in _describe(ship, '    '):
            self._emit(line)
        for label, fn in (
            ('getPosition()', lambda: ship.getPosition()),
            ('teamId', lambda: ship.teamId),
            ('uiId', lambda: ship.uiId),
            ('isAlive()', lambda: ship.isAlive()),
            ('isVisible', lambda: ship.isVisible),
            ('isShipVisible', lambda: ship.isShipVisible),
            ('shipGameData', lambda: ship.shipGameData),
        ):
            try:
                self._emit('    {} = {}'.format(label, _s(fn())))
            except Exception, e:
                self._emit('    {} : <error {}>'.format(label, _s(e)))
        vehId = None
        for name in ('_Ship__id', 'id', 'vehicleId'):
            try:
                vehId = getattr(ship, name)
                if vehId is not None:
                    break
            except:
                pass
        if vehId is not None:
            try:
                player = battle.getPlayerByVehicleId(vehId)
                self._emit('    getPlayerByVehicleId({}) = {} ({})'.format(
                    vehId, _s(player), type(player).__name__))
                if player is not None:
                    pid = getattr(player, 'id', None)
                    if pid is not None:
                        for label, fn in (
                            ('getPlayerShipInfo({})'.format(pid),
                             lambda: battle.getPlayerShipInfo(pid)),
                            ('getPlayerInfo({})'.format(pid),
                             lambda: battle.getPlayerInfo(pid)),
                        ):
                            try:
                                self._emit('    {} = {}'.format(label, _s(fn())))
                            except Exception, e:
                                self._emit('    {} : <error {}>'.format(label, _s(e)))
            except Exception, e:
                self._emit('    getPlayerByVehicleId : <error {}>'.format(_s(e)))

    def _dump_ships(self):
        self._emit('==================== battle.getAllShips() ====================')
        try:
            ships = battle.getAllShips()
        except Exception, e:
            self._emit('battle.getAllShips() -> <error: {}>'.format(_s(e)))
            return
        try:
            n = len(ships)
        except Exception:
            n = '?'
        self._emit('getAllShips -> {} ships'.format(n))
        try:
            selfTeam = None
            try:
                selfTeam = battle.getSelfPlayerInfo().teamId
            except:
                pass
            first = None
            firstEnemy = None
            for s in ships:
                if first is None:
                    first = s
                try:
                    if selfTeam is not None and s.teamId != selfTeam and firstEnemy is None:
                        firstEnemy = s
                except:
                    pass
            if first is not None:
                self._probe_ship(first, 'first ship attributes')
            if firstEnemy is not None:
                self._probe_ship(firstEnemy, 'first ENEMY ship attributes')
            elif selfTeam is not None:
                self._emit('-- no enemy ship found in getAllShips() (selfTeam={}) --'.format(selfTeam))
        except Exception, e:
            self._emit('  <ship dump error: {}>'.format(_s(e)))

    def _dump_minimap(self):
        self._emit('==================== minimap / coords ====================')
        for label, fn in (
            ("ui.getLengthOnMiniMap(33.3)", lambda: ui.getLengthOnMiniMap(33.3)),
            ("battle.getArenaInfo()", lambda: battle.getArenaInfo()),
        ):
            try:
                self._emit('{} -> {}'.format(label, _s(fn())))
            except Exception, e:
                self._emit('{} : <error {}>'.format(label, _s(e)))

    # -- spotted-enemy hunter ----------------------------------------------
    # The hard case for telemetry is an enemy that is SPOTTED (drawn on the
    # in-game minimap because a teammate sees it) but NOT rendered in our own
    # 3D bubble: ship.getPosition() works only for rendered ships, so those
    # enemies have no position. This hunter polls every battle, finds such an
    # enemy, and dumps every candidate source of its position/visibility so we
    # can wire the real collector to the right API on THIS client build.
    def _ship_ids(self, ship):
        uiId = _get(ship, 'uiId')
        vehId = None
        for name in ('_Ship__id', 'id', 'vehicleId'):
            v = _get(ship, name)
            if v is not None:
                vehId = v
                break
        return uiId, vehId

    def _pid_for(self, vehId):
        if vehId is None:
            return None
        p = _try(lambda: battle.getPlayerByVehicleId(vehId))
        if p is None:
            return None
        return _get(p, 'id')

    def _ship_game_data(self, ship, pid):
        for getter in (
            lambda: ship.shipGameData,
            lambda: battle.getPlayerInfo(pid)['shipGameData'],
            lambda: battle.getPlayerInfo(pid).shipGameData,
        ):
            gd = _try(getter)
            if gd is not None:
                return gd
        return None

    def _gd_get(self, gd, key):
        if gd is None:
            return None
        v = _try(lambda: gd[key])
        if v is not None:
            return v
        return _try(lambda: getattr(gd, key))

    def _get_pos(self, ship):
        p = _try(lambda: ship.getPosition())
        if p is None:
            return None
        return _try(lambda: [float(p.x), float(p.y), float(p.z)])

    def _visibility(self, ship, gd):
        vis = self._gd_get(gd, 'isVisible')
        if vis is None:
            vis = _get(ship, 'isVisible')
        shipVis = self._gd_get(gd, 'isShipVisible')
        if shipVis is None:
            shipVis = _get(ship, 'isShipVisible')
        return vis, shipVis

    def _avatar_entity_for(self, pid, uiId):
        cc = getattr(constants, 'UiComponents', None)
        avKey = getattr(cc, 'avatar', None) if cc is not None else None
        if avKey is None:
            return None
        for ent in (_try(lambda: list(dataHub.getEntityCollections('avatar'))) or []):
            comp = _try(lambda e=ent: e[avKey])
            if comp is None:
                continue
            cid = _get(comp, 'playerId')
            if cid is None:
                cid = _get(comp, 'id')
            if cid is not None and cid == pid:
                return ent
        return None

    def _entity_comp_pos(self, pid, uiId, compName):
        ent = self._avatar_entity_for(pid, uiId)
        if ent is None:
            return None
        cc = getattr(constants, 'UiComponents', None)
        key = getattr(cc, compName, None) if cc is not None else None
        if key is None:
            return None
        return _component_pos_probe(_try(lambda e=ent, k=key: e[k]))

    def _dump_position_components(self, pid, uiId):
        # THE answer source: every ship entity carries worldPosition / mapPosition
        # / minimapMarker / mapProjection components. getPosition() is None for a
        # spotted-but-not-rendered enemy, but these components may still hold its
        # coordinates -- this is what the extractor fix now reads.
        self._emit('-- POSITION COMPONENTS (entity worldPosition/mapPosition/...) --')
        ent = self._avatar_entity_for(pid, uiId)
        if ent is None:
            self._emit('   (no avatar entity matched pid={} uiId={})'.format(pid, uiId))
            return
        cc = getattr(constants, 'UiComponents', None)
        for cname in ('worldPosition', 'mapPosition', 'minimapMarker',
                      'mapProjection', 'distance'):
            key = getattr(cc, cname, None) if cc is not None else None
            comp = _try(lambda e=ent, k=key: e[k]) if key is not None else None
            self._emit('   [{}] key={} type={} repr={}'.format(
                cname, key, type(comp).__name__, _safe_call(lambda c=comp: c)))
            self._emit('   [{}] extracted_vec = {}'.format(
                cname, _s(_component_pos_probe(comp))))
            if comp is not None:
                for line in _describe(comp, '       '):
                    self._emit(line)
                # expand the inner position Point (the actual coordinates source)
                sub = _get(comp, 'position')
                if sub is not None:
                    self._emit('       .position type={} x={} y={} z={} repr={}'.format(
                        type(sub).__name__, _s(_get(sub, 'x')), _s(_get(sub, 'y')),
                        _s(_get(sub, 'z')), _safe_call(lambda s=sub: s)))

    def _dump_calibration(self):
        # For rendered ships we have BOTH getPosition() (world) and the mapPosition
        # component, so these pairs reveal the minimap->world transform the real
        # extractor calibrates from. Dump a handful for verification.
        self._emit('-- CALIBRATION: getPosition(world) vs mapPosition.position --')
        cc = getattr(constants, 'UiComponents', None)
        mpKey = getattr(cc, 'mapPosition', None) if cc is not None else None
        ships = _try(lambda: battle.getAllShips(), []) or []
        n = 0
        for ship in ships:
            if n >= 8:
                break
            uiId, vehId = self._ship_ids(ship)
            pid = self._pid_for(vehId)
            wp = self._get_pos(ship)
            ent = self._avatar_entity_for(pid, uiId)
            mp = None
            yaw = None
            if ent is not None and mpKey is not None:
                comp = _try(lambda e=ent: e[mpKey])
                pt = _get(comp, 'position')
                mp = _try(lambda p=pt: [float(p.x), float(p.y)])
                yaw = _get(comp, 'yaw')
            self._emit('   team={} pid={} world={} map={} mapYaw={}'.format(
                _get(ship, 'teamId'), pid, _s(wp), _s(mp), _s(yaw)))
            n += 1

    def _scan_collections_for(self, uiId, vehId, pid):
        self._emit('-- dataHub scan for matching entity (uiId={}, vehId={}, pid={}) --'
                   .format(uiId, vehId, pid))
        cc = getattr(constants, 'UiComponents', None)
        names = list(CANDIDATE_COLLECTIONS)
        for a in _safe_dir(cc):           # also try every component key as a collection name
            if a not in names:
                names.append(a)
        wanted = set([x for x in (uiId, vehId, pid) if x is not None])
        for name in names:
            coll = _try(lambda n=name: dataHub.getEntityCollections(n))
            if coll is None:
                continue
            items = _try(lambda c=coll: list(c))
            if not items:
                continue
            comp_key = getattr(cc, name, None) if cc is not None else None
            if comp_key is None:
                continue
            for ent in items:
                comp = _try(lambda e=ent, k=comp_key: e[k])
                if comp is None:
                    continue
                refs = set()
                for idk in ('playerId', 'uiId', 'vehicleId', 'id', 'shipId'):
                    v = _get(comp, idk)
                    if v is not None:
                        refs.add(v)
                if not (refs & wanted):
                    continue
                self._emit('  [match] collection={} comp={}'.format(name, type(comp).__name__))
                for line in _describe(comp, '      '):
                    self._emit(line)
                for posk in ('position', 'worldPosition', 'point', 'coords', 'x', 'y', 'z'):
                    val = _get(comp, posk, _UNSET_PROBE)
                    if val is not _UNSET_PROBE:
                        self._emit('      {}.{} = {}'.format(name, posk, _s(val)))

    def _deep_dump_enemy(self, ship, kind):
        uiId, vehId = self._ship_ids(ship)
        pid = self._pid_for(vehId)
        self._emit('')
        self._emit('============== ENEMY DUMP [{}] tick={} =============='.format(kind, self._huntTicks))
        self._emit('ids: uiId={} vehId={} playerId={}'.format(uiId, vehId, pid))
        self._emit('-- dir(ship) values --')
        for line in _describe(ship, '    '):
            self._emit(line)
        self._emit('    getPosition() = {}'.format(_safe_call(lambda: ship.getPosition())))
        gd = self._ship_game_data(ship, pid)
        self._emit('-- shipGameData type={} repr={} --'.format(type(gd).__name__, _safe_call(lambda: gd)))
        for key in ('isVisible', 'isShipVisible', 'health', 'maxHealth', 'yaw', 'speed',
                    'position', 'worldPosition', 'x', 'y', 'z', 'coords', 'point'):
            self._emit('    gd[{}] = {}'.format(key, _s(self._gd_get(gd, key))))
        if gd is not None and hasattr(gd, 'keys'):
            self._emit('    gd.keys() = {}'.format(_safe_call(lambda: list(gd.keys()))))
        elif gd is not None:
            for line in _describe(gd, '    gd.'):
                self._emit(line)
        if pid is not None:
            for label, fn in (('getPlayerInfo', lambda: battle.getPlayerInfo(pid)),
                              ('getPlayerShipInfo', lambda: battle.getPlayerShipInfo(pid))):
                self._emit('-- battle.{}({}) = {} --'.format(label, pid, _safe_call(fn)))
                obj = _try(fn)
                if obj is not None and not isinstance(obj, (int, long, float, bool, str, unicode)):
                    for line in _describe(obj, '    '):
                        self._emit(line)
        self._dump_position_components(pid, uiId)
        self._scan_collections_for(uiId, vehId, pid)

    def _hunt(self, *args):
        if self._captured:
            return
        now = _try(lambda: utils.getTimeFromGameStart(), 0.0) or 0.0
        if (now - self._lastHunt) < 0.2:      # throttle: perTick fires every frame
            return
        self._lastHunt = now
        self._huntTicks += 1
        ships = _try(lambda: battle.getAllShips(), []) or []
        best = None
        bestKind = None
        for ship in ships:
            team = _get(ship, 'teamId')
            if self._selfTeam is not None and team == self._selfTeam:
                continue                       # skip self/allies
            if _try(lambda s=ship: s.isAlive()) is False:
                continue
            uiId, vehId = self._ship_ids(ship)
            pid = self._pid_for(vehId)
            pos = self._get_pos(ship)
            compPos = self._entity_comp_pos(pid, uiId, 'worldPosition')
            if compPos is None:
                compPos = self._entity_comp_pos(pid, uiId, 'mapPosition')
            # JACKPOT: no getPosition() (the symptom: spotted-but-not-rendered)
            # yet the entity position component DOES expose coords -> this proves
            # the extractor fix reads a valid source. Capture and stop.
            if pos is None and compPos is not None:
                best, bestKind = ship, 'COMPONENT-POS-NO-GETPOS'
                break
            if pos is None:
                if bestKind is None:
                    best, bestKind = ship, 'ALIVE-NO-POSITION'
                continue                       # keep hunting for the jackpot
            if best is None:
                gd = self._ship_game_data(ship, pid)
                vis, _shipVis = self._visibility(ship, gd)
                best, bestKind = ship, ('SPOTTED-AND-RENDERED' if vis else 'RENDERED')
        if best is not None and bestKind not in self._dumpedKinds:
            self._dumpedKinds.add(bestKind)
            self._deep_dump_enemy(best, bestKind)
            if bestKind == 'COMPONENT-POS-NO-GETPOS':
                self._captured = True          # proven -- stop; otherwise keep hunting
                self._stop_hunt()
            self._flush()
        if (self._huntTicks % 25) == 1:
            self._emit('[hunt] tick={} captured={} kinds={}'
                       .format(self._huntTicks, self._captured, sorted(self._dumpedKinds)))
            self._flush()

    def _start_hunt(self):
        self._stop_hunt()
        self._captured = False
        self._dumpedKinds = set()
        self._huntTicks = 0
        self._lastHunt = 0.0
        self._selfTeam = _try(lambda: battle.getSelfPlayerInfo().teamId)
        self._huntHandle = _try(lambda: callbacks.perTick(self._hunt))
        self._emit('[hunt] started (selfTeam={}, handle={})'.format(self._selfTeam, self._huntHandle))
        self._flush()

    def _stop_hunt(self):
        if self._huntHandle is not None:
            _try(lambda: callbacks.cancel(self._huntHandle))
            self._huntHandle = None

    # -- entry points -------------------------------------------------------
    def _on_start(self, *args):
        self._lines = []
        self._emit('############### WowsProbe battle start dump ###############')
        for step in (self._dump_modules, self._dump_ui_components,
                     self._dump_singles, self._dump_collections,
                     self._dump_self, self._dump_ships, self._dump_minimap):
            try:
                step()
            except Exception, e:
                self._emit('[probe] step {} failed: {}'.format(step.__name__, _s(e)))
        self._flush()

    def _on_shown(self, *args):
        # Some entities are only ready once the battle UI is shown -- dump again.
        self._emit('############### WowsProbe battle shown re-dump ###############')
        for step in (self._dump_singles, self._dump_collections,
                     self._dump_self, self._dump_ships, self._dump_minimap):
            try:
                step()
            except Exception, e:
                self._emit('[probe] step {} failed: {}'.format(step.__name__, _s(e)))
        self._flush()
        # Start polling for a spotted-but-not-rendered enemy (the key unknown).
        self._start_hunt()
        _try(lambda: self._dump_calibration())
        self._flush()

    def _on_quit(self, *args):
        self._stop_hunt()
        self._emit('############### WowsProbe battle quit ###############')
        self._emit('[hunt] final: captured={} kinds={} ticks={}'
                   .format(self._captured, sorted(self._dumpedKinds), self._huntTicks))
        self._flush()


gProbe = Probe()
