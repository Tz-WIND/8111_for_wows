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
"""


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
    """
    vid = _as_id(value)
    if vid is not None:
        return vid
    ship = _get_field(value, 'shipId', 'vehicleId', 'vehicleID')
    vid = _as_id(ship)
    if vid is not None:
        return vid
    return _as_id(_get_field(value, 'id', '_Ship__id'))


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

    def on_damages(self, victim_vehicle_id, damages, resolve=None):
        victim_vid, frozen = freeze_damage_packet(victim_vehicle_id, damages)
        victim = self.lookup(victim_vid, resolve)
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

    def on_shell(self, victim_vehicle_id, shooter_vehicle_id, damage, resolve=None):
        """Per-shell hit from onReceiveShellInfo (own shots, both guns).

        Used as a second ledger so a main-battery salvo is not lost when
        the vehicle HP callback keeps only the simultaneous secondary
        packet. snapshot() fills the hole from shells, then adds later
        ship-packet DoT instead of taking max() of the two running totals.
        """
        dmg = extract_damage({'damage': damage})
        if dmg is None:
            return
        victim = self.lookup(victim_vehicle_id, resolve)
        attacker = self.lookup(shooter_vehicle_id, resolve)
        if victim is None or attacker is None:
            return
        attacker_id, team_id = attacker
        victim_id = victim[0]
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
        for key in keys:
            pairs[key] = merge_ship_and_shell(
                ship_map.get(key, 0), shell_map.get(key, 0))
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
            inf['byVictim'][vid] = inf['byVictim'].get(vid, 0) + dmg
            rec = received.setdefault(vid, {'total': 0, 'byAttacker': {}})
            rec['total'] += dmg
            rec['byAttacker'][aid] = rec['byAttacker'].get(aid, 0) + dmg
            team = self._team_for_player(aid)
            if team is not None:
                team_total[team] = team_total.get(team, 0) + dmg
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
