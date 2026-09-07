@echo off
REM ===================================================================
REM  MarketBrief - generate + build now (no publish, no notify)
REM  Check the result with:  python -m mbrief preview
REM ===================================================================
setlocal

cd /d "%~dp0.."

set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1

REM [SECURE] Prefer the project virtualenv by absolute path
set "PY=%CD%\.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python"

if not exist "logs" mkdir "logs"

echo [1/2] generating briefing (claude -p, usually 5-8 min)...
"%PY%" -m mbrief generate %*
set EXITCODE=%ERRORLEVEL%
if not "%EXITCODE%"=="0" (
    echo generate failed ^(exit=%EXITCODE%^). See logs\mbrief.log
    endlocal & exit /b %EXITCODE%
)

echo [2/2] building static site...
"%PY%" -m mbrief build
set EXITCODE=%ERRORLEVEL%
if not "%EXITCODE%"=="0" (
    echo build failed ^(exit=%EXITCODE%^). See logs\mbrief.log
    endlocal & exit /b %EXITCODE%
)

echo.
echo Done. Preview with:
echo     "%PY%" -m mbrief preview

endlocal & exit /b 0

REM --------------------------------------------------
REM Security Checklist
REM Applied:
REM   - OS Command Injection: arguments (%*) are passed only to the mbrief CLI argparse, never to a shell string
REM   - Missing Error Handling: each step's exit code is checked and propagated
REM   - Improper Authorization: publish and notify are never run here, so nothing is made public by accident
REM Not Applied:
REM   - [WARN] none
REM --------------------------------------------------
