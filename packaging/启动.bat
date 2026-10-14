@echo off
REM c4timer launcher (packaged build).
REM NOTE: pure ASCII + CRLF on purpose; cmd.exe mis-parses UTF-8 Chinese in .bat.
chcp 65001 >nul
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1
cd /d %~dp0

if not exist c4timer.exe (
  echo [!] c4timer.exe not found next to this .bat.
  echo     Unzip the whole folder first, then run this file inside it.
  pause
  exit /b 1
)

REM Auto-calibration: build fingerprint references from the plant clips saved by
REM earlier rounds, but ONLY when no reference exists yet (never overwrites yours).
c4timer.exe calibrate

c4timer.exe run
set RC=%ERRORLEVEL%
if "%RC%"=="0" exit /b 0
echo.
echo [!] c4timer exited with code %RC%.
echo     If the overlay never showed up, run 体检.bat and read the 结论 line.
pause
exit /b %RC%
