# MIT License
"""Accumulate inflicted / received / team damage from ModsAPI packets.

Kept free of game APIs so kill-salvo attribution can be regression-tested
outside the WoWS sandbox (Python 2 and 3).

`events.onReceiveDamagesOnShip` delivers `(victimVehicleId, damages)`.
`battle.getPlayerByVehicleId` often returns None on the killing blow
because the vehicle is already despawned. Dropping the batch in that
case is exactly how a Devastating Strike's high damage vanishes from
the running total: the ship was alive (and cached) a tick earlier.

A second failure mode: secondaries and main battery hitting *different*
ships in the same frame. The client reuses the `damages` list and may
also drop one of the two vehicle packets. Freeze the packet before any
lookup, and merge shell info so a lost main-battery salvo still counts.
Later fire/flood on that target is added on top of the fill, not
swallowed by max().

A third failure mode: the victim is currently unspotted (灭点 -- last
seen on the minimap, still alive). `onReceiveDamagesOnShip` often never
fires because the Vehicle entity is not loaded, and `onReceiveShellInfo`
arrives with victimId 0 / -1. Dropping that packet is how dark-target
salvos vanish from the running total even though the in-game ribbon
moved. Count the shell against the shooter anyway, and attribute the
victim from a last-seen ghost when we can.
"""

# ModsAPI sentinel: victim/shooter id when the ship is not currently
# loaded (灭点, scenario structures, etc.). Not a real vehicle id.
UNKNOWN_VICTIM = 0
GHOST_MATCH_MAX_DIST = 2500.0


def _get_field(obj, *names):
    """Read the first present attribute or mapping key; 0 is a valid value."""
    if obj is None:
        return None
    for name in names:
        try:
            value = obj[name]
        except Exception:
            try:
                value = getattr(obj, name)
            except Exception:
                continue
        if value is not None:
            return value
    return None


def _as_id(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except Exception:
        return None
    if number != number:  # NaN
        return None
    try:
        return int(number)
    except Exception:
        return None


def extract_vehicle_id(value):
    """Coerce a damage-event victim/attacker identifier to a vehicle id.

    ModsAPI usually passes a raw int. After a kill it may instead pass the
    leftover vehicle entity (``.id``) or we may have cached a PlayerInfo
    (``.shipId`` is the vehicle, ``.id`` is the player -- do not confuse).
    0 / -1 are sentinels for "not currently loaded" (灭点), not ids.
    """
    vid = _as_id(value)
    if vid is not None:
        return vid if vid > 0 else None
    ship = _get_field(value, 'shipId', 'vehicleId', 'vehicleID')
    vid = _as_id(ship)
    if vid is not None:
        return vid if vid > 0 else None
    vid = _as_id(_get_field(value, 'id', '_Ship__id'))
    if vid is not None:
        return vid if vid > 0 else None
    return None


def extract_attacker_vehicle_id(entry):
    return extract_vehicle_id(_get_field(
        entry, 'vehicleID', 'vehicleId', 'aggressorID', 'aggressorId',
        'shooterId'))


def extract_damage(entry):
    raw = _get_field(entry, 'damage', 'Damage')
    if raw is None or isinstance(raw, bool):
        return None
    try:
        dmg = float(raw)
    except Exception:
        return None
    if dmg != dmg or dmg <= 0:
        return None
    try:
        as_int = int(dmg)
        if as_int == dmg:
            return as_int
    except Exception:
        pass
    return dmg


def player_ids_from(player):
    """Return (vehicle_id, player_id, team_id) from a PlayerInfo / dict / None."""
    if player is None:
        return None
    player_id = _as_id(_get_field(player, 'playerId', 'id'))
    vehicle_id = _as_id(_get_field(player, 'shipId', 'vehicleId', 'vehicleID'))
    team_id = _get_field(player, 'teamId')
    if player_id is None or vehicle_id is None:
        return None
    return (vehicle_id, player_id, team_id)


def roster_ids_from_avatar(avatar, vehicle=None):
    """Ids for the damage vehicle cache from a dataHub avatar entity.

    Use ``avatar.playerId`` only. ``avatar.id`` is a different alias
    (ui_health indexes both); writing it as the damage-table player key
    desyncs /damage from roster.
    """
    pid = _as_id(_get_field(avatar, 'playerId'))
    veh = None
    if vehicle is not None:
        veh = _as_id(_get_field(vehicle, 'id'))
    if veh is None:
        veh = _as_id(_get_field(avatar, 'shipId'))
    team = _get_field(avatar, 'teamId')
    if pid is None or veh is None:
        return None
    return (veh, pid, team)


def merge_ship_and_shell(ship, shell):
    """Combine HP packets with shell-info gap fills for one attacker/victim.

    Ship packets are HP (impact + fire/flood). Shell info is impact only.
    When ship has caught up to shell, it already includes the salvo (and
    any later DoT) -- use ship so we do not double-count. When ship is
    still behind, the ship total is extra DoT on a missed salvo, not a
    subset of the shells -- add them. ``max(ship, shell)`` would swallow
    that DoT.
    """
    try:
        ship = ship or 0
        shell = shell or 0
    except Exception:
        return 0
    if ship >= shell:
        return ship
    return ship + shell


def _xz(pos):
    """Horizontal (x, z) from a Vector3, [x,y,z] list, or None."""
    if pos is None:
        return None
    try:
        x = float(pos[0])
        z = float(pos[2]) if len(pos) > 2 else float(pos[1])
        return (x, z)
    except Exception:
        pass
    try:
        return (float(pos.x), float(pos.z))
    except Exception:
        return None


def guess_ghost_victim(last_seen_entries, shot_position=None, max_dist=None):
    """Pick a last-seen (灭点) enemy as the shell victim when ModsAPI omits the id.

    ``last_seen_entries`` is an iterable of collector last-seen records:
    ``{identity: {vehicleId, relation, ...}, pos: [x,y,z]}``. Allies
    (relation==1) are ignored. With a shot impact, the nearest enemy
    within ``max_dist`` metres wins; without one, a single remaining
    enemy ghost is used.
    """
    if max_dist is None:
        max_dist = GHOST_MATCH_MAX_DIST
    enemies = []
    for seen in last_seen_entries or []:
        ident = None
        pos = None
        if isinstance(seen, dict):
            ident = seen.get('identity') or seen
            pos = seen.get('pos')
        if not isinstance(ident, dict):
            continue
        if ident.get('relation') == 1:
            continue
        vid = extract_vehicle_id(ident.get('vehicleId'))
        if vid is None:
            vid = extract_vehicle_id(ident.get('vehicleID'))
        if vid is None:
            continue
        enemies.append((vid, pos))
    if not enemies:
        return None
    shot = _xz(shot_position)
    if shot is None:
        if len(enemies) == 1:
            return enemies[0][0]
        return None
    best = None
    best_d = None
    max_d2 = max_dist * max_dist
    for vid, pos in enemies:
        xz = _xz(pos)
        if xz is None:
            continue
        dx = xz[0] - shot[0]
        dz = xz[1] - shot[1]
        d2 = dx * dx + dz * dz
        if d2 > max_d2:
            continue
        if best_d is None or d2 < best_d:
            best_d = d2
            best = vid
    if best is not None:
        return best
    if len(enemies) == 1:
        return enemies[0][0]
    return None


def freeze_damage_packet(victim_vehicle_id, damages):
    """Copy victim id + damage primitives before any live lookup.

    The client reuses the `damages` list across onReceiveDamagesOnShip
    callbacks. A re-entrant packet (secondaries vs a different target in
    the same frame) will mutate that list; reading it after a lookup
    would attribute the wrong hits or drop the first salvo entirely.
    """
    victim_vid = extract_vehicle_id(victim_vehicle_id)
    entries = []
    try:
        iterable = list(damages or [])
    except Exception:
        iterable = []
    for entry in iterable:
        dmg = extract_damage(entry)
        if dmg is None:
            continue
        entries.append({
            'vehicleID': extract_attacker_vehicle_id(entry),
            'damage': dmg,
        })
    return victim_vid, entries


class DamageTracker(object):
    def __init__(self, resolve=None):
        # resolve(vehicle_id) -> (player_id, team_id) or None. Optional live
        # lookup (battle.getPlayerByVehicleId); the cache is consulted first.
        self._resolve = resolve
        self.clear()

    def clear(self):
        self._inflicted = {}   # attackerPlayerId -> {'total':x, 'byVictim':{vid:dmg}}
        self._received = {}    # victimPlayerId   -> {'total':x, 'byAttacker':{aid:dmg}}
        self._teamTotal = {}   # teamId -> total (ship packets only; snapshot merges)
        self._vehicles = {}    # vehicleId -> (playerId, teamId)
        self._player_team = {}  # playerId -> teamId
        self._shell_inflicted = {}  # attackerPlayerId -> {victimPlayerId: dmg}

    def remember(self, vehicle_id, player_id, team_id=None):
        vid = extract_vehicle_id(vehicle_id)
        if vid is None:
            return
        prev = self._vehicles.get(vid)
        if prev is not None:
            keep_pid, keep_team = prev
            if keep_team is None and team_id is not None:
                keep_team = team_id
            self._vehicles[vid] = (keep_pid, keep_team)
            if keep_team is not None:
                self._player_team[keep_pid] = keep_team
            return
        pid = _as_id(player_id)
        if pid is None:
            return
        self._vehicles[vid] = (pid, team_id)
        if team_id is not None:
            self._player_team[pid] = team_id

    def remember_player(self, player):
        rec = player_ids_from(player)
        if rec is None:
            return False
        self.remember(rec[0], rec[1], rec[2])
        return True

    def lookup(self, vehicle_id, resolve=None):
        vid = extract_vehicle_id(vehicle_id)
        if vid is None:
            return None
        cached = self._vehicles.get(vid)
        if cached is not None:
            return cached
        # Some callbacks pass playerId where a vehicleId is expected.
        for rec in self._vehicles.values():
            if rec[0] == vid:
                return rec
        resolver = resolve if resolve is not None else self._resolve
        if resolver is None:
            return None
        try:
            info = resolver(vid)
        except Exception:
            info = None
        if not info:
            return None
        player_id, team_id = info[0], info[1]
        self.remember(vid, player_id, team_id)
        cached = self._vehicles.get(vid)
        return cached

    def _resolve_victim(self, victim_vehicle_id, resolve=None, fallback_victim=None):
        victim = self.lookup(victim_vehicle_id, resolve)
        if victim is not None:
            return victim
        if fallback_victim is None:
            return None
        return self.lookup(fallback_victim, resolve)

    def on_damages(self, victim_vehicle_id, damages, resolve=None,
                   fallback_victim=None):
        victim_vid, frozen = freeze_damage_packet(victim_vehicle_id, damages)
        victim = self._resolve_victim(victim_vid, resolve, fallback_victim)
        if victim is None:
            return
        victim_id = victim[0]
        for entry in frozen:
            dmg = entry.get('damage')
            if dmg is None:
                continue
            attacker_vid = entry.get('vehicleID')
            attacker = self.lookup(attacker_vid, resolve)
            if attacker is None:
                continue
            attacker_id, team_id = attacker
            inf = self._inflicted.setdefault(
                attacker_id, {'total': 0, 'byVictim': {}})
            inf['total'] += dmg
            inf['byVictim'][victim_id] = inf['byVictim'].get(victim_id, 0) + dmg
            rec = self._received.setdefault(
                victim_id, {'total': 0, 'byAttacker': {}})
            rec['total'] += dmg
            rec['byAttacker'][attacker_id] = (
                rec['byAttacker'].get(attacker_id, 0) + dmg)
            if team_id is not None:
                self._teamTotal[team_id] = self._teamTotal.get(team_id, 0) + dmg
                self._player_team[attacker_id] = team_id

    def on_shell(self, victim_vehicle_id, shooter_vehicle_id, damage,
                 resolve=None, fallback_victim=None):
        """Per-shell hit from onReceiveShellInfo (own shots, both guns).

        Used as a second ledger so a main-battery salvo is not lost when
        the vehicle HP callback keeps only the simultaneous secondary
        packet. snapshot() fills the hole from shells, then adds later
        ship-packet DoT instead of taking max() of the two running totals.

        When the victim is currently unspotted, ModsAPI often passes
        victimId 0. Count the shell against the shooter anyway; pass
        ``fallback_victim`` (a last-seen ghost id) to name the target.
        """
        dmg = extract_damage({'damage': damage})
        if dmg is None:
            return
        attacker = self.lookup(shooter_vehicle_id, resolve)
        if attacker is None:
            return
        victim = self._resolve_victim(victim_vehicle_id, resolve, fallback_victim)
        attacker_id, team_id = attacker
        victim_id = victim[0] if victim is not None else UNKNOWN_VICTIM
        bucket = self._shell_inflicted.setdefault(attacker_id, {})
        bucket[victim_id] = bucket.get(victim_id, 0) + dmg
        if team_id is not None:
            self._player_team[attacker_id] = team_id

    def _team_for_player(self, player_id):
        team = self._player_team.get(player_id)
        if team is not None:
            return team
        for pid, team_id in self._vehicles.values():
            if pid == player_id:
                return team_id
        return None

    def _merged_pairs(self):
        ship_map = {}
        shell_map = {}
        keys = set()
        for aid, info in self._inflicted.items():
            for vid, dmg in info['byVictim'].items():
                key = (aid, vid)
                ship_map[key] = dmg
                keys.add(key)
        for aid, victims in self._shell_inflicted.items():
            for vid, dmg in victims.items():
                key = (aid, vid)
                shell_map[key] = dmg
                keys.add(key)
        pairs = {}
        unknown_by_attacker = {}
        for key in keys:
            aid, vid = key
            if vid == UNKNOWN_VICTIM:
                unknown_by_attacker[aid] = (
                    unknown_by_attacker.get(aid, 0) + shell_map.get(key, 0))
                continue
            pairs[key] = merge_ship_and_shell(
                ship_map.get(key, 0), shell_map.get(key, 0))
        # 灭点 shells (victimId=0) later named by an HP packet: if the ship
        # ledger already has at least that much, the unknown fill is the
        # same salvo -- drop it. Otherwise keep it as unattributed extra.
        for aid, unknown in unknown_by_attacker.items():
            if not unknown:
                continue
            identified_ship = 0
            for (a, _v), dmg in ship_map.items():
                if a == aid:
                    identified_ship += dmg
            if identified_ship >= unknown:
                continue
            pairs[(aid, UNKNOWN_VICTIM)] = unknown
        return pairs

    def snapshot(self):
        # stringify int keys so they survive JSON object keys cleanly
        def keymap(d):
            return dict((str(k), v) for k, v in d.items())
        inflicted = {}
        received = {}
        team_total = {}
        for (aid, vid), dmg in self._merged_pairs().items():
            inf = inflicted.setdefault(aid, {'total': 0, 'byVictim': {}})
            inf['total'] += dmg
            team = self._team_for_player(aid)
            if team is not None:
                team_total[team] = team_total.get(team, 0) + dmg
            if vid == UNKNOWN_VICTIM:
                continue
            inf['byVictim'][vid] = inf['byVictim'].get(vid, 0) + dmg
            rec = received.setdefault(vid, {'total': 0, 'byAttacker': {}})
            rec['total'] += dmg
            rec['byAttacker'][aid] = rec['byAttacker'].get(aid, 0) + dmg
        out_inf = {}
        for aid, info in inflicted.items():
            out_inf[str(aid)] = {
                'total': info['total'],
                'byVictim': keymap(info['byVictim']),
            }
        out_rec = {}
        for vid, info in received.items():
            out_rec[str(vid)] = {
                'total': info['total'],
                'byAttacker': keymap(info['byAttacker']),
            }
        return {
            'inflicted': out_inf,
            'received': out_rec,
            'teamTotal': keymap(team_total),
        }
