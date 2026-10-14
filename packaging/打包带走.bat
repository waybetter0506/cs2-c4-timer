@echo off
REM Pack this whole folder (program + learned audio data) into ONE zip,
REM so you can carry it to another PC (e.g. an internet cafe) through WeChat.
REM NOTE: pure ASCII + CRLF on purpose; cmd.exe mis-parses UTF-8 Chinese in .bat.
chcp 65001 >nul
cd /d %~dp0
powershell -NoProfile -ExecutionPolicy Bypass -Command "$src=(Get-Location).Path; $dst=Join-Path (Split-Path $src -Parent) ('C4Timer_portable_'+(Get-Date -Format 'yyyyMMdd_HHmm')+'.zip'); Compress-Archive -Path (Join-Path $src '*') -DestinationPath $dst -Force; Write-Host ''; Write-Host ('[OK] '+$dst)"
echo.
echo The zip was written to the PARENT folder of this one.
echo Send it to your WeChat "File Transfer Helper", then on the other PC:
echo   1. download it there
echo   2. unzip it anywhere
echo   3. run the BAT files inside (install GSI config first, then start)
echo.
pause
