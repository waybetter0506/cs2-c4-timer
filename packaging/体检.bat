@echo off
REM Environment check: CS2 install / cfg / GSI port / effective params.
REM NOTE: pure ASCII + CRLF on purpose; cmd.exe mis-parses UTF-8 Chinese in .bat.
chcp 65001 >nul
set PYTHONIOENCODING=utf-8
cd /d %~dp0
c4timer.exe doctor
echo.
pause
