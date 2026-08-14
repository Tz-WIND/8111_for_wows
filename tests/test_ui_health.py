# MIT License -- dataHub avatar health component (TTaroTeamPanel source).
import os
import sys

MOD_DIR = os.path.join(
    os.path.dirname(__file__), "..", "mod", "PnFMods", "WowsExtractor")
sys.path.insert(0, MOD_DIR)

import ui_health as H  # noqa: E402


class _CC(object):
    health = "health"
    avatar = "avatar"
    vehicle = "vehicle"


class _Comp(object):
    def __init__(self, **fields):
        self.__dict__.update(fields)


class _Entity(object):
    def __init__(self, components):
        self._components = components

    def __getitem__(self, key):
        return self._components[key]


def _avatar_entity(health, avatar, vehicle=None):
    components = {"health": health, "avatar": avatar}
    if vehicle is not None:
        components["vehicle"] = vehicle
    return _Entity(components)


def test_indexes_health_value_and_max_by_player_id():
    entities = [_avatar_entity(
        _Comp(value=50000, max=80000, isAlive=True),
        _Comp(playerId=805828113, id=268369920),
    )]
    index = H.index_avatar_health(entities, _CC)
    rec = index[805828113]
    assert rec["health"] == 50000
    assert rec["maxHealth"] == 80000


def test_indexes_the_same_row_under_avatar_id_and_vehicle_id():
    entities = [_avatar_entity(
        _Comp(value=50000, max=80000, isAlive=True),
        _Comp(playerId=805828113, id=268369920),
        _Comp(id=268369921),
    )]
    index = H.index_avatar_health(entities, _CC)
    assert index[805828113] is index[268369920]
    assert index[268369921] is index[268369920]


def test_alive_zero_value_is_treated_as_full_hp():
    # TTaro/Autospy: value==0 while isAlive means the bar has not received
    # its first tick yet, not that the ship is dead.
    rec = H.normalize_ui_health(0, 80000, True)
    assert rec == {"health": 80000, "maxHealth": 80000}


def test_dead_ship_emits_zero_health_and_keeps_max():
    rec = H.normalize_ui_health(0, 80000, False)
    assert rec == {"health": 0, "maxHealth": 80000}


def test_apply_fills_missing_health_from_ui_index():
    entry = {"playerId": 805828113, "vehicleId": 268369920}
    index = {
        268369920: {"health": 50000, "maxHealth": 80000},
    }
    H.apply_ui_health(entry, index)
    assert entry["health"] == 50000
    assert entry["maxHealth"] == 80000


def test_apply_does_not_overwrite_battle_ship_health():
    entry = {
        "playerId": 1,
        "health": 77850,
        "maxHealth": None,
    }
    index = {1: {"health": 72000, "maxHealth": 97700}}
    H.apply_ui_health(entry, index)
    assert entry["health"] == 77850
    assert entry["maxHealth"] == 97700


def test_apply_matches_vehicle_id_when_player_id_namespaces_differ():
    # Live capture: objects.playerId was the vehicle-like id; roster used
    # avatar.playerId. UI health is on the avatar entity under both ids.
    entry = {"playerId": 268369920}
    index = {
        805828113: {"health": 1000, "maxHealth": 2000},
        268369920: {"health": 1000, "maxHealth": 2000},
    }
    H.apply_ui_health(entry, index)
    assert entry["health"] == 1000
    assert entry["maxHealth"] == 2000
