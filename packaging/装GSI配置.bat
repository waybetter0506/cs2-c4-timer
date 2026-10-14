@echo off
REM Install the GSI config into CS2's cfg folder (the only write to the game dir).
REM NOTE: pure ASCII + CRLF on purpose; cmd.exe mis-parses UTF-8 Chinese in .bat.
chcp 65001 >nul
set PYTHONIOENCODING=utf-8
cd /d %~dp0
c4timer.exe install-gsi
echo.
pause
