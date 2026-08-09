"""Shared helpers for the contract tests.

The server module lives in `server/` and is run as a script rather than
installed as a package, so tests put that directory on `sys.path` the same way
`server.py` puts it there for its `maps` import.
"""

import asyncio
import contextlib
import json
import os
import sys
import time

import pytest
from aiohttp.test_utils import TestClient, TestServer

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SERVER_DIR = os.path.join(REPO_ROOT, "server")
if SERVER_DIR not in sys.path:
    sys.path.insert(0, SERVER_DIR)

import server as srv  # noqa: E402


TEST_POLL_INTERVAL = 0.02

# The flat body every pre-envelope client was written against. Nothing here may
# be renamed or dropped: `server/static/overlay.html` and
# `server/examples/ws_client.py` read these keys directly.
LEGACY_KEYS = (
    "schema", "active", "ts", "battleType", "gameMode", "map", "bounds",
    "boundsSource", "self", "objects", "roster", "damage", "ballistics", "diag",
)

ENVELOPE_KEYS = (
    "serviceId", "apiVersion", "instanceId", "seq", "battleId", "source",
    "capabilities", "availability", "extensions",
)


def run(coro):
    """Drive one coroutine to completion on a fresh event loop."""
    return asyncio.run(coro)


def make_store(**kwargs):
    kwargs.setdefault("poll_interval", TEST_POLL_INTERVAL)
    return srv.Store(**kwargs)


def active_state(**overrides):
    state = {
        "schema": 1,
        "active": True,
        "ts": 1.0,
        "self": {"playerId": 2000, "teamId": 0, "health": 40000,
                 "maxHealth": 80000, "yaw": 0.0, "speed": 20.0,
                 "position": [0.0, 0.0, 0.0]},
        "ships": [],
        "damage": {"inflicted": {}, "received": {}, "teamTotal": {}},
        "ballistics": {"available": False},
        "diag": {},
    }
    state.update(overrides)
    return state


def ended_state(**overrides):
    state = {"schema": 1, "active": False, "ts": 2.0, "ships": [], "self": None}
    state.update(overrides)
    return state


def basic_meta(**overrides):
    meta = {
        "schema": 1,
        "battleType": "RandomBattle",
        "gameMode": "Domination",
        "selfPlayerId": 2000,
        "map": {"name": "Test Atoll", "minX": -1000.0, "maxX": 1000.0,
                "minZ": -1000.0, "maxZ": 1000.0},
        "roster": [{"playerId": 2000, "teamId": 0, "relation": 0,
                    "name": "Tester", "shipName": "Yamato",
                    "shipType": "Battleship", "shipTier": 10}],
    }
    meta.update(overrides)
    return meta


async def wait_until(predicate, timeout=5.0, interval=0.01):
    """Poll an async predicate until it holds, or fail the test.

    File-watcher driven tests cannot know exactly when a write is observed, so
    they assert on the eventual state instead of sleeping a fixed amount.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if await predicate():
            return
        await asyncio.sleep(interval)
    raise AssertionError("condition not reached within %.1fs" % timeout)


@contextlib.asynccontextmanager
async def running_client(tmp_path, interval=TEST_POLL_INTERVAL):
    """A live app reading real files, plus the paths its watcher is polling."""
    state_file = os.path.join(str(tmp_path), "state.json")
    meta_file = os.path.join(str(tmp_path), "meta.json")
    app = srv.build_app({
        "demo": False,
        "interval": interval,
        "static_dir": srv.DEFAULT_STATIC,
        "state_file": state_file,
        "meta_file": meta_file,
        "allowed_origins": set(),
    })
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        yield client, Files(state_file, meta_file)
    finally:
        await client.close()


class Files:
    """Writes the collector's two JSON files the way the mod does."""

    def __init__(self, state_file, meta_file):
        self.state_file = state_file
        self.meta_file = meta_file

    def write_state(self, payload):
        self._write(self.state_file, json.dumps(payload))

    def write_meta(self, payload):
        self._write(self.meta_file, json.dumps(payload))

    def write_raw_state(self, text):
        self._write(self.state_file, text)

    @staticmethod
    def _write(path, text):
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text)
        # The watcher compares mtimes; nudge it so back-to-back writes in a test
        # are never collapsed by filesystem timestamp granularity.
        stamp = time.time()
        os.utime(path, (stamp, stamp))


@pytest.fixture
def srv_module():
    return srv
