@echo off
REM Live GSI monitor (console only, no overlay). Keep this window open while playing.
REM NOTE: pure ASCII + CRLF.
chcp 65001 >nul
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1
cd /d "%~dp0"
python -m c4timer listen
echo.
echo [monitor exited] press any key to close.
pause >nul
