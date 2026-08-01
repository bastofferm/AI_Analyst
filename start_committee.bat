@echo off
REM ============================================================================
REM  AI_Analyst - standalone Investment Committee launcher
REM ----------------------------------------------------------------------------
REM  Starts both services and opens the app in your browser:
REM     * FastAPI backend   http://127.0.0.1:8027
REM     * Next.js frontend  http://127.0.0.1:3027
REM  Each runs in its own minimized window; close a window to stop that service.
REM
REM  Prerequisites (one-time):
REM    1. Python venv:   py -3 -m venv .venv
REM                      .venv\Scripts\pip install -r backend\requirements.txt
REM    2. Frontend deps: cd frontend ^&^& npm install
REM    3. A local Postgres "xbrl_sec" database must be running, and these user
REM       environment variables set (once, from any terminal):
REM           setx PGPASSWORD "your-postgres-password"
REM           setx DEEPSEEK_API_KEY "sk-your-key-here"
REM       ...or paste a DeepSeek key into the app UI for a single session.
REM ============================================================================
setlocal EnableExtensions
set "ROOT=%~dp0"
if "%ROOT:~-1%"=="\" set "ROOT=%ROOT:~0,-1%"
set "API_HOST=127.0.0.1"
set "API_PORT=8027"
set "WEB_HOST=127.0.0.1"
set "WEB_PORT=3027"
set "API_BASE=http://%API_HOST%:%API_PORT%"
set "WEB_BASE=http://%WEB_HOST%:%WEB_PORT%"

REM --- Python: repo venv if present, else system python ---
set "PYTHON_EXE=%ROOT%\.venv\Scripts\python.exe"
if not exist "%PYTHON_EXE%" set "PYTHON_EXE=python"
set "NEXT_BIN=%ROOT%\frontend\node_modules\.bin\next.cmd"
set "FRONTEND_START=%ROOT%\start_frontend.bat"
if not exist "%NEXT_BIN%" (
    echo [error] Frontend dependencies are missing. Run: cd frontend ^&^& npm install
    pause
    exit /b 1
)

REM --- Backend runtime env (inherited by the child windows) ---
set "PYTHONPATH=%ROOT%\backend"
set "MZQA_SKIP_SCHEMA=1"
REM Disable the shared SQLite LLM cache: it is not concurrency-safe and can
REM poison committee runs (empty-content replays / "database is locked").
set "MZQA_DISABLE_LLM_CACHE=1"
set "DATABASE_URL=postgresql://postgres@127.0.0.1:5432/xbrl_sec"
set "XBRL_SEC_DATABASE_URL=postgresql://postgres@127.0.0.1:5432/xbrl_sec"
set "DB_SCHEMA=sec"
set "XBRL_SEC_SCHEMA=sec"
set "ALLOWED_ORIGINS=http://localhost:%WEB_PORT%,http://127.0.0.1:%WEB_PORT%"
set "NEXT_PUBLIC_API_BASE_URL=%API_BASE%"

REM --- DeepSeek key check (non-fatal) ---
if defined DEEPSEEK_API_KEY goto :key_ok
reg query "HKCU\Environment" /v DEEPSEEK_API_KEY >nul 2>&1 && goto :key_ok
echo [warn] DEEPSEEK_API_KEY is not set. The committee will use its deterministic
echo        (no-LLM) path only until you set a key or paste one into the app UI.
echo.
:key_ok

echo Starting standalone backend   %API_BASE%/docs ...
start "AI_Analyst API" /min /D "%ROOT%" "%PYTHON_EXE%" -m uvicorn api.main:app --host %API_HOST% --port %API_PORT%

echo Starting standalone frontend  %WEB_BASE% ...
start "AI_Analyst Web" /min /D "%ROOT%" "%FRONTEND_START%"

echo Opening browser in a few seconds...
timeout /t 6 /nobreak >nul
start "" "%WEB_BASE%"
echo.
echo Standalone AI_Analyst services are starting in minimized windows:
echo   App:      %WEB_BASE%
echo   API docs: %API_BASE%/docs
echo Close those windows to stop.
endlocal
