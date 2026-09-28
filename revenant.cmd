@echo off
rem revenant - CLI shim. Put this folder on PATH and call `revenant` from anywhere.
rem `revenant gui` opens the desktop app.
rem No parenthesised block here: %ERRORLEVEL% inside one expands at parse time
rem and would always report the previous command's exit code.
setlocal
set "HERE=%~dp0"
rem A copy the installer downloaded records the Python it checked, 3.10 or newer.
rem Without that, the py launcher's newest Python 3, or the first python on PATH.
set "PY="
if exist "%HERE%.revenant-python" set /p PY=<"%HERE%.revenant-python"
rem A folder of that name would pass `if exist` too; the trailing backslash tells.
if defined PY if exist "%PY%" if not exist "%PY%\" goto :recorded
where py >nul 2>&1 && goto :launcher
python "%HERE%revenant.py" %*
exit /b %ERRORLEVEL%
:recorded
"%PY%" "%HERE%revenant.py" %*
exit /b %ERRORLEVEL%
:launcher
py -3 "%HERE%revenant.py" %*
exit /b %ERRORLEVEL%
