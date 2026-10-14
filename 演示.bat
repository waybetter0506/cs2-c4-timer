@echo off
REM Demo: replay the real capture through the overlay. No game needed.
REM NOTE: pure ASCII + CRLF; cmd.exe mis-parses UTF-8 Chinese and LF-only endings.
chcp 65001 >nul
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1
cd /d "%~dp0"
echo Replaying the real GSI capture through the overlay (8x speed).
echo Watch the top-right corner of the screen. Press Ctrl+C to stop early.
echo.
python -m c4timer demo --speed 8 --show-idle
echo.
echo [demo exited] press any key to close.
pause >nul
