@echo off
rem Double-click this to remove the shortcuts and, if the installer downloaded one,
rem the copy of the app it made.
title Uninstall Revenant
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0uninstall.ps1"
echo.
pause
