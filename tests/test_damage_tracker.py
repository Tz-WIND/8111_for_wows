# MIT License -- damage accumulation must survive a killing blow.
import os
import sys

MOD_DIR = os.path.join(
    os.path.dirname(__file__), "..", "mod", "PnFMods", "WowsExtractor")
sys.path.insert(0, MOD_DIR)

import damage_tracker as D  # noqa: E402


class _Player(object):
    def __init__(self, player_id, team_id, ship_id):
        self.id = player_id
        self.teamId = team_id
        self.shipId = ship_id


def _tracker(live=None):
    return D.DamageTracker(resolve=live)


def test_kill_salvo_counts_when_live_lookup_misses_cached_victim():
    """Devastating Strike: the victim vehicle is often already gone from
    battle.getPlayerByVehicleId by the time onReceiveDamagesOnShip fires.
    A roster/ship cache built while they were alive must still attribute
    the batch -- otherwise the whole high-damage packet is dropped."""
    tracker = _tracker(live=lambda _vid: None)
    tracker.remember(77, player_id=901, team_id=1)
    tracker.remember(1, player_id=537, team_id=0)

    tracker.on_damages(77, [{'vehicleID': 1, 'damage': 97200}])

    snap = tracker.snapshot()
    assert snap['inflicted']['537']['total'] == 97200
    assert snap['inflicted']['537']['byVictim']['901'] == 97200
    assert snap['received']['901']['total'] == 97200
    assert snap['teamTotal']['0'] == 97200


def test_unknown_vehicle_is_still_dropped_without_a_cache_entry():
    tracker = _tracker(live=lambda _vid: None)
    tracker.on_damages(77, [{'vehicleID': 1, 'damage': 97200}])
    assert tracker.snapshot() == {
        'inflicted': {}, 'received': {}, 'teamTotal': {},
    }


def test_successful_live_lookup_is_cached_for_the_next_miss():
    seen = []

    def resolve(vid):
        seen.append(vid)
        if vid == 77:
            return (901, 1)
        if vid == 1:
            return (537, 0)
        return None

    tracker = _tracker(live=resolve)
    tracker.on_damages(77, [{'vehicleID': 1, 'damage': 1200}])
    tracker._resolve = lambda _vid: None
    tracker.on_damages(77, [{'vehicleID': 1, 'damage': 88000}])
    assert tracker.snapshot()['inflicted']['537']['total'] == 89200
    assert seen == [77, 1]


def test_object_damage_entries_and_vehicleId_alias_count():
    tracker = _tracker(live=lambda _vid: None)
    tracker.remember(77, 901, 1)
    tracker.remember(1, 537, 0)

    class _Hit(object):
        def __init__(self):
            self.vehicleId = 1
            self.damage = 4100

    tracker.on_damages(77, [_Hit(), {'vehicleId': 1, 'damage': 900}])
    assert tracker.snapshot()['inflicted']['537']['total'] == 5000


def test_remember_player_uses_shipId_not_player_id_as_vehicle():
    tracker = _tracker(live=lambda _vid: None)
    tracker.remember_player(_Player(901, 1, 77))
    tracker.remember_player(_Player(537, 0, 1))
    tracker.on_damages(77, [{'vehicleID': 1, 'damage': 500}])
    assert tracker.snapshot()['received']['901']['total'] == 500


def test_victim_vehicle_entity_id_resolves_through_cache():
    tracker = _tracker(live=lambda _vid: None)
    tracker.remember(77, 901, 1)
    tracker.remember(1, 537, 0)

    class _Vehicle(object):
        def __init__(self, vid):
            self.id = vid

    tracker.on_damages(_Vehicle(77), [{'vehicleID': 1, 'damage': 100}])
    assert tracker.snapshot()['received']['901']['total'] == 100


def test_zero_and_missing_damage_are_skipped():
    tracker = _tracker(live=lambda _vid: None)
    tracker.remember(77, 901, 1)
    tracker.remember(1, 537, 0)
    tracker.on_damages(77, [
        {'vehicleID': 1, 'damage': 0},
        {'vehicleID': 1},
        {'vehicleID': 1, 'damage': 50},
    ])
    assert tracker.snapshot()['inflicted']['537']['total'] == 50


def test_clear_wipes_totals_and_vehicle_cache():
    tracker = _tracker(live=lambda _vid: None)
    tracker.remember(77, 901, 1)
    tracker.remember(1, 537, 0)
    tracker.on_damages(77, [{'vehicleID': 1, 'damage': 10}])
    tracker.on_shell(77, 1, 10)
    tracker.clear()
    tracker.on_damages(77, [{'vehicleID': 1, 'damage': 10}])
    tracker.on_shell(77, 1, 10)
    assert tracker.snapshot() == {
        'inflicted': {}, 'received': {}, 'teamTotal': {},
    }


def test_reentrant_secondary_packet_must_not_eat_main_battery_hit():
    """Secondaries and main battery hitting different ships in one frame.

    ModsAPI often reuses one `damages` list. A live vehicle lookup can
    re-enter the callback and overwrite that list with the secondary
    packet before the main-battery entries are read -- the overlay then
    misses the main hit. Unstable because it depends on packet order.
    """
    shared = [{'vehicleID': 1, 'damage': 18400}]  # main battery vs target 88

    def resolve(vid):
        if vid == 88:
            shared[:] = [{'vehicleID': 1, 'damage': 350}]  # ATBA vs target 77
            tracker.on_damages(77, shared)
            return (902, 1)
        return None

    tracker = _tracker(live=resolve)
    tracker.remember(77, 901, 1)
    tracker.remember(1, 537, 0)
    tracker.on_damages(88, shared)

    snap = tracker.snapshot()
    assert snap['inflicted']['537']['byVictim']['902'] == 18400
    assert snap['inflicted']['537']['byVictim']['901'] == 350
    assert snap['inflicted']['537']['total'] == 18750


def test_shell_info_fills_main_battery_hit_that_never_arrived():
    """If the vehicle damage callback coalesces two same-tick packets and
    keeps only the secondary target, onReceiveShellInfo still has the
    main-battery shells and must fill the hole without waiting for HP."""
    tracker = _tracker(live=lambda _vid: None)
    tracker.remember(77, 901, 1)
    tracker.remember(88, 902, 1)
    tracker.remember(1, 537, 0)
    tracker.on_damages(77, [{'vehicleID': 1, 'damage': 350}])
    tracker.on_shell(88, 1, 18400)
    snap = tracker.snapshot()
    assert snap['inflicted']['537']['byVictim']['901'] == 350
    assert snap['inflicted']['537']['byVictim']['902'] == 18400
    assert snap['inflicted']['537']['total'] == 18750
    assert snap['teamTotal']['0'] == 18750


def test_shell_info_does_not_double_count_when_ship_packet_already_landed():
    tracker = _tracker(live=lambda _vid: None)
    tracker.remember(88, 902, 1)
    tracker.remember(1, 537, 0)
    tracker.on_damages(88, [{'vehicleID': 1, 'damage': 18400}])
    tracker.on_shell(88, 1, 18400)
    assert tracker.snapshot()['inflicted']['537']['total'] == 18400


def test_shell_fill_then_fire_dot_is_added_on_top():
    """Ship packet missed the salvo (0), shell info fills 18400, then
    fire ticks +3000. max(ship, shell) would stay 18400 and swallow the
    DoT; the ship total is extra HP damage, not a subset of the shells."""
    tracker = _tracker(live=lambda _vid: None)
    tracker.remember(88, 902, 1)
    tracker.remember(1, 537, 0)
    tracker.on_shell(88, 1, 18400)
    assert tracker.snapshot()['inflicted']['537']['byVictim']['902'] == 18400
    tracker.on_damages(88, [{'vehicleID': 1, 'damage': 3000}])
    snap = tracker.snapshot()
    assert snap['inflicted']['537']['byVictim']['902'] == 21400
    assert snap['inflicted']['537']['total'] == 21400
    assert snap['teamTotal']['0'] == 21400


def test_delayed_ship_packet_does_not_double_count_after_shell_fill():
    tracker = _tracker(live=lambda _vid: None)
    tracker.remember(88, 902, 1)
    tracker.remember(1, 537, 0)
    tracker.on_shell(88, 1, 18400)
    tracker.on_damages(88, [{'vehicleID': 1, 'damage': 18400}])
    assert tracker.snapshot()['inflicted']['537']['total'] == 18400


def test_ship_total_that_already_includes_dot_wins_once_it_catches_up():
    tracker = _tracker(live=lambda _vid: None)
    tracker.remember(88, 902, 1)
    tracker.remember(1, 537, 0)
    tracker.on_damages(88, [{'vehicleID': 1, 'damage': 18400}])
    tracker.on_shell(88, 1, 18400)
    tracker.on_damages(88, [{'vehicleID': 1, 'damage': 3000}])
    assert tracker.snapshot()['inflicted']['537']['byVictim']['902'] == 21400


def test_remember_keeps_existing_player_id_and_only_fills_missing_team():
    tracker = _tracker(live=lambda _vid: None)
    tracker.remember(77, 901, None)
    tracker.remember(77, 268369920, 1)
    assert tracker.lookup(77) == (901, 1)


def test_remember_does_not_replace_a_complete_mapping():
    tracker = _tracker(live=lambda _vid: None)
    tracker.remember(77, 901, 1)
    tracker.remember(77, 268369920, 0)
    assert tracker.lookup(77) == (901, 1)


def test_roster_ids_from_avatar_never_use_avatar_id_as_player_id():
    class _Avatar(object):
        def __init__(self, player_id, alias_id, team_id=1):
            self.playerId = player_id
            self.id = alias_id
            self.teamId = team_id

    class _Vehicle(object):
        def __init__(self, vid):
            self.id = vid

    assert D.roster_ids_from_avatar(
        _Avatar(None, 268369920), _Vehicle(77)) is None
    assert D.roster_ids_from_avatar(
        _Avatar(901, 268369920), _Vehicle(77)) == (77, 901, 1)


def test_sentinel_vehicle_ids_are_not_real_entities():
    """ModsAPI uses 0 / -1 when the victim is currently unspotted (灭点)."""
    assert D.extract_vehicle_id(0) is None
    assert D.extract_vehicle_id(-1) is None
    assert D.extract_vehicle_id({'vehicleId': 0}) is None


def test_shell_hit_on_unspotted_victim_still_counts_with_fallback():
    """Hitting a 灭点: onReceiveShellInfo fires with victimId=0, but the
    last-seen ghost still tells us who we shot. That damage must land in
    the running total -- otherwise every dark-target salvo is silently
    dropped even though the in-game ribbon and damage counter move."""
    tracker = _tracker(live=lambda _vid: None)
    tracker.remember(77, 901, 1)
    tracker.remember(1, 537, 0)

    tracker.on_shell(0, 1, 18400, fallback_victim=77)

    snap = tracker.snapshot()
    assert snap['inflicted']['537']['total'] == 18400
    assert snap['inflicted']['537']['byVictim']['901'] == 18400
    assert snap['received']['901']['total'] == 18400
    assert snap['teamTotal']['0'] == 18400


def test_shell_hit_on_unspotted_victim_counts_total_without_fallback():
    """Even if we cannot name the dark target, the shooter's inflicted
    total (and team total) must still move. byVictim/received stay empty
    rather than inventing a ship."""
    tracker = _tracker(live=lambda _vid: None)
    tracker.remember(1, 537, 0)

    tracker.on_shell(0, 1, 4200)

    snap = tracker.snapshot()
    assert snap['inflicted']['537']['total'] == 4200
    assert snap['inflicted']['537']['byVictim'] == {}
    assert snap['received'] == {}
    assert snap['teamTotal']['0'] == 4200


def test_unspotted_shell_then_identified_ship_packet_does_not_double_count():
    """Same salvo: shell info arrives with victimId=0, then the ship HP
    packet names the victim. The unknown shell fill must be consumed, not
    added on top of the identified ship total."""
    tracker = _tracker(live=lambda _vid: None)
    tracker.remember(77, 901, 1)
    tracker.remember(1, 537, 0)

    tracker.on_shell(0, 1, 18400)
    tracker.on_damages(77, [{'vehicleID': 1, 'damage': 18400}])

    snap = tracker.snapshot()
    assert snap['inflicted']['537']['total'] == 18400
    assert snap['inflicted']['537']['byVictim']['901'] == 18400


def test_unspotted_shell_plus_later_dot_on_named_victim_adds_dot():
    """灭点 salvo attributed via last-seen fallback, then fire ticks after
    they light up: same merge as a spotted shell fill + later DoT."""
    tracker = _tracker(live=lambda _vid: None)
    tracker.remember(77, 901, 1)
    tracker.remember(1, 537, 0)

    tracker.on_shell(0, 1, 18400, fallback_victim=77)
    tracker.on_damages(77, [{'vehicleID': 1, 'damage': 3000}])

    snap = tracker.snapshot()
    assert snap['inflicted']['537']['byVictim']['901'] == 21400
    assert snap['inflicted']['537']['total'] == 21400


def test_unattributed_shell_and_later_dot_keep_both_in_total():
    """Without a ghost match, the dark salvo still sits in the shooter
    total; a later named DoT packet is extra, not a replacement."""
    tracker = _tracker(live=lambda _vid: None)
    tracker.remember(77, 901, 1)
    tracker.remember(1, 537, 0)

    tracker.on_shell(0, 1, 18400)
    tracker.on_damages(77, [{'vehicleID': 1, 'damage': 3000}])

    snap = tracker.snapshot()
    assert snap['inflicted']['537']['total'] == 21400
    assert snap['inflicted']['537']['byVictim']['901'] == 3000


def test_damages_packet_with_sentinel_victim_uses_fallback():
    tracker = _tracker(live=lambda _vid: None)
    tracker.remember(77, 901, 1)
    tracker.remember(1, 537, 0)

    tracker.on_damages(0, [{'vehicleID': 1, 'damage': 8800}], fallback_victim=77)

    snap = tracker.snapshot()
    assert snap['inflicted']['537']['total'] == 8800
    assert snap['received']['901']['total'] == 8800


def test_guess_ghost_victim_picks_nearest_enemy_last_seen():
    ghosts = [
        {'identity': {'vehicleId': 77, 'playerId': 901, 'relation': 2},
         'pos': [100.0, 0.0, 100.0]},
        {'identity': {'vehicleId': 88, 'playerId': 902, 'relation': 2},
         'pos': [800.0, 0.0, 800.0]},
        {'identity': {'vehicleId': 11, 'playerId': 537, 'relation': 1},
         'pos': [101.0, 0.0, 101.0]},
    ]
    assert D.guess_ghost_victim(ghosts, [110.0, 0.0, 90.0]) == 77


def test_guess_ghost_victim_uses_the_only_enemy_ghost_without_shot_pos():
    ghosts = [
        {'identity': {'vehicleId': 77, 'playerId': 901, 'relation': 2},
         'pos': [100.0, 0.0, 100.0]},
        {'identity': {'vehicleId': 11, 'playerId': 537, 'relation': 1},
         'pos': [0.0, 0.0, 0.0]},
    ]
    assert D.guess_ghost_victim(ghosts, None) == 77


def test_guess_ghost_victim_stays_none_when_several_ghosts_and_no_shot():
    ghosts = [
        {'identity': {'vehicleId': 77, 'relation': 2}, 'pos': [0.0, 0.0, 0.0]},
        {'identity': {'vehicleId': 88, 'relation': 2}, 'pos': [50.0, 0.0, 0.0]},
    ]
    assert D.guess_ghost_victim(ghosts, None) is None


def test_lookup_accepts_cached_player_id_as_alias():
    """Some callbacks pass playerId where a vehicleId is expected."""
    tracker = _tracker(live=lambda _vid: None)
    tracker.remember(77, 901, 1)
    assert tracker.lookup(901) == (901, 1)


def test_prior_spotted_damage_does_not_eat_later_unattributed_dark_shell():
    """Lifetime HP on a spotted ship must not swallow a later 灭点 salvo
    that ModsAPI left with victimId=0. Comparing unknown shells to the
    attacker's whole ship ledger re-drops mid-game dark hits."""
    tracker = _tracker(live=lambda _vid: None)
    tracker.remember(77, 901, 1)
    tracker.remember(88, 902, 1)
    tracker.remember(1, 537, 0)

    tracker.on_damages(77, [{'vehicleID': 1, 'damage': 20000}])
    tracker.on_shell(0, 1, 5000)

    snap = tracker.snapshot()
    assert snap['inflicted']['537']['total'] == 25000
    assert snap['inflicted']['537']['byVictim']['901'] == 20000
    assert snap['teamTotal']['0'] == 25000


def test_prior_named_salvo_does_not_eat_later_unattributed_dark_shell():
    tracker = _tracker(live=lambda _vid: None)
    tracker.remember(77, 901, 1)
    tracker.remember(1, 537, 0)

    tracker.on_damages(77, [{'vehicleID': 1, 'damage': 20000}])
    tracker.on_shell(77, 1, 20000)
    tracker.on_shell(0, 1, 5000)

    assert tracker.snapshot()['inflicted']['537']['total'] == 25000


def test_guess_ghost_victim_dedupes_same_ship_under_two_last_seen_keys():
    """uiId then vehicleId can leave two last-seen rows for one enemy.
    Without a shot pos that must still count as the only dark target."""
    ghosts = [
        {'identity': {'vehicleId': 77, 'uiId': 5, 'relation': 2},
         'pos': [0.0, 0.0, 0.0]},
        {'identity': {'vehicleId': 77, 'uiId': 5, 'relation': 2},
         'pos': [0.0, 0.0, 0.0]},
    ]
    assert D.guess_ghost_victim(ghosts, None) == 77


def test_dark_last_seen_entries_skip_ships_that_are_currently_spotted():
    last_seen = {
        'v77': {'identity': {'vehicleId': 77, 'playerId': 901, 'relation': 2},
                'pos': [100.0, 0.0, 100.0]},
        'v88': {'identity': {'vehicleId': 88, 'playerId': 902, 'relation': 2},
                'pos': [800.0, 0.0, 800.0]},
    }
    dark = D.dark_last_seen_entries(last_seen, visible_keys=set(['v77', 'p901']))
    assert [e['identity']['vehicleId'] for e in dark] == [88]


def test_dark_filter_then_guess_does_not_pick_closer_spotted_enemy():
    """_lastSeen also stores currently lit ships. Nearest-in-2500m among
    all of them would mis-attribute a 灭点 hit to a spotted neighbour."""
    last_seen = {
        'v11': {'identity': {'vehicleId': 11, 'relation': 2},
                'pos': [100.0, 0.0, 100.0]},
        'v77': {'identity': {'vehicleId': 77, 'relation': 2},
                'pos': [400.0, 0.0, 400.0]},
    }
    shot = [110.0, 0.0, 90.0]
    assert D.guess_ghost_victim(list(last_seen.values()), shot) == 11
    dark = D.dark_last_seen_entries(last_seen, visible_keys=set(['v11']))
    assert D.guess_ghost_victim(dark, shot) == 77


def test_guess_ghost_victim_treats_invalid_shot_position_as_missing():
    """If ModsAPI argument order is off, yaw/flags must not become a point."""
    ghosts = [
        {'identity': {'vehicleId': 77, 'relation': 2}, 'pos': [0.0, 0.0, 0.0]},
        {'identity': {'vehicleId': 88, 'relation': 2}, 'pos': [50.0, 0.0, 0.0]},
    ]
    assert D.guess_ghost_victim(ghosts, 1.57) is None
    assert D.guess_ghost_victim(ghosts, True) is None
