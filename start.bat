@echo off
rem AI Kaiwa Kansatsushitsu - start (ASCII only; the folder path comes from %~dp0)
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0launcher\start.ps1" %*
