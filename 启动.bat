@echo off
REM Start c4timer (GSI + overlay). Before that: auto-calibrate audio if needed.
REM NOTE: pure ASCII + CRLF; cmd.exe mis-parses UTF-8 Chinese and LF-only endings.
chcp 65001 >nul
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1
cd /d %~dp0
REM Auto-calibration: build fingerprint references from the plant clips saved by earlier
REM rounds, but ONLY when no reference exists yet (never overwrites your own).
python tools\auto_calibrate.py
python -m c4timer run
