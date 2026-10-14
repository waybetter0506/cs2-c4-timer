@echo off
REM Tell the program where CS2 is installed.
REM Use this ONLY if 体检.bat says it cannot find the CS2 cfg folder
REM (happens with unusual Steam install locations).
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

if not "%~1"=="" (
  c4timer.exe set-cs2 --dir "%~1"
  goto done
)

echo Drag the "Counter-Strike Global Offensive" folder into this window,
echo then press Enter. Do not type quotes.
echo.
c4timer.exe set-cs2

:done
echo.
pause
