"""Trusted minimap nx/ny must win over world-derived normalization.

Spotted-only enemies often get a world `position` from an unstable map->world
calibration. When that transform flips axes mid-battle the world coords jump,
but the collector's mapPosition-based nx/ny stay correct. The overlay reads
server nx/ny, so the server must prefer those trusted values.
"""

from conftest import active_state, basic_meta

import server as srv


def test_trusted_nx_ny_preferred_over_world_position():
    meta = basic_meta()
    # World position would normalize to ~ (0.75, 0.25) with the test bounds.
    # Trusted minimap coords say the ship is at bottom-left instead.
    state = active_state(ships=[{
        "playerId": 2000,
        "teamId": 1,
        "relation": 2,
        "name": "Yamato",
        "alive": True,
        "visible": True,
        "position": [500.0, 0.0, 500.0],
        "nx": 0.20,
        "ny": 0.80,
        "yaw": 1.0,
    }])
    objects, _bounds = srv.build_map_objects(meta, state)
    assert len(objects) == 1
    assert objects[0]["nx"] == 0.20
    assert objects[0]["ny"] == 0.80


def test_world_normalize_still_used_without_nx_ny():
    meta = basic_meta()
    state = active_state(ships=[{
        "playerId": 2000,
        "teamId": 1,
        "relation": 2,
        "alive": True,
        "visible": True,
        "position": [0.0, 0.0, 0.0],
    }])
    objects, _bounds = srv.build_map_objects(meta, state)
    assert objects[0]["nx"] == 0.5
    assert objects[0]["ny"] == 0.5


def test_trusted_last_nx_ny_preferred_over_last_position():
    meta = basic_meta()
    state = active_state(ships=[{
        "playerId": 2000,
        "teamId": 1,
        "relation": 2,
        "alive": True,
        "visible": False,
        "lastPosition": [500.0, 0.0, 500.0],
        "lastNx": 0.20,
        "lastNy": 0.80,
        "lastSeenTs": 1.0,
        "staleSeconds": 2.0,
    }])
    objects, _bounds = srv.build_map_objects(meta, state)
    assert objects[0]["lastNx"] == 0.20
    assert objects[0]["lastNy"] == 0.80
