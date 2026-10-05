@echo off
rem ============================================================
rem  start-all.bat
rem  Start the whole Toutiao-News project with one double-click:
rem    window 1 -> backend  (uvicorn, http://127.0.0.1:8000)
rem    window 2 -> frontend (vite,    http://127.0.0.1:5173)
rem  then open the browser.
rem
rem  Usage:
rem    double-click                   start everything
rem    start-all.bat --no-browser     same, but do not open the browser
rem
rem  Put this file in the BACKEND folder (toutiao_backend). It expects the
rem  frontend to sit next to it (..\xwzx-news), which is the layout here.
rem
rem  ------------------------------------------------------------
rem  WHY THIS FILE IS PURE ASCII - DO NOT ADD CHINESE LINES HERE
rem  ------------------------------------------------------------
rem  cmd.exe reads a .bat file using the system ANSI code page
rem  (936 / GBK on Chinese Windows), while text editors usually save as
rem  UTF-8. Chinese bytes then decode into garbage, AND the stray bytes eat
rem  the newline of the following line, so the next line gets glued onto the
rem  broken one - cmd reports
rem      'xxx' is not recognized as an internal or external command
rem  and the script dies. Putting "chcp 65001" in the file does NOT fix it
rem  (verified on this machine). So every message below stays ASCII.
rem  Chinese documentation lives in the chat / project notes, not here.
rem
rem  Also: keep the line endings CRLF (Windows), not LF.
rem ============================================================
setlocal

set "ROOT=%~dp0"
set "FRONT=%~dp0..\xwzx-news"
set "PY=%ROOT%.venv\Scripts\python.exe"
set "BE_PORT=8000"
set "FE_PORT=5173"
set "NOBROWSER="
if /i "%~1"=="--no-browser" set "NOBROWSER=1"

title Toutiao News - launcher

echo ============================================================
echo   Toutiao News  -  starting backend and frontend
echo ============================================================
echo   backend  dir : %ROOT%
echo   frontend dir : %FRONT%
echo.

rem ---------------- sanity checks ----------------
if not exist "%PY%" (
    echo [ERROR] virtualenv python not found:
    echo         %PY%
    echo.
    echo   Fix it once with:
    echo     cd /d "%ROOT%"
    echo     python -m venv .venv
    echo     .venv\Scripts\python.exe -m pip install -r requirements.txt
    echo.
    pause
    exit /b 1
)

if not exist "%FRONT%\package.json" (
    echo [ERROR] frontend project not found, expected a package.json in:
    echo         %FRONT%
    echo.
    pause
    exit /b 1
)

where npm >nul 2>&1
if errorlevel 1 (
    echo [WARN ] "npm" is not on PATH - the frontend window will fail.
    echo         Install Node.js, or start the frontend by hand.
    echo.
)

rem ---------------- backend ----------------
call :portbusy %BE_PORT%
if "%BUSY%"=="1" (
    echo [SKIP ] backend  - port %BE_PORT% is already listening, leave it alone
) else (
    echo [START] backend  - uvicorn on http://127.0.0.1:%BE_PORT%
    start "Toutiao backend :%BE_PORT%" /D "%ROOT%." cmd /k .venv\Scripts\python.exe -u -m uvicorn main:app --host 127.0.0.1 --port %BE_PORT%
)

rem ---------------- frontend ----------------
call :portbusy %FE_PORT%
if "%BUSY%"=="1" (
    echo [SKIP ] frontend - port %FE_PORT% is already listening, leave it alone
) else (
    echo [START] frontend - vite on http://127.0.0.1:%FE_PORT%
    start "Toutiao frontend :%FE_PORT%" /D "%FRONT%" cmd /k npm run dev
)

echo.
echo Waiting 8 seconds for the services to come up ...
rem  Why ping and not timeout.exe?
rem    - timeout.exe aborts immediately when its input is redirected
rem      ("input redirection is not supported"), and some environments put a
rem      different "timeout" earlier in PATH that fails with '/t'.
rem    - ping -n 9 does 9 one-second attempts and needs no console input.
rem  Absolute paths are used for the same reason.
"%SystemRoot%\System32\ping.exe" -n 9 127.0.0.1 >nul 2>&1

if not defined NOBROWSER (
    start "" "http://127.0.0.1:%FE_PORT%"
    echo Browser opened: http://127.0.0.1:%FE_PORT%
)

echo.
echo Done.
echo   - Each service runs in its own window; keep them open while browsing.
echo   - To stop a service: click its window and press Ctrl+C.
echo   - First page load may take ~5s (the backend has no Redis and waits
echo     out a 5s timeout before falling back to MySQL). Later calls are fast.
echo.
pause
exit /b 0

rem ------------------------------------------------------------
rem  helper: sets BUSY=1 when the port passed in as %%1 is listening
rem ------------------------------------------------------------
:portbusy
set "BUSY=0"
netstat -an | findstr ":%1" | findstr "LISTENING" >nul 2>&1
if not errorlevel 1 set "BUSY=1"
exit /b 0
