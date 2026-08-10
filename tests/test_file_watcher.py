# MIT License -- real file-watcher regression tests.
import asyncio
import json
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "server"))
import server as S  # noqa: E402


def _write_json(path, payload, *, mtime):
    path.write_text(json.dumps(payload), encoding="utf-8")
    os.utime(path, (mtime, mtime))


async def _wait_for(predicate, timeout=1.0):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.005)
    raise AssertionError("condition was not reached before timeout")


async def _cancel(task):
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def _ingestion_health(app):
    await _wait_for(lambda: S.INGESTION_HEALTH_KEY in app)
    return app[S.INGESTION_HEALTH_KEY]


async def _wait_for_rejection(health, after_iteration):
    await _wait_for(
        lambda: (health.iteration > after_iteration
                 and health.rejected_revision is not None))


@pytest.mark.asyncio
async def test_bad_state_does_not_kill_watcher_and_same_mtime_can_recover(tmp_path):
    state_file = tmp_path / "state.json"
    meta_file = tmp_path / "meta.json"
    revision = time.time()
    _write_json(meta_file, {"schema": 1, "roster": []}, mtime=revision)
    _write_json(state_file, {"schema": 1, "active": True, "battleId": "b1"},
                mtime=revision)
    store = S.Store(instance_id="watcher-test")
    app = {S.STORE_KEY: store}
    task = asyncio.create_task(
        S.file_watcher(app, str(state_file), str(meta_file), 0.01))
    try:
        await _wait_for(lambda: store.status == "live")
        health = await _ingestion_health(app)
        accepted_seq = store.seq
        accepted_state = store.state

        bad_revision = revision + 1.0
        before_rejection = health.iteration
        invalid_state = {
            "schema": 1, "active": "bad", "battleId": "b1"}
        _write_json(state_file, invalid_state, mtime=bad_revision)
        rejected_signature = S._file_signature(os.stat(state_file))
        await _wait_for_rejection(health, before_rejection)
        assert task.done() is False
        assert store.seq == accepted_seq
        assert store.state == accepted_state
        assert store.status == "live"

        # Replacing the bad body while retaining its mtime must be retried: an
        # invalid revision is never acknowledged by the watcher.
        recovered_state = {
            "schema": 1, "active": False, "battleId": "b1"}
        assert len(json.dumps(invalid_state)) == len(json.dumps(recovered_state))
        _write_json(state_file, recovered_state, mtime=bad_revision)
        assert S._file_signature(os.stat(state_file)) == rejected_signature
        await _wait_for(lambda: store.status == "ended")
        assert store.battle_id == "b1"
        assert task.done() is False
    finally:
        await _cancel(task)


@pytest.mark.asyncio
async def test_unexpected_watcher_error_terminates_task_and_marks_unhealthy(
        tmp_path, monkeypatch):
    state_file = tmp_path / "state.json"
    meta_file = tmp_path / "meta.json"
    store = S.Store(instance_id="watcher-test")
    app = {S.STORE_KEY: store}

    def fail_staleness(_now, _interval):
        raise RuntimeError("watcher invariant failed")

    monkeypatch.setattr(store, "tick_staleness", fail_staleness)
    task = asyncio.create_task(
        S.file_watcher(app, str(state_file), str(meta_file), 0.01))
    app[S.BACKGROUND_TASK_KEY] = task
    try:
        await _wait_for(task.done)
        assert S.background_task_health(app) == (
            False, "RuntimeError: watcher invariant failed")
        assert "RuntimeError: watcher invariant failed" in (
            app[S.INGESTION_HEALTH_KEY].error or "")
        with pytest.raises(RuntimeError, match="watcher invariant failed"):
            await task
    finally:
        if not task.done():
            await _cancel(task)


@pytest.mark.asyncio
async def test_nested_bad_state_is_not_committed_and_watcher_recovers(tmp_path):
    state_file = tmp_path / "state.json"
    meta_file = tmp_path / "meta.json"
    revision = time.time()
    _write_json(meta_file, {"schema": 1, "roster": []}, mtime=revision)
    _write_json(
        state_file,
        {"schema": 1, "active": True, "battleId": "b1", "ships": []},
        mtime=revision,
    )
    store = S.Store(instance_id="watcher-test")
    recorder = _RecordingWs()
    store.ws_clients.add(recorder)
    app = {S.STORE_KEY: store}
    task = asyncio.create_task(
        S.file_watcher(app, str(state_file), str(meta_file), 0.01))
    try:
        await _wait_for(lambda: store.status == "live")
        health = await _ingestion_health(app)
        accepted_seq = store.seq
        accepted_state = store.state

        bad_revision = revision + 1.0
        before_rejection = health.iteration
        _write_json(
            state_file,
            {"schema": 1, "active": True, "battleId": "b1",
             "ships": [{"position": [1]}]},
            mtime=bad_revision,
        )
        await _wait_for_rejection(health, before_rejection)
        assert task.done() is False
        assert store.seq == accepted_seq
        assert store.state == accepted_state
        assert store.status == "live"

        _write_json(
            state_file,
            {"schema": 1, "active": False, "battleId": "b1", "ships": []},
            mtime=bad_revision,
        )
        await _wait_for(lambda: store.status == "ended")
        assert task.done() is False
    finally:
        await _cancel(task)


@pytest.mark.asyncio
async def test_invalid_side_rejects_same_iteration_meta_state_batch(tmp_path):
    state_file = tmp_path / "state.json"
    meta_file = tmp_path / "meta.json"
    revision = time.time()
    original_meta = {"schema": 1, "map": {"id": "map-1"}, "roster": []}
    original_state = {
        "schema": 1, "active": True, "battleId": "b1", "ships": []}
    _write_json(meta_file, original_meta, mtime=revision)
    _write_json(state_file, original_state, mtime=revision)
    store = S.Store(instance_id="watcher-test")
    app = {S.STORE_KEY: store}
    task = asyncio.create_task(
        S.file_watcher(app, str(state_file), str(meta_file), 0.01))
    try:
        await _wait_for(lambda: store.status == "live")
        health = await _ingestion_health(app)
        accepted_seq = store.seq

        rejected_mtime = revision + 1.0
        before_rejection = health.iteration
        _write_json(
            meta_file,
            {"schema": 1, "map": {"id": "map-2"}, "roster": []},
            mtime=rejected_mtime,
        )
        _write_json(
            state_file,
            {"schema": 1, "active": "false", "battleId": "b1"},
            mtime=rejected_mtime,
        )
        await _wait_for_rejection(health, before_rejection)

        assert store.meta == original_meta
        assert store.state == original_state
        assert store.seq == accepted_seq
        assert health.rejected_revision["source"] == "state"

        _write_json(
            state_file,
            {"schema": 1, "active": False, "battleId": "b1", "ships": []},
            mtime=rejected_mtime,
        )
        await _wait_for(
            lambda: store.status == "ended"
            and store.meta.get("map", {}).get("id") == "map-2")
        assert task.done() is False
    finally:
        await _cancel(task)


@pytest.mark.asyncio
async def test_file_rewritten_during_read_is_rejected_then_retried(
        tmp_path, monkeypatch):
    state_file = tmp_path / "state.json"
    meta_file = tmp_path / "meta.json"
    revision = time.time()
    _write_json(meta_file, {"schema": 1, "roster": []}, mtime=revision)
    _write_json(
        state_file,
        {"schema": 1, "active": True, "battleId": "old", "ships": []},
        mtime=revision,
    )
    real_read_json = S.read_json
    raced = {"done": False}

    def rewrite_during_read(path):
        payload = real_read_json(path)
        if os.path.abspath(path) == os.path.abspath(state_file) and not raced["done"]:
            raced["done"] = True
            _write_json(
                state_file,
                {"schema": 1, "active": False, "battleId": "new", "ships": []},
                mtime=revision + 1.0,
            )
        return payload

    monkeypatch.setattr(S, "read_json", rewrite_during_read)
    store = S.Store(instance_id="watcher-test")
    recorder = _RecordingWs()
    store.ws_clients.add(recorder)
    app = {S.STORE_KEY: store}
    task = asyncio.create_task(
        S.file_watcher(app, str(state_file), str(meta_file), 0.01))
    try:
        health = await _ingestion_health(app)
        await _wait_for(lambda: health.rejected_revision is not None)
        await _wait_for(lambda: store.status == "ended")

        assert health.rejected_revision["source"] == "state"
        assert health.rejected_revision["reason"] == "changed_during_read"
        assert recorder.messages[0]["source"]["status"] == "ended"
        assert recorder.messages[0]["battleId"] == "new"
        assert task.done() is False
    finally:
        await _cancel(task)


class _RecordingWs:
    def __init__(self):
        self.messages = []

    async def send_str(self, payload):
        self.messages.append(json.loads(payload))


@pytest.mark.asyncio
async def test_old_active_file_is_first_published_as_stale(tmp_path):
    state_file = tmp_path / "state.json"
    meta_file = tmp_path / "meta.json"
    old_mtime = time.time() - 30.0
    _write_json(meta_file, {"schema": 1, "roster": []}, mtime=old_mtime)
    _write_json(
        state_file,
        {"schema": 1, "active": True, "battleId": "old-battle"},
        mtime=old_mtime,
    )
    store = S.Store(instance_id="watcher-test")
    recorder = _RecordingWs()
    store.ws_clients.add(recorder)
    app = {S.STORE_KEY: store}
    task = asyncio.create_task(
        S.file_watcher(app, str(state_file), str(meta_file), 0.01))
    try:
        await _wait_for(lambda: bool(recorder.messages))
        assert recorder.messages[0]["source"]["status"] == "stale"
        assert recorder.messages[0]["source"]["updatedAt"] == pytest.approx(old_mtime)
        assert store.last_state_update == pytest.approx(old_mtime)
    finally:
        await _cancel(task)
