@echo off
REM ============================================================
REM  8111 for WoWS -- start the local HTTP + WebSocket server
REM  (aiohttp, managed by uv).
REM
REM  Edit GAME_DIR below to point at your World_of_Warships
REM  install folder, then double-click this file.
REM
REM  Or run directly, e.g.:
REM     run_server.bat --demo
REM     run_server.bat --game-dir "D:\Games\World_of_Warships"
REM     run_server.bat --state-file "D:\...\WowsExtractor\state.json"
REM ============================================================

setlocal
set "GAME_DIR=D:\Games\World_of_Warships"
set "PORT=8111"

cd /d "%~dp0"

where uv >nul 2>nul
if errorlevel 1 (
    echo [error] uv not found. Install it from https://docs.astral.sh/uv/
    pause
    exit /b 1
)

REM create .venv and install aiohttp if needed (fast no-op once synced)
uv sync

if "%~1"=="" (
    uv run python server/server.py --port %PORT% --game-dir "%GAME_DIR%"
) else (
    uv run python server/server.py --port %PORT% %*
)

endlocal
pause
