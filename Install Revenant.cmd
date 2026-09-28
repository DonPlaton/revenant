@echo off
rem Double-click this to install Revenant.
rem
rem Next to install.ps1, in the downloaded zip or a clone, it installs from that
rem folder. Downloaded on its own from the releases page, it fetches the installer
rem from GitHub and installs the current version, the same as the one-liner.
rem
rem Add -NativeWindow after install.ps1 below, or after the closing bracket of the
rem one-liner, for a frameless window instead of a chromeless browser one. It
rem installs pywebview, which is why it is not the default.
title Install Revenant
if exist "%~dp0install.ps1" goto local
powershell -NoProfile -ExecutionPolicy Bypass -Command "& ([scriptblock]::Create((Invoke-RestMethod 'https://raw.githubusercontent.com/DonPlaton/revenant/main/install.ps1')))"
goto done
:local
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1"
:done
echo.
pause
