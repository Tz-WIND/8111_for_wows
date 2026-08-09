"""Dead ships must not appear as last-seen ("灭点") ghosts.

The collector sometimes loses a ship from getAllShips() without a clean
isAlive()==False frame, or records alive as 0 instead of False. Those rows
must not keep last-seen markers that the overlay draws as ghosts.
"""

from conftest import active_state, basic_meta

import server as srv


def _ghost_ship(**overrides):
    ship = {
        "playerId": 3001,
        "vehicleId": 77,
        "teamId": 1,
        "relation": 2,
        "name": "Yamato",
        "alive": True,
        "visible": False,
        "lastPosition": [200.0, 0.0, -400.0],
        "lastNx": 0.60,
        "lastNy": 0.70,
        "lastHealth": 12000,
        "lastSeenTs": 1.0,
        "staleSeconds": 3.0,
    }
    ship.update(overrides)
    return ship


def test_alive_false_strips_ghost_markers():
    objects, _ = srv.build_map_objects(basic_meta(), active_state(ships=[
        _ghost_ship(alive=False),
    ]))
    assert objects[0]["alive"] is False
    assert "lastNx" not in objects[0]
    assert "lastNy" not in objects[0]
    assert "lastX" not in objects[0]


def test_alive_zero_counts_as_dead():
    objects, _ = srv.build_map_objects(basic_meta(), active_state(ships=[
        _ghost_ship(alive=0),
    ]))
    assert objects[0]["alive"] is False
    assert "lastNx" not in objects[0]


def test_zero_health_counts_as_dead():
    objects, _ = srv.build_map_objects(basic_meta(), active_state(ships=[
        _ghost_ship(alive=True, health=0, lastHealth=0),
    ]))
    assert objects[0]["alive"] is False
    assert "lastNx" not in objects[0]


def test_zero_last_health_on_dark_ship_counts_as_dead():
    objects, _ = srv.build_map_objects(basic_meta(), active_state(ships=[
        _ghost_ship(alive=True, health=None, lastHealth=0),
    ]))
    assert objects[0]["alive"] is False
    assert "lastNx" not in objects[0]


def test_live_unspotted_ghost_is_kept():
    objects, _ = srv.build_map_objects(basic_meta(), active_state(ships=[
        _ghost_ship(alive=True, lastHealth=12000),
    ]))
    assert objects[0]["alive"] is True
    assert objects[0]["lastNx"] == 0.60
    assert objects[0]["lastNy"] == 0.70
