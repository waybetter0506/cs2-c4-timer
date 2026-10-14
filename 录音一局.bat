@echo off
REM Record one session: CS2 loopback audio + GSI, sample-aligned (plant-announce template).
REM NOTE: pure ASCII + CRLF; cmd.exe mis-parses UTF-8 Chinese and LF-only endings.
chcp 65001 >nul
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1
cd /d %~dp0
echo ============================================================
echo   Audio + GSI capture  (plant-announce voice template)
echo ============================================================
echo.
echo  [!] Close the c4timer overlay first (it holds GSI port 30077).
echo  [!] Then play normally for a few rounds: plants, gunfire, deaths.
echo  [!] Press Ctrl+C in this window to stop.
echo.
python -X utf8 tools\record_audio.py %*
echo.
pause >nul
