# MIT License -- WebSocket hello-frame ordering.
"""A connecting client must never observe a lower seq after a higher one.

The race: add to ws_clients, then await the initial send_str. A broadcast can
land a newer frame first; the delayed hello then delivers an older seq.
"""
import json

import pytest

from conftest import active_state, make_store
import server as srv


class RecordingWs:
    def __init__(self, on_first_send=None):
        self.frames = []
        self._on_first_send = on_first_send

    async def send_str(self, payload):
        self.frames.append(json.loads(payload))
        if self._on_first_send is not None and len(self.frames) == 1:
            callback = self._on_first_send
            self._on_first_send = None
            await callback()


@pytest.mark.asyncio
async def test_ws_hello_stays_monotonic_when_store_moves_during_send():
    store = make_store()
    store.apply(1.0, state=active_state(ts=1.0))
    hello_seq = store.seq

    async def bump():
        store.apply(2.0, state=active_state(ts=2.0))

    ws = RecordingWs(on_first_send=bump)
    await srv.open_ws_subscription(store, ws)

    seqs = [frame["seq"] for frame in ws.frames]
    assert seqs == sorted(seqs)
    assert seqs[0] == hello_seq
    assert seqs[-1] == store.seq
    assert store.seq > hello_seq
    assert ws in store.ws_clients


@pytest.mark.asyncio
async def test_ws_hello_single_frame_when_cursor_idle():
    store = make_store()
    store.apply(1.0, state=active_state(ts=1.0))
    ws = RecordingWs()
    await srv.open_ws_subscription(store, ws)
    assert len(ws.frames) == 1
    assert ws.frames[0]["seq"] == store.seq
    assert ws in store.ws_clients
