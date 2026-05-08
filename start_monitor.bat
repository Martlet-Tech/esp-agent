@echo off
title ESP-Agent Monitor Server
echo ============================================
echo  ESP-Agent Serial Monitor Server
echo ============================================
echo.
echo  Starting server on http://127.0.0.1:8099
echo  Open that address in your browser.
echo.
echo  Press Ctrl+C to stop.
echo ============================================
python -u "%~dp0serial_server.py" --port 8099
if %ERRORLEVEL% NEQ 0 (
    echo.
    echo  pyserial not found. Install with:
    echo    pip install pyserial
    pause
)
