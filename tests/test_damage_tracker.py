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
