@echo off
setlocal
rem Launcher: locate a WORKING Python 3 interpreter and run check-usage.py with the same args.
rem The logic lives in check-usage.py; this wrapper is the Windows entry point.
rem
rem Each candidate is validated with `-c "import sys"` before use so the Windows Store
rem "python.exe" app-execution-alias stub (which prints an install message and exits nonzero)
rem is skipped instead of being run. `exit /b` with no argument is deliberate: it preserves the
rem interpreter's real exit code, whereas `exit /b %errorlevel%` inside a parenthesized block
rem would be expanded at parse time and always report 0.
set "SCRIPT=%~dp0check-usage.py"
set "ARGS=%*"

py -3 -c "import sys" >nul 2>nul && (
  py -3 "%SCRIPT%" %ARGS%
  exit /b
)
python -c "import sys" >nul 2>nul && (
  python "%SCRIPT%" %ARGS%
  exit /b
)
python3 -c "import sys" >nul 2>nul && (
  python3 "%SCRIPT%" %ARGS%
  exit /b
)

rem No usable interpreter: still honor the "JSON on every exit path" contract.
echo claude-usage: no working python interpreter found on PATH 1>&2
echo {"available": false, "reason": "no working python interpreter found on PATH"}
exit /b 2
