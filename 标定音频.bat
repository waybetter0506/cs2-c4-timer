@echo off
REM Build audio fingerprint references from the plant clips the program saved, then show the scores.
REM NOTE: pure ASCII + CRLF; cmd.exe mis-parses UTF-8 Chinese and LF-only endings.
REM Prerequisite (this is the one step that cannot be automated): play one round with 启动.bat
REM running, so that runtime\audio_dumps\plant_*.wav exists.
chcp 65001 >nul
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1
cd /d "%~dp0"

set "PY=python"
where python >nul 2>nul || set "PY=%LOCALAPPDATA%\Programs\Python\Python312\python.exe"

echo ==============================================================================
echo  c4timer audio calibration (from your own recordings)
echo ==============================================================================
echo.
echo  This builds the "announcement fingerprint" references from the clips that
echo  启动.bat saved automatically after each bomb plant.
echo.
echo  If nothing is built, it means there is no material yet:
echo    play one round with 启动.bat running, then run this again.
echo.

"%PY%" tools\build_fingerprints.py
if errorlevel 1 goto :no_material

echo.
echo  ---- score check (your plant clips vs background) ----
echo.
"%PY%" tools\fp_eval.py

echo.
echo  What good looks like: the four plant clips should score well above
echo  "need=16" while live snapshots / noise stay below it.
echo  Run 启动.bat next; every plant should then log "播报候选 -> 确认".
echo.
echo press any key to close.
pause >nul
exit /b 0

:no_material
echo.
echo  [!] No material yet (or the build failed). Steps:
echo      1) double-click 启动.bat and play one round (it saves a 10s clip per plant)
echo      2) double-click this file again
echo.
echo press any key to close.
pause >nul
exit /b 1
