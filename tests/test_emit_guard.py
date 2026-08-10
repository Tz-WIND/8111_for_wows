# MIT License -- quit-vs-tick emit guard for the in-game collector.
import os
import sys

MOD_DIR = os.path.join(
    os.path.dirname(__file__), "..", "mod", "PnFMods", "WowsExtractor")
sys.path.insert(0, MOD_DIR)

import emit_guard as G  # noqa: E402


def test_rejects_live_frame_after_collector_quit():
    assert G.should_write_state(False, {"schema": 1, "active": True}) is False


def test_accepts_terminal_inactive_frame_after_quit():
    assert G.should_write_state(False, {"schema": 1, "active": False}) is True


def test_accepts_live_frame_while_battle_active():
    assert G.should_write_state(True, {"schema": 1, "active": True}) is True


def test_rejects_non_dict_snapshots():
    assert G.should_write_state(True, None) is False
