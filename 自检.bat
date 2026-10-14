@echo off
REM P0 self-test: GSI parser regression against the real capture fixture.
REM NOTE: pure ASCII + CRLF; cmd.exe mis-parses UTF-8 Chinese and LF-only endings.
chcp 65001 >nul
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1
cd /d "%~dp0"
python -m c4timer test
echo.
echo [self-test exited] press any key to close.
pause >nul
