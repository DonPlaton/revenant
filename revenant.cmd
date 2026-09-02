@echo off
rem revenant - CLI shim. Put this folder on PATH and call `revenant` from anywhere.
rem `revenant gui` opens the desktop app.
rem No parenthesised block here: %ERRORLEVEL% inside one expands at parse time
rem and would always report the previous command's exit code.
setlocal
set "HERE=%~dp0"
where py >nul 2>&1 && goto :launcher
python "%HERE%revenant.py" %*
exit /b %ERRORLEVEL%
:launcher
py -3 "%HERE%revenant.py" %*
exit /b %ERRORLEVEL%
