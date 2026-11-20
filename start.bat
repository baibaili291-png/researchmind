@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo Starting ResearchMind on http://127.0.0.1:8000
echo Press Ctrl+C to stop.
"C:\Users\Lenovo\.workbuddy\binaries\python\versions\3.13.12\python.exe" -m backend.api.server --port 8000
pause
