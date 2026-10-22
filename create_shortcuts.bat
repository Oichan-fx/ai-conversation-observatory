@echo off
rem AI Kaiwa Kansatsushitsu - create desktop shortcuts (ASCII only; the folder path comes from %~dp0)
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0launcher\create_shortcuts.ps1" %*
