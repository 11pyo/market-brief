@echo off
REM ===================================================================
REM  MarketBrief - daily briefing run (called by Windows Task Scheduler)
REM  refresh -> score -> generate -> build -> publish -> notify
REM  Log: logs\daily.log (appended, rotated at 5 MB); details: logs\mbrief.log
REM ===================================================================
setlocal

cd /d "%~dp0.."

REM Force UTF-8 so Korean/emoji output does not break under cp949
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1

REM [SECURE] Prefer the project virtualenv by absolute path - avoids PATH hijacking and global-site-packages drift
set "PY=%CD%\.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python"

if not exist "logs" mkdir "logs"

REM Rotate daily.log when it exceeds 5 MB (keep one previous generation)
if exist "logs\daily.log" (
    for %%A in ("logs\daily.log") do if %%~zA GTR 5242880 (
        if exist "logs\daily.1.log" del /q "logs\daily.1.log"
        move /y "logs\daily.log" "logs\daily.1.log" >nul
    )
)

echo. >> "logs\daily.log"
echo ===== %DATE% %TIME% : daily start (%PY%) ===== >> "logs\daily.log"

"%PY%" -m mbrief daily >> "logs\daily.log" 2>&1
set EXITCODE=%ERRORLEVEL%

echo ===== %DATE% %TIME% : daily end (exit=%EXITCODE%) ===== >> "logs\daily.log"

endlocal & exit /b %EXITCODE%

REM --------------------------------------------------
REM Security Checklist
REM Applied:
REM   - OS Command Injection: no external arguments; fixed command only
REM   - Use of Vulnerable API: project .venv interpreter used by absolute path when present
REM   - Missing Error Handling: exit code propagated so Task Scheduler records failures
REM   - Error Message Information Exposure: output goes to local log files only
REM   - Resource exhaustion: daily.log rotated at 5 MB
REM Not Applied:
REM   - [WARN] If .venv is missing the script falls back to the global "python" on PATH.
REM     Run: python -m venv .venv && .venv\Scripts\pip install -r requirements.txt
REM --------------------------------------------------
