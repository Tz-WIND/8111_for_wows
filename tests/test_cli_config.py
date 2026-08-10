# MIT License -- CLI source resolution without opening a listening socket.
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "server"))
import server as S  # noqa: E402


def _resolve(argv, tmp_path):
    return S.resolve_runtime_config(
        S.parse_args(argv), repo_root=str(tmp_path))


def test_explicit_state_file_selects_sibling_meta(tmp_path):
    state_file = tmp_path / "telemetry" / "state.json"
    result = _resolve(["--state-file", str(state_file)], tmp_path)

    assert result["app_config"]["demo"] is False
    assert result["app_config"]["state_file"] == str(state_file)
    assert result["app_config"]["meta_file"] == str(
        state_file.with_name("meta.json"))


def test_game_directory_selects_latest_build(tmp_path):
    game_dir = tmp_path / "World_of_Warships"
    old_dir = game_dir / "bin" / "100" / "res_mods" / "PnFMods" / "WowsExtractor"
    new_dir = game_dir / "bin" / "200" / "res_mods" / "PnFMods" / "WowsExtractor"
    old_dir.mkdir(parents=True)
    new_dir.mkdir(parents=True)

    result = _resolve(["--game-dir", str(game_dir)], tmp_path)

    assert result["app_config"]["state_file"] == str(new_dir / "state.json")
    assert result["app_config"]["meta_file"] == str(new_dir / "meta.json")


def test_missing_source_configuration_is_rejected(tmp_path):
    with pytest.raises(S.ConfigurationError, match="game_dir.*state-file"):
        _resolve([], tmp_path)


def test_demo_mode_needs_no_file_source(tmp_path):
    result = _resolve(["--demo"], tmp_path)

    assert result["app_config"]["demo"] is True
    assert result["app_config"]["state_file"] is None
    assert result["app_config"]["meta_file"] is None
    assert result["source_desc"] == "DEMO (synthetic data)"
