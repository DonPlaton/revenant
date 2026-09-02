@echo off
rem Double-click this to install Revenant. It is the same thing install.ps1 does,
rem with the native window turned on, wrapped so Explorer can run it.
title Install Revenant
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1" -NativeWindow
echo.
pause
