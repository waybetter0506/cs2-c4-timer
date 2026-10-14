@echo off
REM Environment check: CS2 cfg, GSI port, audio devices, template source.
REM NOTE: pure ASCII + CRLF; cmd.exe mis-parses UTF-8 Chinese and LF-only endings.
chcp 65001 >nul
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1
cd /d "%~dp0"
python -m c4timer doctor
echo.
echo [doctor exited] press any key to close.
pause >nul
