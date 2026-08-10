@echo off
REM ============================================================
REM  8111 for WoWS -- start the local HTTP + WebSocket server
REM  (aiohttp, managed by uv).
REM
REM  All settings (game folder, port, poll interval) live in
REM  config.ini next to this file -- edit that, then double-click.
REM
REM  You can still override on the command line, e.g.:
REM     run_server.bat --demo
REM     run_server.bat --game-dir "D:\Games\World_of_Warships"
REM     run_server.bat --port 8125
REM     run_server.bat --config "D:\path\to\other-config.ini"
REM ============================================================

setlocal

cd /d "%~dp0"

where uv >nul 2>nul
if errorlevel 1 (
    echo [error] uv not found. Install it from https://docs.astral.sh/uv/
    pause
    exit /b 1
)

REM create .venv and install aiohttp if needed (fast no-op once synced)
uv sync --no-dev

REM no args -> read everything from config.ini; otherwise pass flags through
uv run --no-dev python server/server.py %*

endlocal
pause
