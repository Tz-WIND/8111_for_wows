# MIT License -- HTTP/WebSocket contract tests.
"""Integration tests over a live aiohttp app: /all and /ws must serve
byte-identical content for a given cursor, /healthz must carry the envelope
identity, and the legacy overlay/ws_client payload shape must still parse.
"""
import asyncio
import json
import os
import sys
import warnings

import pytest
from aiohttp.web_exceptions import NotAppKeyWarning

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "server"))
import server as S  # noqa: E402


@pytest.fixture
def app_config(tmp_path):
    # Point at nonexistent files: the watcher polls harmlessly and never mutates
    # state, so tests seed the store directly for deterministic control.
    return {
        "demo": False,
        "interval": 0.05,
        "static_dir": S.DEFAULT_STATIC,
        "state_file": str(tmp_path / "missing_state.json"),
        "meta_file": str(tmp_path / "missing_meta.json"),
        "allowed_origins": set(),
    }


@pytest.fixture
async def client(aiohttp_client, app_config):
    app = S.build_app(app_config)
    return await aiohttp_client(app)


def test_build_app_uses_typed_app_keys_without_warnings(app_config):
    with warnings.catch_warnings():
        warnings.simplefilter("error", NotAppKeyWarning)
        app = S.build_app(app_config)

    assert S.STORE_KEY in app
    assert S.CONFIG_KEY in app
    assert S.STATIC_DIR_KEY in app
    assert S.INGESTION_HEALTH_KEY in app


def seed_battle(client):
    # Use a real recent timestamp so the live background watcher's staleness
    # tick (which compares against wall-clock) does not fire mid-test.
    store = client.app[S.STORE_KEY]
    store.apply(S.time.time(), meta={"schema": 1,
                                     "battleType": "RandomBattle",
                                     "roster": [{"playerId": 2000, "teamId": 0}]},
                state={"schema": 1, "active": True, "ts": 1.0,
                       "self": {"playerId": 2000, "health": 50000},
                       "ships": [{"uiId": 1, "playerId": 2000, "teamId": 0,
                                  "position": [0.0, 0.0, 0.0]}]})
    return store


async def test_all_and_ws_are_byte_identical(client):
    seed_battle(client)
    resp = await client.get("/all")
    rest_text = await resp.text()

    ws = await client.ws_connect("/ws")
    msg = await ws.receive()
    ws_text = msg.data
    await ws.close()

    assert rest_text == ws_text                    # same cursor -> same bytes
    obj = json.loads(rest_text)
    assert obj["seq"] == 1
    assert obj["source"]["status"] == "live"


async def test_repeat_all_read_does_not_advance_seq(client):
    seed_battle(client)
    a = json.loads(await (await client.get("/all")).text())
    b = json.loads(await (await client.get("/all")).text())
    assert a["seq"] == b["seq"] == 1


async def test_healthz_carries_envelope_identity(client):
    store = seed_battle(client)
    resp = await client.get("/healthz")
    hz = json.loads(await resp.text())
    assert resp.status == 200
    assert hz["serviceId"] == S.SERVICE_ID
    assert hz["apiVersion"] == S.API_VERSION
    assert hz["instanceId"] == store.instance_id
    assert hz["seq"] == 1
    assert hz["status"] == "live"
    assert hz["battleId"] == store.battle_id
    assert hz["ok"] is True
    assert hz["backgroundError"] is None
    # legacy healthz fields still present
    for key in ("ok", "battleActive", "ageSeconds", "version", "wsClients",
                 "hasMeta", "hasState"):
        assert key in hz, key


class _FailedBackgroundTask:
    def done(self):
        return True

    def cancelled(self):
        return False

    def exception(self):
        return RuntimeError("watcher exploded: " + "x" * 1000)


async def test_healthz_reports_bounded_background_failure(client):
    original = client.app[S.BACKGROUND_TASK_KEY]
    client.app._state[S.BACKGROUND_TASK_KEY] = _FailedBackgroundTask()
    try:
        resp = await client.get("/healthz")
        hz = json.loads(await resp.text())
    finally:
        client.app._state[S.BACKGROUND_TASK_KEY] = original

    assert hz["ok"] is False
    assert resp.status == 503
    assert hz["backgroundError"].startswith("RuntimeError: watcher exploded")
    assert len(hz["backgroundError"]) <= S.HEALTH_ERROR_MAX_CHARS


class _ClosingWs:
    def __init__(self):
        self.closed = False

    async def close(self):
        self.closed = True


class _BlockingWs:
    def __init__(self):
        self.started = asyncio.Event()
        self.closed = False

    async def close(self):
        self.started.set()
        await asyncio.Event().wait()
        self.closed = True


class _OrderedClients:
    def __init__(self, *items):
        self.items = list(items)

    def __iter__(self):
        return iter(list(self.items))

    def __len__(self):
        return len(self.items)

    def discard(self, item):
        if item in self.items:
            self.items.remove(item)


async def test_cleanup_records_failed_background_task_and_closes_websockets(app_config):
    app = S.build_app(app_config)

    async def fail_watcher():
        raise RuntimeError("cleanup watcher failure")

    task = asyncio.create_task(fail_watcher())
    await asyncio.sleep(0)
    assert task.done()
    ws = _ClosingWs()
    app[S.STORE_KEY].ws_clients.add(ws)
    app[S.BACKGROUND_TASK_KEY] = task

    await app.on_cleanup[0](app)

    assert ws.closed is True
    assert "RuntimeError: cleanup watcher failure" in (
        app[S.INGESTION_HEALTH_KEY].error or "")


async def test_cleanup_external_cancellation_is_re_raised_after_bounded_cleanup(
        app_config):
    app = S.build_app(app_config)
    background = asyncio.create_task(asyncio.Event().wait())
    blocking = _BlockingWs()
    closing = _ClosingWs()
    clients = _OrderedClients(blocking, closing)
    app[S.BACKGROUND_TASK_KEY] = background
    app[S.STORE_KEY].ws_clients = clients

    cleanup = asyncio.create_task(app.on_cleanup[0](app))
    await asyncio.wait_for(blocking.started.wait(), timeout=1.0)
    cleanup.cancel()

    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(cleanup, timeout=1.0)
    assert closing.closed is True
    assert blocking.closed is False
    assert blocking in clients.items
    assert closing not in clients.items


async def test_all_legacy_shape_still_parses(client):
    seed_battle(client)
    obj = json.loads(await (await client.get("/all")).text())
    # the shape ws_client.py / overlay.html rely on
    assert obj["schema"] == 1
    assert isinstance(obj["objects"], list) and obj["objects"]
    assert obj["objects"][0]["playerId"] == 2000
    assert obj["self"]["playerId"] == 2000
    assert obj["map"] is not None


async def test_empty_server_serves_fixed_empty_values(client):
    obj = json.loads(await (await client.get("/all")).text())
    assert obj["self"] is None
    assert obj["objects"] == []
    assert obj["roster"] == []
    assert obj["damage"] == {"inflicted": {}, "received": {}, "teamTotal": {}}
    assert obj["ballistics"] == {"available": False}
    assert obj["source"]["status"] == "waiting"
