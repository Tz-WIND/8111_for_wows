"""Snapshot shape: pinned empty values, the v1 envelope, and legacy fields."""

from conftest import (
    ENVELOPE_KEYS,
    LEGACY_KEYS,
    active_state,
    basic_meta,
    make_store,
)

import server as srv


# --- fixed empty values --------------------------------------------------


def test_empty_snapshot_uses_fixed_shapes():
    body = srv.build_all({}, {})
    assert body["self"] is None
    assert body["objects"] == []
    assert body["roster"] == []
    assert body["damage"] == {"inflicted": {}, "received": {}, "teamTotal": {}}
    assert body["ballistics"] == {"available": False}
    assert body["bounds"] is None


def test_empty_self_dict_is_reported_as_null():
    body = srv.build_all({}, {"self": {}})
    assert body["self"] is None


def test_null_roster_becomes_empty_list():
    body = srv.build_all({"roster": None}, {})
    assert body["roster"] == []


def test_damage_always_has_three_tables_and_keeps_extras():
    body = srv.build_all({}, {"damage": {"inflicted": {"1": 5}, "spotting": {"1": 2}}})
    assert body["damage"]["inflicted"] == {"1": 5}
    assert body["damage"]["received"] == {}
    assert body["damage"]["teamTotal"] == {}
    assert body["damage"]["spotting"] == {"1": 2}


def test_non_dict_damage_table_is_replaced_not_passed_through():
    body = srv.build_all({}, {"damage": {"inflicted": "nonsense"}})
    assert body["damage"]["inflicted"] == {}


def test_ballistics_available_is_coerced_to_bool():
    body = srv.build_all({}, {"ballistics": {"ammoType": "AP"}})
    assert body["ballistics"]["available"] is False
    assert body["ballistics"]["ammoType"] == "AP"


# --- legacy compatibility ------------------------------------------------


def test_legacy_keys_survive_the_envelope():
    store = make_store()
    store.set(meta=basic_meta(), state=active_state())
    snapshot = srv.build_snapshot(store)
    for key in LEGACY_KEYS:
        assert key in snapshot, "legacy consumers read %r" % key
    assert snapshot["schema"] == 1


def test_envelope_keys_are_all_present():
    store = make_store()
    store.set(meta=basic_meta(), state=active_state())
    snapshot = srv.build_snapshot(store)
    for key in ENVELOPE_KEYS:
        assert key in snapshot


def test_envelope_identity_matches_the_store():
    store = make_store()
    store.set(state=active_state())
    snapshot = srv.build_snapshot(store)
    assert snapshot["serviceId"] == srv.SERVICE_ID
    assert snapshot["apiVersion"] == srv.API_VERSION
    assert snapshot["instanceId"] == store.instance_id
    assert snapshot["seq"] == store.version
    assert snapshot["source"]["status"] == srv.STATUS_LIVE
    assert snapshot["source"]["updatedAt"] == store.last_update


def test_demo_mode_is_reported_in_source():
    store = make_store(source_kind="demo-generator", mode="demo")
    store.set(state=active_state())
    assert srv.build_snapshot(store)["source"]["mode"] == "demo"


# --- capabilities --------------------------------------------------------


def test_unsupported_domains_are_declared_not_omitted():
    caps = srv.build_capabilities()
    for name in srv.UNSUPPORTED_DOMAINS:
        assert caps[name] is None
    for name in srv.SUPPORTED_DOMAINS:
        assert isinstance(caps[name], str)


# --- availability --------------------------------------------------------


def test_absent_domains_are_unknown_never_false():
    store = make_store()
    store.set(state={"schema": 1, "active": True, "ts": 1.0})
    availability = srv.build_snapshot(store)["availability"]
    assert availability["self"] == srv.AVAIL_UNKNOWN
    assert availability["objects"] == srv.AVAIL_UNKNOWN
    assert availability["ballistics"] == srv.AVAIL_UNKNOWN


def test_present_domains_are_available():
    store = make_store()
    store.set(meta=basic_meta(), state=active_state())
    availability = srv.build_snapshot(store)["availability"]
    assert availability["self"] == srv.AVAIL_AVAILABLE
    assert availability["roster"] == srv.AVAIL_AVAILABLE
    assert availability["map"] == srv.AVAIL_AVAILABLE


def test_unsupported_domains_are_flagged_unsupported_in_availability():
    store = make_store()
    store.set(meta=basic_meta(), state=active_state())
    availability = srv.build_snapshot(store)["availability"]
    for name in srv.UNSUPPORTED_DOMAINS:
        assert availability[name] == srv.AVAIL_UNSUPPORTED


def test_stale_frame_marks_live_domains_stale_but_not_meta_domains():
    store = make_store()
    store.set(meta=basic_meta(), state=active_state())
    # Age the frame past the stale threshold without any new data arriving.
    assert store.refresh_status(now=store.last_active_update + 60.0) is True
    availability = srv.build_snapshot(store)["availability"]
    assert availability["self"] == srv.AVAIL_STALE
    assert availability["roster"] == srv.AVAIL_AVAILABLE
    assert availability["map"] == srv.AVAIL_AVAILABLE


def test_damage_presence_needs_a_populated_table():
    store = make_store()
    store.set(state=active_state())
    assert srv.build_snapshot(store)["availability"]["damage"] == srv.AVAIL_AVAILABLE

    store.set(state=active_state(damage={"inflicted": {"2000": 1500},
                                         "received": {}, "teamTotal": {}}))
    assert srv.build_snapshot(store)["availability"]["damage"] == srv.AVAIL_AVAILABLE


# --- extensions ----------------------------------------------------------


def test_extensions_default_to_empty():
    store = make_store()
    store.set(meta=basic_meta(), state=active_state())
    assert srv.build_snapshot(store)["extensions"] == {}


def test_unknown_extension_passes_through_and_becomes_a_capability():
    store = make_store()
    store.set(state=active_state(extensions={
        "vendor.torpedoes": {"schema": "2.0", "data": {"inbound": []}},
    }))
    snapshot = srv.build_snapshot(store)
    assert snapshot["extensions"]["vendor.torpedoes"] == {
        "schema": "2.0", "data": {"inbound": []}}
    assert snapshot["capabilities"]["vendor.torpedoes"] == "2.0"
    assert snapshot["availability"]["vendor.torpedoes"] == srv.AVAIL_UNKNOWN


def test_malformed_extension_entries_are_dropped():
    store = make_store()
    store.set(state=active_state(extensions={
        "vendor.missing": {"schema": "1.0"},
        "vendor.scalar": "nope",
        "vendor.good": {"schema": "1.0", "data": 1},
    }))
    extensions = srv.build_snapshot(store)["extensions"]
    assert set(extensions) == {"vendor.good"}


def test_extensions_from_meta_and_state_are_merged():
    store = make_store()
    store.set(
        meta=basic_meta(extensions={
            "vendor.caps": {"schema": "1.0", "data": []}}),
        state=active_state(extensions={
            "vendor.kills": {"schema": "1.0", "data": []}}),
    )
    assert set(srv.build_snapshot(store)["extensions"]) == {
        "vendor.caps", "vendor.kills"}
