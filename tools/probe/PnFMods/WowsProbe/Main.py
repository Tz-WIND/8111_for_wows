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
        events.onBattleStart(self._on_start)
        events.onBattleShown(self._on_shown)
        logInfo('WowsProbe loaded. Will dump API surface on battle start.')

    # -- output helpers -----------------------------------------------------
    def _emit(self, line=''):
        self._lines.append(line)
        logInfo(line)

    def _flush(self):
        if os is None:
            self._emit('[probe] os unavailable -- cannot write dump file.')
            return
        try:
            mod_dir = utils.getModDir()
        except Exception, e:
            self._emit('[probe] utils.getModDir() failed: {}'.format(_s(e)))
            return
        path = mod_dir + '/probe_dump.txt'
        try:
            f = open(path, 'w')
            try:
                f.write('\n'.join([_s(l) for l in self._lines]))
            finally:
                f.close()
            logInfo('[probe] WROTE DUMP -> {}'.format(os.path.abspath(path)))
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
            first = None
            for s in ships:
                first = s
                break
            if first is not None:
                self._emit('-- first ship attributes --')
                for line in _describe(first, '    '):
                    self._emit(line)
                # probe common accessors explicitly
                for label, fn in (
                    ('getPosition()', lambda: first.getPosition()),
                    ('teamId', lambda: first.teamId),
                    ('uiId', lambda: first.uiId),
                    ('isAlive()', lambda: first.isAlive()),
                ):
                    try:
                        self._emit('    {} = {}'.format(label, _s(fn())))
                    except Exception, e:
                        self._emit('    {} : <error {}>'.format(label, _s(e)))
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


gProbe = Probe()
