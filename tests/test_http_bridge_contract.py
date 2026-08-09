"""Transport-level contract: identity on /healthz, REST/WS parity, torn files."""

import json

from conftest import (
    LEGACY_KEYS,
    active_state,
    basic_meta,
    ended_state,
    run,
    running_client,
    wait_until,
)

import server as srv


async def get_json(client, path):
    resp = await client.get(path)
    assert resp.status == 200
    return await resp.json()


# --- /healthz ------------------------------------------------------------


def test_healthz_exposes_service_identity(tmp_path):
    async def scenario():
        async with running_client(tmp_path) as (client, _files):
            health = await get_json(client, "/healthz")
            assert health["serviceId"] == srv.SERVICE_ID
            assert health["apiVersion"] == srv.API_VERSION
            assert isinstance(health["instanceId"], str) and health["instanceId"]
            # Pre-envelope fields must stay put.
            for key in ("ok", "battleActive", "ageSeconds", "version",
                        "wsClients", "hasMeta", "hasState"):
                assert key in health

    run(scenario())


def test_healthz_instance_id_matches_the_snapshot(tmp_path):
    async def scenario():
        async with running_client(tmp_path) as (client, _files):
            health = await get_json(client, "/healthz")
            snapshot = await get_json(client, "/all")
            assert health["instanceId"] == snapshot["instanceId"]

    run(scenario())


# --- cursor over HTTP ----------------------------------------------------


def test_repeated_rest_reads_share_one_cursor(tmp_path):
    async def scenario():
        async with running_client(tmp_path) as (client, files):
            files.write_state(active_state())
            await wait_until(lambda: is_active(client))

            first = await get_json(client, "/all")
            second = await get_json(client, "/all")
            assert first["seq"] == second["seq"]
            assert first == second

    run(scenario())


def test_rest_and_websocket_agree_at_the_same_cursor(tmp_path):
    async def scenario():
        async with running_client(tmp_path) as (client, files):
            files.write_meta(basic_meta())
            files.write_state(active_state())
            await wait_until(lambda: is_settled(client))

            # Retry a couple of times: a stale-status tick between the two reads
            # is a legitimate content change, not a parity violation.
            for _ in range(3):
                async with client.ws_connect("/ws") as ws:
                    pushed = json.loads((await ws.receive()).data)
                fetched = await get_json(client, "/all")
                if pushed["seq"] == fetched["seq"]:
                    assert pushed == fetched
                    return
            raise AssertionError("cursor kept moving between reads")

    run(scenario())


def test_new_frames_advance_the_cursor(tmp_path):
    async def scenario():
        async with running_client(tmp_path) as (client, files):
            files.write_state(active_state())
            await wait_until(lambda: is_active(client))
            before = (await get_json(client, "/all"))["seq"]

            files.write_state(active_state(ts=99.0))
            await wait_until(lambda: has_ts(client, 99.0))
            after = (await get_json(client, "/all"))["seq"]
            assert after > before

    run(scenario())


# --- torn / unreadable files --------------------------------------------


def test_torn_state_file_keeps_the_last_good_frame(tmp_path):
    async def scenario():
        async with running_client(tmp_path) as (client, files):
            files.write_state(active_state(ts=7.0))
            await wait_until(lambda: has_ts(client, 7.0))

            files.write_raw_state('{"schema": 1, "active": tr')
            # A later meta write proves the watcher completed a pass that also
            # read the half-written state file.
            files.write_meta(basic_meta())
            await wait_until(lambda: has_roster(client))

            snapshot = await get_json(client, "/all")
            assert snapshot["ts"] == 7.0
            assert snapshot["self"] is not None
            assert snapshot["source"]["status"] != srv.STATUS_ENDED

    run(scenario())


def test_torn_file_never_fabricates_a_battle_end(tmp_path):
    async def scenario():
        async with running_client(tmp_path) as (client, files):
            files.write_state(active_state(battleId="b-1"))
            await wait_until(lambda: is_active(client))

            files.write_raw_state("not json at all")
            files.write_meta(basic_meta())
            await wait_until(lambda: has_roster(client))

            snapshot = await get_json(client, "/all")
            assert snapshot["active"] is True
            assert snapshot["battleId"] == "b-1"

    run(scenario())


def test_unknown_top_level_keys_do_not_break_the_snapshot(tmp_path):
    async def scenario():
        async with running_client(tmp_path) as (client, files):
            files.write_state(active_state(somethingBrandNew={"x": 1}))
            await wait_until(lambda: is_active(client))
            snapshot = await get_json(client, "/all")
            assert snapshot["extensions"] == {}
            assert snapshot["self"] is not None

    run(scenario())


# --- battle lifecycle over the wire -------------------------------------


def test_battle_end_is_reported_as_ended(tmp_path):
    async def scenario():
        async with running_client(tmp_path) as (client, files):
            files.write_state(active_state(battleId="b-1"))
            await wait_until(lambda: is_active(client))

            files.write_state(ended_state(battleId="b-1"))
            await wait_until(lambda: has_status(client, srv.STATUS_ENDED))

            snapshot = await get_json(client, "/all")
            assert snapshot["battleId"] == "b-1"
            assert snapshot["self"] is None
            assert snapshot["objects"] == []

    run(scenario())


# --- legacy clients ------------------------------------------------------


def test_websocket_frames_still_carry_the_legacy_body(tmp_path):
    async def scenario():
        async with running_client(tmp_path) as (client, files):
            files.write_meta(basic_meta())
            files.write_state(active_state())
            await wait_until(lambda: is_settled(client))

            async with client.ws_connect("/ws") as ws:
                frame = json.loads((await ws.receive()).data)
            for key in LEGACY_KEYS:
                assert key in frame
            # overlay.html reads these directly.
            assert isinstance(frame["objects"], list)
            assert "teamTotal" in frame["damage"]

    run(scenario())


def test_legacy_side_endpoints_keep_working(tmp_path):
    async def scenario():
        async with running_client(tmp_path) as (client, files):
            files.write_meta(basic_meta())
            files.write_state(active_state())
            await wait_until(lambda: is_settled(client))

            assert isinstance(await get_json(client, "/map_obj.json"), list)
            assert (await get_json(client, "/map_info"))["boundsKnown"] is True
            assert isinstance(await get_json(client, "/roster"), list)
            assert "teamTotal" in await get_json(client, "/damage")
            assert "available" in await get_json(client, "/ballistics")
            assert (await get_json(client, "/indicators"))["health"] == 40000

    run(scenario())


# --- predicates ----------------------------------------------------------


async def is_active(client):
    return (await get_json(client, "/all"))["active"] is True


async def is_settled(client):
    """Both files observed: the state frame is live and the meta frame landed.

    The watcher can pick the two files up on different passes, so a test that
    reads meta-derived fields has to wait for meta specifically.
    """
    snapshot = await get_json(client, "/all")
    return (snapshot["active"] is True
            and bool(snapshot["roster"])
            and snapshot["bounds"] is not None)


async def has_ts(client, ts):
    return (await get_json(client, "/all"))["ts"] == ts


async def has_roster(client):
    return bool((await get_json(client, "/all"))["roster"])


async def has_status(client, status):
    return (await get_json(client, "/all"))["source"]["status"] == status
