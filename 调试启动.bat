@echo off
cd /d "%~dp0"
python app.py --debug
if errorlevel 1 pause
