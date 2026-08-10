# MIT License -- pure battle identity regression tests.
import os
import sys


MOD_DIR = os.path.join(
    os.path.dirname(__file__), "..", "mod", "PnFMods", "WowsExtractor")
sys.path.insert(0, MOD_DIR)

import battle_identity as B  # noqa: E402


def test_fallback_ids_do_not_repeat_between_game_sessions():
    assert B.make_fallback_battle_id("session-a", 1, 7) != (
        B.make_fallback_battle_id("session-b", 1, 7))


def test_fallback_ids_advance_within_one_game_session():
    model = B.BattleIdentity("session-a")
    first = model.start(player_id=7)
    second = model.start(player_id=7)
    assert first != second


def test_late_arena_id_replaces_fallback_in_meta_and_state():
    model = B.BattleIdentity("session-a")
    fallback = model.start(player_id=7)
    assert model.promote("42") == "arena-42"
    assert model.current != fallback
    assert model.current == "arena-42"


def test_invalid_late_arena_id_does_not_erase_fallback():
    model = B.BattleIdentity("session-a")
    fallback = model.start(player_id=7)
    assert model.promote(None) == fallback
    assert model.promote(0) == fallback
