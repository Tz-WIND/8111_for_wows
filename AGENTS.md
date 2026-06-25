# AGENTS.md

## Cursor Cloud specific instructions

This repo is a `uv`-managed Python app: an "8111-style" HTTP + WebSocket telemetry
server for World of Warships (`server/server.py`, aiohttp). The `mod/` and
`tools/probe/` directories are game-side Python 2.7 mods that only run inside the
WoWS client and cannot be exercised in this environment.

- Dependencies are managed by `uv` (see `pyproject.toml` / `uv.lock`); the update
  script runs `uv sync`. `uv` installs to `~/.local/bin`, which is on PATH for the
  cloud agent user.
- Run the server in synthetic mode (no game needed):
  `uv run python server/server.py --demo` — serves on `http://127.0.0.1:8111`
  (overlay at `/overlay`, endpoint index at `/`, WebSocket at `/ws`).
- Offline self-test against bundled fixtures (no game needed):
  `uv run python server/server.py --state-file server/sample_data/state.json`.
- Quick checks: `curl http://127.0.0.1:8111/healthz` and
  `uv run python server/examples/ws_client.py --port 8111 --messages 3`.
- There is no test suite and no configured linter; "build" is just running the app.
- Real game usage needs a root `config.ini` (copy from `config.example.ini`) with
  `game_dir` pointing at a WoWS install — not possible in this VM, so use `--demo`
  or `--state-file` for development/testing.
