@echo off
chcp 65001 >nul
cd /d "%~dp0"
".venv\Scripts\python.exe" -m fcoh.cli build examples/mounting-bracket.fcoh.yaml --cad fusion --wait 180
pause
