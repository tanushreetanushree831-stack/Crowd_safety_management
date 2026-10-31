@echo off
rem Double-click to start CrowdSense. Works with no internet (ESP32 Wi-Fi only).
cd /d "%~dp0"
start "" cmd /c "timeout /t 8 >nul & start http://127.0.0.1:5000"
python app.py
pause