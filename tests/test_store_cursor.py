"""Cursor semantics and the waiting/live/stale/ended status machine."""

from conftest import active_state, basic_meta, ended_state, make_store

import server as srv


# --- seq cursor ----------------------------------------------------------


def test_seq_advances_on_every_content_change():
    store = make_store()
    start = store.version
    store.set(state=active_state())
    assert store.version == start + 1
    store.set(state=active_state(ts=2.0))
    assert store.version == start + 2


def test_rendering_a_snapshot_does_not_advance_the_cursor():
    store = make_store()
    store.set(state=active_state())
    seq = store.version
    srv.build_snapshot(store)
    srv.build_snapshot(store)
    assert store.version == seq


def test_same_cursor_renders_identical_bytes():
    import json

    store = make_store()
    store.set(meta=basic_meta(), state=active_state())
    first = json.dumps(srv.build_snapshot(store), sort_keys=True)
    second = json.dumps(srv.build_snapshot(store), sort_keys=True)
    assert first == second


def test_status_change_advances_seq_without_refreshing_age():
    store = make_store()
    store.set(state=active_state())
    seq, updated_at = store.version, store.last_update

    assert store.refresh_status(now=store.last_active_update + 60.0) is True
    assert store.version == seq + 1
    # ageSeconds must keep climbing: the data really is that old.
    assert store.last_update == updated_at


def test_refresh_status_is_idempotent_while_nothing_changes():
    store = make_store()
    store.set(state=active_state())
    seq = store.version
    assert store.refresh_status(now=store.last_active_update) is False
    assert store.version == seq


# --- status machine ------------------------------------------------------


def test_starts_out_waiting():
    assert make_store().source_status == srv.STATUS_WAITING


def test_empty_state_file_is_still_waiting():
    store = make_store()
    store.set(state={})
    assert store.source_status == srv.STATUS_WAITING


def test_active_frame_is_live():
    store = make_store()
    store.set(state=active_state())
    assert store.source_status == srv.STATUS_LIVE


def test_live_data_goes_stale_after_the_threshold():
    store = make_store(poll_interval=0.1)
    store.set(state=active_state())
    base = store.last_active_update

    assert store.derive_status(now=base + 1.0) == srv.STATUS_LIVE
    assert store.derive_status(now=base + srv.stale_after(0.1) + 0.01) == srv.STATUS_STALE


def test_stale_threshold_follows_the_poll_interval():
    assert srv.stale_after(0.1) == 2.0
    assert srv.stale_after(1.0) == 5.0
    assert srv.stale_after(None) == 2.0


def test_stale_data_recovers_to_live_when_frames_resume():
    store = make_store()
    store.set(state=active_state())
    store.refresh_status(now=store.last_active_update + 60.0)
    assert store.source_status == srv.STATUS_STALE

    store.set(state=active_state(ts=3.0))
    assert store.source_status == srv.STATUS_LIVE


def test_inactive_frame_after_a_battle_is_ended():
    store = make_store()
    store.set(state=active_state())
    store.set(state=ended_state())
    assert store.source_status == srv.STATUS_ENDED


def test_inactive_frame_without_a_prior_battle_is_waiting_not_ended():
    store = make_store()
    store.set(state=ended_state())
    assert store.source_status == srv.STATUS_WAITING


def test_a_dead_collector_never_looks_like_a_finished_battle():
    """Staleness must not be mistaken for the battle being over."""
    store = make_store()
    store.set(state=active_state())
    for offset in (5.0, 60.0, 3600.0):
        assert store.derive_status(now=store.last_active_update + offset) == srv.STATUS_STALE


# --- battle identity -----------------------------------------------------


def test_battle_id_is_taken_from_the_state_frame():
    store = make_store()
    store.set(state=active_state(battleId="abc-1-2000"))
    assert store.battle_id == "abc-1-2000"


def test_battle_id_can_come_from_meta_alone():
    store = make_store()
    store.set(meta=basic_meta(battleId="meta-1"), state=active_state())
    assert store.battle_id == "meta-1"


def test_battle_id_survives_on_the_final_frame():
    store = make_store()
    store.set(state=active_state(battleId="abc-1-2000"))
    store.set(state=ended_state(battleId="abc-1-2000"))
    assert store.source_status == srv.STATUS_ENDED
    assert srv.build_snapshot(store)["battleId"] == "abc-1-2000"


def test_battle_id_changes_between_battles():
    store = make_store()
    store.set(state=active_state(battleId="abc-1-2000"))
    store.set(state=ended_state(battleId="abc-1-2000"))
    store.set(state=active_state(battleId="abc-2-2000"))
    assert store.battle_id == "abc-2-2000"


def test_legacy_frames_get_a_minted_battle_id():
    store = make_store()
    store.set(state=active_state())
    minted = store.battle_id
    assert minted and store.instance_id in minted


def test_minted_battle_id_is_stable_within_one_battle():
    store = make_store()
    store.set(state=active_state())
    minted = store.battle_id
    store.set(state=active_state(ts=2.0))
    store.set(state=active_state(ts=3.0))
    assert store.battle_id == minted


def test_minted_battle_id_is_reissued_for_the_next_battle():
    store = make_store()
    store.set(state=active_state())
    first = store.battle_id
    store.set(state=ended_state())
    assert store.battle_id == first  # the ended frame stays attributable

    store.set(state=active_state(ts=10.0))
    assert store.battle_id != first


def test_instance_ids_differ_between_processes():
    assert make_store().instance_id != make_store().instance_id
