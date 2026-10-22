@echo off
rem AI Kaiwa Kansatsushitsu - stop (ASCII only; the folder path comes from %~dp0)
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0launcher\stop.ps1" %*
