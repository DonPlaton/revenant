@echo off
rem Double-click this to install Revenant. Same thing install.ps1 does, wrapped so
rem Explorer can run it. Add -NativeWindow below for a frameless window instead of
rem a chromeless browser one; it installs pywebview, which is why it is not default.
title Install Revenant
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1"
echo.
pause
