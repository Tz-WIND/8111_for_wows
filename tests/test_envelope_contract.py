# MIT License -- contract tests for the additive service envelope.
"""Unit tests for the (instanceId, seq) cursor, battleId and source.status
state machine, and the fixed empty-value contract. No network: they drive the
Store directly, the same object /all and /ws serve from.
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "server"))
import server as S  # noqa: E402


def status_of(obj):
    return obj["source"]["status"]


def make_store():
    return S.Store(instance_id="inst1", source_kind="file")


def state_payload(active=True, **values):
    payload = {"schema": 1, "active": active}
    payload.update(values)
    return payload


def meta_payload(**values):
    payload = {"schema": 1}
    payload.update(values)
    return payload


# --------------------------------------------------------------------------
# envelope + fixed empty values
# --------------------------------------------------------------------------
def test_empty_snapshot_fixed_values():
    obj, _ = make_store().snapshot()
    assert obj["serviceId"] == S.SERVICE_ID
    assert obj["apiVersion"] == S.API_VERSION
    assert obj["instanceId"] == "inst1"
    assert obj["seq"] == 0
    assert obj["battleId"] is None
    assert obj["self"] is None
    assert obj["objects"] == []
    assert obj["roster"] == []
    assert obj["damage"] == {"inflicted": {}, "received": {}, "teamTotal": {}}
    assert obj["ballistics"] == {"available": False}
    assert obj["extensions"] == {}
    assert status_of(obj) == "waiting"


def test_legacy_flat_keys_preserved():
    # Old clients read these flat keys directly; the envelope must not drop them.
    obj, _ = make_store().snapshot()
    for key in ("schema", "active", "ts", "battleType", "gameMode", "map",
                "bounds", "boundsSource", "self", "objects", "roster",
                "damage", "ballistics", "diag"):
        assert key in obj, key


def test_capabilities_and_availability_domains():
    obj, _ = make_store().snapshot()
    domains = {"self", "objects", "roster", "damage", "ballistics", "map"}
    assert set(obj["capabilities"]) == domains
    assert set(obj["availability"]) == domains
    # nothing present yet -> unknown, not a falsy "false"
    assert obj["availability"]["self"] == "unknown"


def test_present_empty_domains_are_available():
    st = make_store()
    assert st.apply(
        1.0,
        meta=meta_payload(roster=[], map={}),
        state=state_payload(ships=[], damage={}),
    ) is True

    obj, _ = st.snapshot()
    assert obj["availability"]["objects"] == "available"
    assert obj["availability"]["roster"] == "available"
    assert obj["availability"]["damage"] == "available"
    assert obj["availability"]["map"] == "available"


# --------------------------------------------------------------------------
# seq cursor semantics
# --------------------------------------------------------------------------
def test_repeat_read_does_not_advance_seq():
    st = make_store()
    a, _ = st.snapshot()
    b, _ = st.snapshot()
    assert a["seq"] == b["seq"] == 0


def test_seq_advances_on_data_change_and_status_flip():
    st = make_store()
    st.apply(100.0, state=state_payload(ts=1.0, ships=[]))
    assert st.snapshot()[0]["seq"] == 1
    st.apply(101.0, state=state_payload(ts=2.0, ships=[]))
    assert st.snapshot()[0]["seq"] == 2
    # a live->stale flip is a content change: new seq, new snapshot
    assert st.tick_staleness(200.0, 0.1) is True
    assert st.snapshot()[0]["seq"] == 3


# --------------------------------------------------------------------------
# battleId + status state machine
# --------------------------------------------------------------------------
def test_battle_id_generated_and_retained():
    st = make_store()
    st.apply(1.0, state=state_payload(ships=[]))
    assert st.battle_id == "inst1-b1"
    st.apply(2.0, state=state_payload(ships=[]))
    assert st.battle_id == "inst1-b1"          # same battle, unchanged
    st.apply(3.0, state=state_payload(False))    # real end
    assert status_of(st.snapshot()[0]) == "ended"
    assert st.battle_id == "inst1-b1"          # retained on the terminal frame


def test_new_battle_increments_id():
    st = make_store()
    st.apply(1.0, state=state_payload())
    st.apply(2.0, state=state_payload(False))
    st.apply(3.0, state=state_payload())
    assert st.battle_id == "inst1-b2"


def test_mod_provided_battle_id_wins():
    st = make_store()
    st.apply(1.0, state=state_payload(battleId="ARENA-42"))
    assert st.battle_id == "ARENA-42"


def test_status_transitions_waiting_live_stale_ended():
    st = make_store()
    assert status_of(st.snapshot()[0]) == "waiting"
    st.apply(10.0, state=state_payload(ships=[]))
    assert status_of(st.snapshot()[0]) == "live"
    st.tick_staleness(100.0, 0.1)
    assert status_of(st.snapshot()[0]) == "stale"
    # availability of live-derived domains reads as stale, never fabricated data
    assert st.snapshot()[0]["availability"]["objects"] == "stale"


def test_disconnect_does_not_fabricate_ended():
    # Losing updates while live must become `stale`, never `ended`.
    st = make_store()
    st.apply(10.0, state=state_payload(ships=[]))
    st.tick_staleness(100.0, 0.1)
    assert status_of(st.snapshot()[0]) == "stale"
    # a further staleness tick is idempotent (already stale, not live)
    assert st.tick_staleness(200.0, 0.1) is False


def test_stale_threshold_uses_max_2s_and_5x_poll():
    st = make_store()
    st.apply(1000.0, state=state_payload())
    # 1.5s < max(2, 5*0.1) -> still live
    assert st.tick_staleness(1001.5, 0.1) is False
    # 2.5s > 2s -> stale
    assert st.tick_staleness(1002.5, 0.1) is True
    # with a slow poll, threshold is 5*1.0 = 5s
    st2 = make_store()
    st2.apply(1000.0, state=state_payload())
    assert st2.tick_staleness(1004.0, 1.0) is False
    assert st2.tick_staleness(1006.0, 1.0) is True


# --------------------------------------------------------------------------
# extensions passthrough
# --------------------------------------------------------------------------
def test_known_extension_passed_through():
    st = make_store()
    st.apply(1.0, state=state_payload(
        extensions={
            "vendor.torpedoes": {
                "schema": "1.0", "data": {"count": 3}}}))
    ext = st.snapshot()[0]["extensions"]
    assert ext == {
        "vendor.torpedoes": {"schema": "1.0", "data": {"count": 3}}}


def test_extension_metadata_and_dynamic_capability_survive():
    extension = {
        "vendor.future": {
            "schema": "2.0",
            "data": {},
            "available": False,
            "future": 7,
        },
    }
    st = make_store()
    assert st.apply(
        1.0,
        state=state_payload(extensions=extension),
    ) is True

    obj, _ = st.snapshot()
    assert obj["extensions"] == extension
    assert obj["capabilities"]["vendor.future"] == "2.0"
    assert obj["availability"]["vendor.future"] == "unknown"


def test_malformed_extension_dropped():
    st = make_store()
    st.apply(1.0, state=state_payload(
        extensions={
            "bad": {"schema": "1.0", "data": {}},
            "vendor.scalar": 123,
            "vendor.noData": {"schema": "1.0"},
            "vendor.noSchema": {"data": {}},
            "vendor.bad name": {"schema": "1.0", "data": {}},
            "vendor.blankSchema": {"schema": "   ", "data": {}},
        }))
    assert st.snapshot()[0]["extensions"] == {}


# --------------------------------------------------------------------------
# file-bridge validation + independent freshness clocks
# --------------------------------------------------------------------------
@pytest.mark.parametrize("payload", [
    [],
    {},
    {"schema": 2, "active": True},
    {"schema": 1, "active": "false"},
    {"schema": 1, "active": 1},
    {"schema": 1, "active": True, "self": {"position": [1]}},
    {"schema": 1, "active": True, "ships": [{"position": [1]}]},
    {"schema": 1, "active": True, "ships": [{"position": [10 ** 1000, 0, 0]}]},
    {"schema": 1, "active": True, "ships": [{"lastPosition": [1]}]},
    {"schema": 1, "active": True,
     "ships": [{"health": 100, "maxHealth": "unknown"}]},
    {"schema": 1, "active": True, "ships": [{"playerId": []}]},
    {"schema": 1, "active": True, "self": {"yaw": float("nan")}},
    {"schema": 1, "active": True, "ships": [{"yaw": float("inf")}]},
])
def test_invalid_state_payload_does_not_mutate_store(payload):
    st = make_store()

    assert st.apply(10.0, state=payload) is False

    assert st.state == {}
    assert st.seq == 0
    assert st.status == "waiting"
    assert st.last_state_update is None
    assert st.revision_at is None


@pytest.mark.parametrize("payload", [
    [],
    {},
    {"schema": 2},
    {"schema": 1, "map": []},
    {"schema": 1, "roster": [{"playerId": []}]},
    {"schema": 1, "roster": [{"maxHealth": "unknown"}]},
    {"schema": 1, "map": {"width": float("inf")}},
])
def test_invalid_meta_payload_does_not_mutate_store(payload):
    st = make_store()

    assert st.apply(10.0, meta=payload) is False

    assert st.meta == {}
    assert st.seq == 0
    assert st.status == "waiting"
    assert st.last_meta_update is None
    assert st.revision_at is None


def test_valid_nested_file_shapes_build_snapshot():
    st = make_store()
    meta = meta_payload(
        map={"id": "map-1"},
        roster=[{"playerId": 7, "maxHealth": 1000}],
    )
    state = state_payload(
        self={"position": [0.0, 0.0, 0.0], "health": 900, "maxHealth": 1000},
        ships=[{
            "playerId": 7,
            "position": [10.0, 0.0, 20.0],
            "lastPosition": [5.0, 0.0, 15.0],
            "health": 900,
            "maxHealth": 1000,
        }],
    )

    assert st.apply(10.0, meta=meta, state=state) is True
    obj, _ = st.snapshot()
    assert obj["objects"][0]["x"] == 10.0
    assert obj["objects"][0]["lastZ"] == 15.0


def test_mixed_valid_and_invalid_batch_is_atomic():
    st = make_store()
    original_meta = meta_payload(map={"id": "map-1"})
    original_state = state_payload(ships=[])
    assert st.apply(10.0, meta=original_meta, state=original_state) is True
    original_seq = st.seq

    assert st.apply(
        11.0,
        meta=meta_payload(map={"id": "map-2"}),
        state=state_payload(ships=[{"position": [1]}]),
    ) is False

    assert st.meta == original_meta
    assert st.state == original_state
    assert st.seq == original_seq
    assert st.status == "live"


@pytest.mark.parametrize("table", ["inflicted", "received", "teamTotal"])
@pytest.mark.parametrize("bad_value", [[], "invalid", 1])
def test_damage_tables_must_be_objects_when_present(table, bad_value):
    st = make_store()

    assert st.apply(
        10.0,
        state=state_payload(damage={table: bad_value}),
    ) is False

    assert st.state == {}
    assert st.seq == 0
    assert st.revision_at is None


@pytest.mark.parametrize("bad_value", [[], "invalid", 1])
def test_ballistics_payload_must_be_an_object(bad_value):
    st = make_store()

    assert st.apply(
        10.0,
        state=state_payload(ballistics=bad_value),
    ) is False

    assert st.state == {}
    assert st.seq == 0


@pytest.mark.parametrize(("ballistics", "expected"), [
    ({"ammoType": "AP"}, {"ammoType": "AP", "available": False}),
    ({"available": 1, "ammoType": "HE"},
     {"available": False, "ammoType": "HE"}),
    ({"available": "yes", "range": 12.0},
     {"available": False, "range": 12.0}),
    ({"available": True, "ammoType": "SAP"},
     {"available": True, "ammoType": "SAP"}),
])
def test_ballistics_output_has_strict_boolean_availability(
        ballistics, expected):
    st = make_store()

    assert st.apply(
        10.0,
        state=state_payload(ballistics=ballistics),
    ) is True

    obj, encoded = st.snapshot()
    assert obj["ballistics"] == expected
    assert type(obj["ballistics"]["available"]) is bool
    assert json.loads(encoded)["ballistics"] == expected


def test_extreme_health_ratio_does_not_emit_infinity():
    st = make_store()
    assert st.apply(
        10.0,
        state=state_payload(
            ships=[{"health": 1e308, "maxHealth": 1e-308}],
        ),
    ) is True

    obj, encoded = st.snapshot()
    assert obj["objects"][0]["hpRatio"] is None
    assert "Infinity" not in encoded


def test_snapshot_json_encoder_rejects_non_finite_values_as_last_defense():
    st = make_store()
    # Deliberately bypass apply(), proving the encoder cannot silently emit the
    # non-standard JSON tokens NaN/Infinity if a future validation path regresses.
    st.state = {"schema": 1, "active": True, "self": {"yaw": float("nan")}}

    with pytest.raises(ValueError, match="Out of range float values"):
        st.snapshot()


def test_snapshot_object_cannot_mutate_cached_cursor():
    st = make_store()
    assert st.apply(
        10.0,
        state=state_payload(ships=[{"playerId": 7}]),
    ) is True

    first, encoded = st.snapshot()
    first["source"]["status"] = "corrupted"
    first["objects"].append({"playerId": 99})

    second, encoded_again = st.snapshot()
    assert encoded_again == encoded
    assert second == json.loads(encoded)
    assert second["source"]["status"] == "live"
    assert [item["playerId"] for item in second["objects"]] == [7]


def test_meta_update_and_stale_transition_do_not_change_state_freshness():
    st = make_store()
    assert st.apply(100.0, state=state_payload()) is True
    assert st.last_state_update == 100.0
    assert st.revision_at == 100.0

    assert st.apply(101.0, meta=meta_payload(map={"id": "map-1"})) is True
    assert st.status == "live"
    assert st.last_state_update == 100.0
    assert st.last_meta_update == 101.0
    assert st.revision_at == 101.0

    assert st.tick_staleness(103.0, 0.1) is True
    assert st.status == "stale"
    assert st.last_state_update == 100.0
    assert st.revision_at == 103.0

    assert st.apply(104.0, meta=meta_payload(roster=[])) is True
    assert st.status == "stale"
    assert st.last_state_update == 100.0
    assert st.last_meta_update == 104.0
    assert st.snapshot()[0]["source"]["updatedAt"] == 100.0


def test_initial_valid_inactive_state_is_ended_and_keeps_battle_id():
    st = make_store()

    assert st.apply(10.0, state=state_payload(False, battleId="arena-terminal")) is True

    assert st.status == "ended"
    assert st.battle_id == "arena-terminal"


def test_state_source_time_drives_immediate_staleness():
    st = make_store()

    assert st.apply(
        200.0,
        state=state_payload(battleId="old-active"),
        state_updated_at=100.0,
    ) is True
    assert st.tick_staleness(200.0, 0.1) is True

    obj, _ = st.snapshot()
    assert status_of(obj) == "stale"
    assert obj["source"]["updatedAt"] == 100.0


def test_extreme_finite_coordinates_do_not_derive_non_finite_values():
    st = make_store()
    assert st.apply(
        10.0,
        meta=meta_payload(map={
            "minX": -1e308,
            "maxX": 1e308,
            "minZ": -1e308,
            "maxZ": 1e308,
        }),
        state=state_payload(ships=[{"position": [1e308, 0.0, 1e308]}]),
    ) is True

    obj, encoded = st.snapshot()
    assert "nx" not in obj["objects"][0]
    assert "ny" not in obj["objects"][0]
    assert "NaN" not in encoded


def test_zero_battle_id_is_retained_on_initial_inactive_state():
    st = make_store()

    assert st.apply(10.0, state=state_payload(False, battleId=0)) is True

    assert st.status == "ended"
    assert st.battle_id == "0"


def test_overflowing_source_timestamp_falls_back_without_partial_commit():
    st = make_store()
    payload = state_payload(battleId="timestamp-safe")

    assert st.apply(
        10.0,
        state=payload,
        state_updated_at=10 ** 1000,
    ) is True

    assert st.state == payload
    assert st.last_state_update == 10.0
    assert st.revision_at == 10.0
    assert st.seq == 1
