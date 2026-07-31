@echo off
setlocal EnableExtensions
set "ROOT=%~dp0"
if "%ROOT:~-1%"=="\" set "ROOT=%ROOT:~0,-1%"
set "WEB_HOST=127.0.0.1"
set "WEB_PORT=3027"
set "API_BASE=http://127.0.0.1:8027"
set "NEXT_BIN=%ROOT%\frontend\node_modules\.bin\next.cmd"

if not exist "%NEXT_BIN%" (
    echo [error] Frontend dependencies are missing. Run: cd frontend ^&^& npm install
    pause
    exit /b 1
)

REM Refuse to touch anything if the port is already taken. The cache wipe below
REM is destructive, so doing it first meant a failed bind left the ALREADY
REM RUNNING server serving 404s from a deleted .next — a working app broken by
REM a start attempt. Check first, destroy nothing on failure.
REM Matched on addresses, not on the state word: netstat is localized (it prints
REM ABHOEREN on a German Windows, not LISTENING), so a /c:"LISTENING" filter
REM would never fire here. A listening socket is the one whose foreign address
REM is 0.0.0.0:0 - that pair is the same in every language.
netstat -ano -p tcp | findstr /c:"%WEB_HOST%:%WEB_PORT% " | findstr /c:"0.0.0.0:0" >nul 2>&1
if not errorlevel 1 (
    echo [error] Port %WEB_PORT% is already in use - the frontend is probably running.
    echo         Open http://%WEB_HOST%:%WEB_PORT% , or stop that process first.
    echo         Nothing was changed.
    pause
    exit /b 1
)

if exist "%ROOT%\frontend\.next" (
    echo Clearing frontend build cache...
    rmdir /s /q "%ROOT%\frontend\.next" >nul 2>&1
)

mkdir "%ROOT%\frontend\.next\server" >nul 2>&1
> "%ROOT%\frontend\.next\server\middleware-manifest.json" echo {"version":3,"middleware":{},"functions":{},"sortedMiddleware":[]}

cd /d "%ROOT%\frontend"
set "NEXT_PUBLIC_API_BASE_URL=%API_BASE%"
call "%NEXT_BIN%" dev -H %WEB_HOST% -p %WEB_PORT%
