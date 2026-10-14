@echo off
REM Install the GSI config into the CS2 cfg folder (the only game-dir write this project does).
REM NOTE: pure ASCII + CRLF; cmd.exe mis-parses UTF-8 Chinese and LF-only endings.
REM The Chinese wrapper name is what users double-click; the command itself is ASCII.
chcp 65001 >nul
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1
cd /d "%~dp0"
python -m c4timer install-gsi
echo.
echo [install-gsi exited] press any key to close.
pause >nul
