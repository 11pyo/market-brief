@echo off
REM ===================================================================
REM  MarketBrief - intraday tick (called every 30 min during KRX hours)
REM  Refreshes quotes/charts only. No LLM call, no briefing regeneration.
REM  Publishes only when the market data actually changed.
REM ===================================================================
setlocal

cd /d "%~dp0.."

set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1

REM [SECURE] Prefer the project virtualenv by absolute path - avoids PATH hijacking
set "PY=%CD%\.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python"

if not exist "logs" mkdir "logs"

REM Rotate tick.log when it exceeds 5 MB (keep one previous generation)
if exist "logs\tick.log" (
    for %%A in ("logs\tick.log") do if %%~zA GTR 5242880 (
        if exist "logs\tick.1.log" del /q "logs\tick.1.log"
        move /y "logs\tick.log" "logs\tick.1.log" >nul
    )
)

"%PY%" -m mbrief tick >> "logs\tick.log" 2>&1
set EXITCODE=%ERRORLEVEL%

endlocal & exit /b %EXITCODE%

REM --------------------------------------------------
REM Security Checklist
REM Applied:
REM   - OS Command Injection: no external arguments; fixed command only
REM   - Use of Vulnerable API: project .venv interpreter used by absolute path when present
REM   - Missing Error Handling: exit code propagated so Task Scheduler records failures
REM   - Error Message Information Exposure: output goes to a local log file only
REM   - Resource exhaustion: tick.log rotated at 5 MB
REM Not Applied:
REM   - [WARN] The KRX-hours gate lives in the app (INTRADAY_WINDOW), not in the schedule.
REM     Running this outside market hours is harmless: it exits immediately without network use.
REM --------------------------------------------------
